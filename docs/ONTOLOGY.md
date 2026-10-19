# Ontology: AR follow-up

The business map the system works on: which **objects** exist, how they **link** across systems,
which **signals** are derived from them, who the **roles** are, and which **actions** a case can
end in. Every layer (rules, LLM steps, agent, human review) reads and writes in these terms.

## Summary

Architecture diagram: `docs/architecture.mmd` (Mermaid; renders on GitHub).

**The ontology** is one shared model of the AR business over three systems that do not share
keys: billing (who owes what), Salesforce (who the customer is, renewals, tickets, calls) and
the inbox (what the customer said). Sync links them into one view per customer (§3); rules turn
that view into **signals** (§4); signals pick one **action** from a fixed list (§6), owned by a
**role** (§5). Every statement any layer makes cites an object ID (§2), so a reviewer can check it.

**The work splits by how much judgement a case needs:**

| Layer | Handles | How | Can it be wrong in a way we catch? |
|---|---|---|---|
| 1 Rules | all 58 follow-ups: detect, link, signal, pick action, rank, draft | SQL + Python, no LLM | acceptance vs labels (100/100) and an independent SQL balance check |
| 2 LLM steps | read replies (intent, promise date); write a cited brief per card | one stateless call each, then a validator | citations must resolve; every amount, date and number must be in the cited records |
| 3 Agent | dispute follow-ups only (8/58 = 14 %) | chooses which records to read through read-only tools, recommends with evidence | cites and numbers must be in what its tools returned this run |
| Human | every outcome | Slack card: approve / edit / change action / hold | decisions and audit rows are append-only |

**The agent** (§8) answers one question per dispute: *is the over-billing real, and has it
already been credited?* It reads invoices, invoice lines, credit notes, payments, CRM cases,
activities and replies through a narrow MCP server, calls a `calculate` code tool for any
arithmetic, and returns `issue_credit_note`, `close_case_resume_collection` or `needs_human`
with 3 to 5 cited findings. It cannot write anything; a person approves the outcome in Slack.

**Status (2026-10-16).** Layers 1-3 and human review implemented (`sync.py`, `rules.py`,
`llm_steps.py`, `agent.py` + `mcp_server.py`, `review.py`, `slack_app.py`). On the labelled
data: 100/100 customers linked, 0 wrong; 100/100 actions correct; replies 42/42; briefs 58/58
valid; disputes 8/8 correct; injected errors caught 316/316 (briefs) and 64/64 (agent).
Thresholds were tuned on this same data; re-check on held-out data at step 8.

## 1. Data sources

| Source | Stands in for | Access | Holds |
|---|---|---|---|
| Salesforce Developer Edition | CRM | REST, OAuth client credentials, read-only client (`arfollow/salesforce.py`) | Account, Contact, Opportunity, Case, Task |
| `data/out/billing.db` (SQLite) | NetSuite | `sqlite3` with `mode=ro` at runtime; only `scripts/load_db.py` writes | customers, invoices, invoice_lines, payments, charge_attempts, credit_notes, usage_monthly |
| `data/out/inbox.json` | AR mailbox | file read | customer replies |
| Slack (Socket Mode) | team workspace | bot token; buttons via Socket Mode | digest, review threads, decisions |
| `data/app.db` (SQLite) | the system's own store | read-write, system only | sync snapshot, matches, follow-ups, drafts, decisions, outbox, audit log |

The system writes only to its own store and to Slack. Salesforce and billing are read-only.

## 2. Object types

