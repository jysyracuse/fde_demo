# Lessons (append-only)

Real mistakes and user corrections. Append new entries; edit only `Count`, `Status`, `Rule`
and append to `Evidence`. One-off accidents are not lessons.

Status: `observed` (1×) → `recurring` (2×+, propose promotion) → `promoted` (rule in force)
→ `retired` (made structurally impossible; text rule deleted).

Promotion (via `/promote`, user approves before any change to CLAUDE.md, rules, settings, hooks):
1. Pick the highest enforcement that works: architecture makes the mistake impossible >
   type / Pydantic validation > check script (error message states the correct way) > test >
   `.claude/settings.json` permission or hook > text rule. Text only for judgement calls.
2. Reproduce the original mistake and watch the new check fail it.
3. Register the rule in the CLAUDE.md Rule table with this lesson's ID.

---

## L-001 CREATE ROLE and CREATE DATABASE in one transaction
- Date: 2026-10-16
- Symptom: local Postgres test DB setup failed during step 2.
- Root cause: Postgres runs `CREATE DATABASE` only outside a transaction block; the setup sent
  it in the same transaction as `CREATE ROLE` (psycopg opens a transaction by default).
- Evidence: `docs/DEVLOG.md:12` (step 2, attempt 2); commit 355bb5a
- Count: 1
- Status: retired (2026-10-16: Postgres replaced by SQLite; no role/database setup exists)
- Rule: —

## L-002 Started work without resolving a HANDOFF open question
- Date: 2026-10-16
- Symptom: steps 0–2 were built in Python while HANDOFF still lists "Python or TypeScript" as open.
- Root cause: the "(default Python)" note was treated as a decision; nothing required asking
  before starting.
- Evidence: `docs/HANDOFF.md:125`; commit 355bb5a
- Count: 1
- Status: observed
- Rule: — (text reminder in CLAUDE.md Session protocol; not a promoted rule)

## L-003 Validator stricter than the generator it checks; acceptance covered a subset
- Date: 2026-10-16
- Symptom: in Slack, Approve and Pause on renewal card CUS-01041 did nothing. The system's own
  note cites the Opportunity CloseDate 2026-11-07; `check_numbers` accepted invoice dates only
  and raised; the handler posted no error, so the reviewer saw a dead button.
- Root cause: (1) the validator's allowed set (invoice fields) was narrower than what the drafts
  cite (any evidence record); (2) step_7 exercised 3 of 7 actions, so the gap passed acceptance;
  (3) rejections were not surfaced to the user; (4) pause ran a check that only matters for output.
- Evidence: `.audit/decisions.tsv` row 2026-10-16T12:00Z; `arfollow/review.py:30`;
  `scripts/accept/step_7.py` (now approves every action: failed before fix, passes after)
- Recurrence 2026-10-16 (step 5): `check_numbers` again narrower than its sources. 56/58 LLM
  briefs rejected: inbox records not loaded, dates matched inside IDs (`FU-2026-10-15-…`),
  credit notes lack currency. Opposite gap too: textual dates ("15 October 2026") were never
  checked, and stray numbers were matched as substrings (7/316 injected counts passed).
  Evidence: `.audit/decisions.tsv` rows 2026-10-16T13:30Z; `arfollow/review.py` `check_numbers`;
  `scripts/accept/step_5.py` (injection 309/316 before, 316/316 after; real briefs 58/58 valid)
- Recurrence 2026-10-16 (step 6): agent validator parsed MCP tool output as one JSON document,
  but MCP returns a list as one content block per item; IDs from list tools were missed and 5/8
  real answers rejected. Caught by the promoted rule (real answers must pass) before step_6
  was written. Evidence: `arfollow/agent.py` (rejoin as JSON array); agent_runs rows before fix.
- Count: 3
- Status: promoted (user approved 2026-10-16)
- Rule: CLAUDE.md Rule table, "Validators proven both ways"

## L-004 Reminder template stated a fact without checking its signal
- Date: 2026-10-16
- Symptom: reviewer overrode CUS-01041 (bank transfer, renewal) to gentle_reminder and approved;
  the simulated email told the customer their card had expired. No card exists.
