# External review 2026-10-16 (Pi + GPT-6.1)

Reviewer brief: assess the current implementation, verify issues, propose minimal fixes; no new
frameworks or large refactors. Verified by Claude against code and data on 2026-10-16.

## Findings and verdicts

| # | Finding | Verdict | Evidence |
|---|---|---|---|
| 1 | Roles: reviewer gets every record instead of "what was checked, what you decide" | partly holds | `slack_app.py` ROLE routes owners; card shows all evidence, no checked/decide split |
| 2 | Seat increase ≠ over-billing; contract seats not modelled | holds (most serious) | `mcp_server.compare_seats` compares to historical usual count; no subscription data; every seat jump in the synthetic data is an error, so 8/8 is untested against legitimate expansion |
| 3A | Cross-record fact borrowing passes the agent validator | holds, reproduced | `agent.validate_investigation`: numbers checked against the whole transcript, not the cited record. "INV-A paid AUD 900.00 [INV-A]" (900 belongs to INV-B) → no problems |
| 3B | True quote, opposite label passes the reply validator | holds, reproduced | `llm_steps.validate_reply`: "I have not paid" labelled `paid` → no problems |
| 3C | "validated" on cards overstates what is proven | holds | `slack_app.py` brief and investigation headers |
| 4.1 | Failed charge on a not-yet-overdue invoice is missed | logic holds; 0 cases in data | `rules.build_followups` iterates overdue customers only |
| 4.2 | Old reply intents keep routing after resolution | holds | `rules.py` `intents = {all replies}` |
| 4.3 | promise_date extracted, unused | holds | no use in `rules.py` |
| 4.4 | Follow-up ID is per day: no case identity, Hold carry-over or duplicate-reminder guard | holds | `"FU-{today}-{cid}"` |
| 4.5 | Approval not atomic, no authorization, no proposal version check | holds; plus a concrete bug: approving a stale card approves the current DB draft (e.g. after Change action), which the approver never saw | `review.decide` check-then-update; uses `f["draft_body"]` |
| 4.6 | DB success + Slack failure: no pending/retry state | holds | outbox CHECK allows only `sent_simulated` |
| 4.7 | MCP read-only but not scoped to the case's customer | holds | tools accept any customer_id / invoice_id |
| 4.8 | Read-only is a client guard; the SF integration user can write | holds | runtime and loader share the same credentials |
| 5 | No independent eval; LLM increment unproven (keyword = LLM = 100 %) | holds | thresholds tuned on the labelled set |

## Decisions

Demo must fix: 3A + card wording, 2 (subscription + finance evidence, counter-cases), 4.5
(stale draft, atomic, authorization), 4.7, 4.2, 4.1, 3B as eval-covered test, 5 (holdout,
three-arm comparison, VALIDATION.md).
Production later (known limitations): 4.4 full case lifecycle (demo: duplicate-reminder guard
only), 4.3 promise follow-up timer, 4.6 delivery retry, 4.8 separate read-only SF integration user.
Kept: rules first, layered LLM/agent, read-only tools, integer cents, human approval, audit, lessons.
Not adopted now: Temporal, durable runtimes, multi-agent, a heavier database.

## User direction (2026-10-16)

1. Test data = three systems: Salesforce (CRM), a fake **subscription** system (contracts,
   seats, orders, changes), and the fake **finance** system as DB tables (invoices, payments,
   charge attempts, credit notes).
2. Record this review (this file).
3. Per case the agent decides: query subscription, query finance, or stop because the evidence
   already suffices and hand to the account manager.
4. Post-steps go to named roles; the user registers extra Slack accounts for them.