| Object | Source | ID (cited as) | Key properties |
|---|---|---|---|
| **Customer** | billing `customers` | `CUS-01042` | legal_name, plan, mrr_cents, payment_method, card_expiry, billing_email, currency |
| **Account** | SF `Account` | 18-char SF Id | Name, AccountNumber (billing ID, often missing or reformatted), Rating, Industry |
| **Contact** | SF `Contact` | SF Id | name, Title, Email |
| **Invoice** | billing `invoices` | `INV-104233` | issue_date, due_date, amount_cents, status (open/paid/void) |
| **InvoiceLine** | billing `invoice_lines` | `INV-104233-2` | description, quantity, unit_price_cents (seat over-billing shows here) |
| **Payment** | billing `payments` | `PAY-…` | amount_cents, received_at, reference, status (applied / **unapplied** = no invoice link) |
| **ChargeAttempt** | billing `charge_attempts` | `CH-00042` | attempted_at, result, failure_code (expired_card / insufficient_funds / do_not_honor) |
| **CreditNote** | billing `credit_notes` | `CN-0042` | invoice_id, amount_cents, reason |
| **UsageMonth** | billing `usage_monthly` | customer + month | active_users |
| **Opportunity** | SF `Opportunity` | SF Id | Type (Renewal / Upgrade), StageName, CloseDate, Amount; AE named in Description |
| **SupportCase** | SF `Case` | SF Id | Subject (`[Billing]` prefix = billing case), Status, Priority |
| **Activity** | SF `Task` | SF Id | Subject (`Call:` / `Email:` / `Meeting:`), ActivityDate, Description (payment promises live here) |
| **Message** | inbox | `MSG-0007` | from_email, received_at, subject, body |
| **FollowUp** | derived (app.db) | `FU-…` | one per customer with overdue or failed-charge exposure; the unit of work in Slack |
| **Decision / OutboxMessage / AuditEvent** | app.db | `DEC-…` / `OUT-…` / `AUD-…` | who, when, action, evidence IDs |

Every claim in a brief, draft or recommendation cites one of these IDs. The validator rejects
IDs that do not resolve.

## 3. Links

Billing and CRM share no reliable key on purpose: the data seeds the mismatches a real
integration has.

| Link | How it is established | Hard cases in the data |
|---|---|---|
| Customer ↔ Account | 1) `AccountNumber` normalised (`CUS1042` → `CUS-01042`); 2) normalised name (case, legal suffix, `Harbor/Harbour`, `Co/Company`, `The …`); 3) email domain of Contacts vs `billing_email` | 83 name variants, 9 missing IDs, 9 reformatted IDs. Below-threshold matches go to a human queue, never auto-linked |
| Invoice → Customer | `customer_id` FK | — |
| Payment → Invoice | FK when applied. Unapplied: invoice number in `reference` + exact amount | Amount alone is ambiguous: 4 unapplied payments match 16 open invoices by amount; the reference narrows each to one |
| SupportCase → Invoice | invoice ID in Subject / Description | Some dispute cases are vague ("Invoice query"); link at account level, invoice lines show the over-billing |
| Message → Customer | sender email → Contact → Account → Customer; else `billing_email`; else signature name | Some replies come from personal Gmail addresses |
| Opportunity → Account | `AccountId` | AE is only named in Description text |

## 4. Derived signals (per FollowUp)

| Signal | Derived from |
|---|---|
| `amount_overdue_cents`, `days_overdue` (oldest) | open invoices with `due_date < as_of`; amount = each invoice less credit notes against it (L-005) |
| `charge_failure` (code) | latest ChargeAttempt per open invoice failed |
| `open_billing_dispute` | SupportCase with `[Billing]` subject and Status not Closed; or a reply classified `dispute` |
| `credit_note_coverage` | CreditNote amount vs the disputed over-billing (agent tool `compare_seats`, computed in code) |
| `payment_claimed` | reply classified `paid`; or an unapplied Payment matching an overdue invoice |
| `open_renewal` | Opportunity Type Renewal, not Closed, CloseDate ahead |
| `late_history` | paid_at − due_date > 10 days across the last 12 invoices; broken promises in Activities |
| `usage_trend` | active_users over the last 4 months (a single-month dip is not a trend) |
| `reply_intent` (+ promise date) | layer-2 classification of the latest Message: paid / dispute / promise_to_pay / cancel / unclear |
| `customer_tier` | plan + mrr_cents (ranking weight) |