- Root cause: `rules.draft` wrote the expired-card text for every gentle_reminder; notes had a
  fallback for overrides without their signal, reminders did not. `check_numbers` checks
  amounts and dates only, not claims.
- Evidence: outbox id 1; `arfollow/rules.py` `draft`; `tests/test_review.py`
  `test_gentle_reminder_claims_expired_card_only_with_that_signal` (failed before fix)
- Count: 1
- Status: observed
- Rule: —

## L-005 Overdue amount ignored credit notes
- Date: 2026-10-16
- Symptom: CUS-01019 card showed "AUD 218.00 overdue"; credit note CN-0018 (AUD 80.00) already
  reduced INV-105085, so the customer owes AUD 138.00. Same for CUS-01027 (half credit).
  Reported by the user while asking what the case meant.
- Root cause: `rules.build_followups` summed gross invoice amounts; credit notes were only
  attached as evidence. The generator's `labels.json` `amount_overdue` uses the same gross
  definition, so no acceptance check could see the gap. Net balances were also absent from
  `check_numbers`, so a correct net figure would have been rejected.
- Evidence: followups.amount_cents 21800 for CUS-01019; `arfollow/rules.py` `build_followups`.
  Fix: balances net of credit notes in rules, drafts and `check_numbers`; `scripts/accept/step_4.py`
  recomputes every amount in independent SQL (failed on CUS-01019/01027 before, passes after);
  agent arithmetic via `agent.calculate` (code tool, operands must be retrieved values).
- Count: 1
- Status: observed
- Rule: —

## L-006 Decision-log timestamps hand-written instead of read from the clock
- Date: 2026-10-16
- Symptom: `.audit/decisions.tsv` rows on 2026-10-16 carry times up to 18:45Z while the real
  clock was 04:20Z; found when bucketing by the user's 06:00 Sydney day boundary.
- Root cause: ts typed by hand in printf strings instead of `$(date -u +%Y-%m-%dT%H:%MZ)`.
- Evidence: correction row in `.audit/decisions.tsv` stamped 2026-10-16T04:20Z
- Count: 1
- Status: observed
- Rule: —

## L-007 Claimed guarantees beyond what the checks prove
- Date: 2026-10-16
- Symptom: cards say "validated" and reports said "traceable" / "8/8 correct", but the agent
  validator accepts numbers borrowed from an uncited record (3A) and the reply validator accepts
  a true quote with the opposite label (3B); the seat check cannot tell over-billing from a
  legitimate expansion, and the data had no such case. Found by external review.
- Root cause: checks were named by intent ("validated") rather than by the property they test;
  acceptance data lacked counter-cases for the business claim.
- Evidence: `docs/reviews/2026-10-16-external-review.md` rows 2, 3A, 3B, 3C
- Count: 1
- Status: observed
- Rule: —

## L-008 Roles and handoffs designed after the workflow was built
- Date: 2026-10-17
- Symptom: actions routed to four roles (Billing Ops, Finance Lead, AE, Retention) chosen ad hoc;
  hand-offs had no return path, "Finance Lead" is not a real title, and the Slack accounts were
  renamed mid-demo (now Billing Ops Manager and Account Manager), forcing a role remap.
- Root cause: the responsibility matrix (who owns each action, who is customer-facing, how a
  case returns) was not fixed before building routing; related to L-002 (unresolved spec).
- Evidence: `docs/ONTOLOGY.md` §5-6 (rewritten 2026-10-17); `arfollow/slack_app.py` ROLE; `docs/LEARNINGS.md` §4
- Count: 1
- Status: observed
- Rule: —

## L-009 Eval set written by the author of the rules it tests
- Date: 2026-10-17
- Symptom: rules scored 57/58 on the generated set and 25/40 on a holdout written by another
  model; jev.choice 55/58 → 19/40. The first result was reported as evidence that rules suffice.
- Root cause: the generator and the rules were designed together, so every signal had the form
  the rules expect; no case came from outside that assumption.
- Evidence: `docs/eval/2026-10-16-action-eval.md` (Holdout); `docs/LEARNINGS.md` §2
- Count: 1
- Status: observed
- Rule: —
