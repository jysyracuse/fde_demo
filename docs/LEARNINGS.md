# Learnings: ideas, tests, trade-offs

What we believed, how we tested it, what the data said, and what we chose. Mistakes that
should become rules live in `docs/agent/LESSONS.md`; this file links to them.
Format per entry: idea → test → result → trade-off → decision.

## 1. Jev for the follow-up action (2026-10-16/17)
- **Idea (user):** TypeSafe Jev is a structured-type model: a `Choice` with one description per
  option. Feed it the playbook's action descriptions and let it pick the action, including the
  best way to handle a dispute. Expected: cheaper and faster than an LLM, similar accuracy.
- **Test:** `scripts/eval_actions.py`, same case packet and same `PLAYBOOK.md` text for every
  arm. Two Jev variants: `jev.choice` (one 8-way choice) and `jev.checks` (one yes/no per
  playbook check, code applies the order). Sets: generated full set (58 overdue) and a holdout
  of 40 written by Pi / GPT-6.1-sol (precedence conflicts, missing data, new wording).
- **Result:**

  | arm | full set | holdout | needs_human on holdout | median latency |
  |---|---|---|---|---|
  | DeepSeek | 53/58 | 33/40 | 5/5 | 1,150 ms |
  | jev.checks | 52/58 | 32/40 | 0/5 | 237 ms |
  | jev.choice | 55/58 | 19/40 | 0/5 | (same call) |

  jev.choice ignores "first match wins" (expired card → gentle reminder 9 times on the
  holdout). Its wrong picks still had high confidence: 6 of 21 at 0.8 or more.
- **Trade-off:** Jev saves tokens and about 1 s per case. That does not matter at tens of
  cases a day, run as a batch, each reviewed by a human; DeepSeek costs cents for the whole
  holdout. What matters is the cost of a wrong action (dunning a customer who disputes or has
  paid) and the ability to say "not enough evidence": Jev answers yes/no and never routes to a
  human. Jev fits high-volume, real-time, short-text, few-option tasks; this is the opposite.
- **Decision (user, 2026-10-17):** not adopted. DeepSeek stays for reply text; Jev remains an
  eval arm only. Evidence: `docs/eval/2026-10-16-action-eval.md`, `data/eval/holdout/`.

## 2. Rules vs models: the first eval flattered the rules
- **Belief:** rules 57/58 on the full set showed that keyword rules are enough.
- **Test:** holdout written by a different model (Pi), from the playbook only.
- **Result:** rules 25/40. Keyword intent missed new dispute wording ("querying the seat
  line"), read "Not paid yet" as paid, and still read withdrawn cancellations as cancel. Rules
  have no path for a missing field (status null was treated as open).
- **Why:** the generator and the rules were written together by the same author, so every
  signal had the clean form the rules expect. The ranking reversed once someone else wrote the
  cases. Claude's 40/40 blind judgment is not independent either: Claude wrote the batch briefs
  that named the traps.
- **Trade-off chosen:** code owns precedence and structured fields (references, opportunity
  type and stage, counts, dates) and routes missing fields to a human; a model reads reply text
  (intent, promise date), validated. Lesson: L-009.

## 3. Decomposition and confidence
- **Decomposition** (`jev.checks`) hurt on the easy set (52 < 55) and helped on the hard one
  (32 > 19). One result on easy data predicted the wrong direction; the order "first match
  wins" belongs in code, not inside a model's choice.
- **Confidence is not a gate:** confident wrong answers on decoys (0.91, 0.94 on the full set;
  6/21 ≥ 0.8 on the holdout). Calibration needs its own test before any auto-action.
- **Labels need auditing too:** the eval found one wrong generator label (CUS-01091: 4 late
  payments in a standard case; every arm said escalate).

## 4. Roles and hand-offs: why they got confused
- **What happened:** routing was built first, with roles picked ad hoc per action (Billing Ops,
  Finance Lead, AE, Retention). Hand-offs had no return path, "Finance Lead" is not a real
  title, the Slack accounts were renamed mid-demo (now Billing Ops Manager and Account Manager), and the AR and AM jobs were both on the host account.
- **Cost:** DMs fixed after the fact (`dm_owner`), role users moved from env vars to
  `data/org.db`, and now a remap of role keys, playbook wording and card text; the eval reads
  playbook wording, so DeepSeek and Jev results must be rerun after the rename.
- **Root cause:** no responsibility matrix before building: who owns each action, who is
  customer-facing, who may change an invoice, how a case comes back to AR.
- **Fix:** `docs/ONTOLOGY.md` §5-6: three roles, owner per action, a handback with an outcome
  for every hand-off, one owner per case at a time. Lesson: L-008.
- **Learning:** for any workflow with people in it, write the role / action / handback table
  first and check the titles against real job ads; code the routing from that table.

## 5. Tooling
- `pi -p` in a background job waits for stdin EOF: the first holdout run produced nothing in
  2 h. Run it with `< /dev/null` (then 4 batches took about 2 min).

## 6. Case state across days: hand-offs that never come back (2026-10-17)
- **Question (user):** AR hands a customer to the Billing Ops
  Manager on 10-17; they agree with the customer to pay on
  11-01. Does the 10-18 run push the customer again?
- **Answer (code as of 10-17):** yes, every day. Follow-up IDs are per day (`FU-{date}-{cid}`,
  `rules.py:185`), so no decision carries over; a future promise suppresses nothing (only a
  passed promise is used, `rules.py:167`), and 4+ late payments still match chronic_late, so the
  Billing Ops Manager gets the same escalation daily until 11-01. Review findings 4.3 and 4.4 had been parked as
  "production later"; this example shows hand-offs are meaningless without them.
- **Needed:** a case that survives the day (owner, waiting until, reason); while waiting and no
  new signal, no card, one digest line; a new signal (dispute, paid claim) still makes a card;
  promise passed and unpaid → escalate as a broken arrangement. No LLM: the date is picked, the
  rule compares dates; a model only if the date must be read from free text.
- **Where the state lives (trade-off):** split by kind.
  - Business fact (promise date, payment plan) → Salesforce, entered by the Billing Ops Manager as a Task with the
    date in a structured field, as AR teams already do. AM and Sales see it; our system reads
    it and stays read-only (no new write path, review 4.8 untouched).
  - Workflow state (owner, open hand-off, handback outcome, card version) → `app.db`. Writing it
    to the CRM would mean: a write-capable integration user against the read-only rule; custom
    fields owned by the Salesforce admin; no transactions for atomic approve (review 4.5) and
    API limits; overwritten fields with limited history instead of an append-only log; and a
    second copy of state that can disagree with Slack.
  - If the Slack handback and the CRM promise disagree (button says "promise 11-01", no Task in
    Salesforce) → needs_human, so the copy cannot drift silently.
- **Learning:** for every hand-off, design the return trip and the state that survives the day
  before building the forward path (see §4, L-008).
- **Built (2026-10-17):** `cases` table, gate in `rules.build_followups`, handback buttons in
  the owner's DM (date picker for an agreed payment date), digest lists open cases;
  `tests/test_cases.py` (5 mutations, all caught). Not built: the Salesforce cross-check of the
  agreed date, stalled renewal by CloseDate, a guard against daily repeat reminders.