## 5. Roles

| Role | Who | Sees / does | Writes |
|---|---|---|---|
| AR Specialist (`ar_specialist`) | org.db role; the only user who may decide (unset = no one); customer-facing on payment | daily digest, every card; approve / edit / change action / hold; sends standard and gentle reminders; owns each case until handoff and after every handback | Decision (via button) |
| Billing Ops Manager (`billing_ops_manager`) | back office; contacts the customer only to agree a payment plan or give a suspension notice | disputes (invoice lines, contract, credit notes), payment verification and cash application, chronic-late escalations (call, payment plan, suspension, write-off) | handback outcome |
| Account Manager (`account_manager`) | Sales; customer-facing, owns the relationship | renewal cases, churn / retention; commercial concessions (agreed by the Account Manager, issued by the Billing Ops Manager) | handback outcome |
| Layer 1 workflow | system, no LLM | all source data (read) | FollowUp, ranking, rule outcomes |
| Layer 2 LLM steps | system | one FollowUp's records | brief, reply intent, draft (validated) |
| Layer 3 agent | system, dispute follow-ups only, step and budget caps | 9 read-only MCP tools + `calculate` (§8) | `agent_runs` row: recommendation, cited findings, tool transcript |

No system role can write to Salesforce or billing, or send email. Every human action is a Slack
button that writes a Decision and an AuditEvent.

## 6. Actions (case outcomes)

Labels in `labels.json` use these names; evaluation compares against them.

| Action | When (dominant signal) | Owner after AR approves | Effect | Handback to AR (outcome → next step) |
|---|---|---|---|---|
| `pause_and_resolve_dispute` | `open_billing_dispute` | Billing Ops Manager | dunning paused; dispute brief posted | upheld → credit note issued, AR reminds for the corrected balance; rejected → AR reminds with the explanation |
| `verify_payment_no_dunning` | `payment_claimed` | Billing Ops Manager | match candidate shown; no reminder | found → applied, case closed; not arrived → AR sends a standard reminder |
| `notify_account_owner` | `open_renewal` | Account Manager | Slack notification; no reminder | settled in the renewal → closed; renewal lost or stalled → AR resumes collection |
| `retention_handoff` | `reply_intent = cancel` or falling `usage_trend` | Account Manager | handoff note; no reminder | saved → AR resumes; leaving → AR collects the final balance; concession → Billing Ops Manager issues the credit |
| `escalate` | `late_history` (repeated late, broken promise) | Billing Ops Manager | escalation note; no polite reminder | payment plan → AR tracks the schedule; suspension → Billing Ops Manager tells the Account Manager before the notice; call → result logged |
| `gentle_reminder` | `charge_failure = expired_card`, good history | AR Specialist (no handoff) | outbox row + Slack copy | — |
| `standard_reminder` | overdue, no other signal | AR Specialist (no handoff) | outbox row + Slack copy | — |
| `needs_human` | evidence contradicts itself or a field is missing | AR Specialist triages | no message | — |
| `none` | not overdue, no current failure | — | — | — |

Links: one owner per case at a time; every handoff ends in a handback with an outcome, so the
case returns to the AR Specialist. The Account Manager agrees commercial concessions, the Billing Ops Manager issues them (only
Billing Ops changes invoices). The Billing Ops Manager tells the Account Manager before suspending
one of their accounts. Only the
AR Specialist approves outbound reminders; only the current owner records a handback.
Open hand-offs live in app.db `cases` (owner, evidence at hand-off, `wait_until`): while a case
is open and no record ID is new, the customer gets no card, only a digest line; a missed agreed
date makes `chronic_late` match; a case closes on handback or when nothing is overdue.

Precedence when signals overlap (`rules.PRECEDENCE`): dispute >
payment claimed > renewal > churn > chronic late > expired card > standard. Rationale: a wrong
reminder during a dispute or renewal damages the relationship more than a delayed one.

**Decoys** the rules must ignore: a closed billing case, an expired-card failure already
recovered by a later successful charge, a one-month seasonal usage dip.

## 7. Detection (layer 1, SQL, no LLM)

The demo clock is frozen at `as_of = 2026-10-15` (`billing.db` `meta` table), so data and labels stay consistent.

**Overdue invoices**
```sql
SELECT id, customer_id, due_date, amount_cents FROM invoices
WHERE status = 'open' AND due_date < :as_of;
```
Verified: 73 invoices, identical to the labelled set; 58 customers across the 7 non-healthy types.

**Current failed charges**: the latest attempt on an open invoice failed. Using "latest"
excludes recovered failures (20 customers have a failure in history, 16 are current).
```sql
SELECT DISTINCT ca.customer_id, ca.failure_code
FROM charge_attempts ca JOIN invoices i ON i.id = ca.invoice_id AND i.status = 'open'
WHERE ca.result = 'failed'
  AND ca.attempted_at = (SELECT max(attempted_at) FROM charge_attempts WHERE invoice_id = ca.invoice_id);
```

Detection answers "who owes what". Which action applies needs the CRM and inbox signals (§4):
half the "paid but unreconciled" customers have no payment record yet, so only their reply
reveals them.

## 8. Layer 3: the agent (`arfollow/agent.py`, `arfollow/mcp_server.py`)

**Why an agent here and nowhere else.** A dispute's path differs per case: the CRM case may name
the invoice or only say "looks too high"; a credit note may cover all, half or none of the
over-billing. Rules would need a branch per combination; the other six actions do not vary, so
they stay in layer 1.

**Tools.** A self-written MCP server (stdio subprocess) exposes only what an investigation needs.
Both databases are opened `mode=ro`, so writing is impossible, not just forbidden
(`tests/test_mcp_readonly.py`).

| Tool | Returns (with record IDs) |
|---|---|
| `get_customer` | plan, payment method, currency, MRR |
| `list_invoices` / `get_invoice_lines` | last 12 invoices; line items (users × unit price) |
| `compare_seats` | users billed vs the usual count on earlier invoices, extra users, over-billing, credit notes against it, uncredited remainder |
| `list_payments` / `list_credit_notes` | payment and credit history |
| `list_crm_cases` / `list_crm_activities` | Salesforce cases and calls/emails on the matched account |
| `list_replies` | customer emails in the AR inbox |
| `calculate` (local, not MCP) | exact + − × in integer cents; every operand must be a value already retrieved this run |

**Loop.** Input: the follow-up, customer, overdue invoice IDs, signals. The model (DeepSeek,
tool calling) picks tools until it can answer, then returns
`{disputed_invoice, recommendation, findings[{text, cites}]}`.
Limits: 8 tool rounds, 60k tokens; hitting either ends the run as `needs_human`. Typical run:
2-3 rounds, ~13k tokens, ~8 s.

**Validator (traceability).** The answer must parse (Pydantic); the disputed invoice and every
cited ID must have appeared in this run's tool results; every amount, date and number in a
finding must appear in those results (a `calculate` result counts, its operands were checked).
A rejected answer gets one repair round with the problems sent back; still invalid means
`needs_human`. Each run is stored in `agent_runs` with the full tool transcript.

**Proof** (`scripts/accept/step_6.py`): 8/8 recommendations match ground truth; real answers
all pass; 64/64 injected errors rejected (amount, date, invented count, unretrieved or unknown
citation, tool name as citation, wrong invoice, missing citation).

**Output.** The investigation is shown on the dispute card in Slack above the brief. The
reviewer still decides; the agent's recommendation is not yet copied into the Decision row.

## 9. Known gaps

- Account owner: no SF User records per AE; the AE name is parsed from the Opportunity Description.
- Reply intent: validated LLM label, keyword baseline as fallback. Synthetic replies are
  templated, so both score 100 %; a harder eval set is needed. Thresholds are tuned on the labelled set.
- Slack posts the top N cards per digest (`--limit`); the rest stay open in app.db.
