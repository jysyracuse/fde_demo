# Dev log — multi-model workflow (Opus + Pi)

How the project was built with coding agents: which model did what, how often work was sent back,
and when a fresh context or a different model unblocked a task.

## Daily summary (rolling, newest first)

Written by Claude at the end of each working day from `.audit/decisions.tsv`, LESSONS and
verify results. The user does not maintain this section.
A day runs 06:00 to 05:59 the next morning, Sydney time (late-night work counts to the day it
started); decision-log timestamps are UTC and are converted first.

### 2026-10-16
- **Built:** whole loop runs in Slack: layer 1 rules (sync, match, signals, actions), layer 2
  DeepSeek (reply intent, cited briefs), layer 3 agent (disputes only, read-only MCP server,
  `calculate` code tool), human review (approve / edit / change action / hold, role @mentions,
  simulated inbox DM). Stack moved to uv + SQLite; Docker/Postgres/Mailpit dropped.
- **Results:** verify ALL PASS (23 tests; steps 0, 2-7). Actions 100/100, briefs 58/58 valid,
  disputes 8/8 correct, injected errors caught 316/316 (briefs) and 64/64 (agent), agent share 8/58.
- **Mistakes caught:** L-003 validator narrower than its source, 3 times (dead renewal button;
  56/58 briefs rejected; MCP list parsing); promoted after the second, the rule caught the third. L-004 reminder claimed an
  expired card on a bank-transfer customer. L-005 overdue amount ignored credit notes (user
  spotted AUD 218 vs 138). Model-computed numbers rejected; arithmetic moved to code.
- **Decisions:** Salesforce rename patched in place (keeps IDs); docs, decision log, CLAUDE.md
  and data kept out of git; first commit 428044d (not pushed).
- **Evening:** external review (Pi + GPT-6.1) recorded in `docs/reviews/2026-10-16-external-review.md`;
  13/14 findings hold, counterexamples 3A/3B reproduced (L-007). Nested data sets
  minimal/small/medium/large/full (per-customer RNG, stable IDs; subset test fails without it);
  Salesforce loader incremental by default (grow and shrink verified, `--rebuild` kept). Live set
  = minimal (4 cards). Slack: stale cards mark themselves outdated (was a crash), hand-offs DM the
  role owner, roles moved from env vars to `data/org.db` (`scripts/roles.py`, lookup by email),
  `scripts/clear_slack.py`. Multi-account walkthrough (AR/AM host, Billing Ops, Finance Lead) works.
  25 tests; step 7 needs `small` or larger (fails on minimal by design).
- **Eval:** PLAYBOOK.md + `scripts/eval_actions.py`. Full set (58): rules 57, claude 57,
  jev.choice 55, deepseek 53, jev.checks 52; one label error (CUS-01091). Holdout by Pi
  (40, precedence + missing data + new wording): claude 40, deepseek 33, jev.checks 32,
  rules_pkt 25, jev.choice 19; only DeepSeek routes missing data to a human. Ranking reverses:
  keyword intent and an 8-way Jev choice overfit the generator. Pi hung 2 h on stdin (`</dev/null`).
- **Open:** CLAUDE.md edit awaiting review (now also `--set`, roles.py, clear_slack.py); review
  fixes 3A; Billing Ops / Finance Lead have
  no follow-up buttons; subscription system + counter-cases; VALIDATION.md; "field missing" ->
  needs_human in rules; reply intent by model instead of keywords (holdout finding 2).

## Task log

Summarised from `.audit/decisions.tsv` at the end of each step.

| Date | Step | Task | Model | Attempts | Sent back? Why | Fresh context / model switch helped? |
|---|---|---|---|---|---|---|
| 2026-10-16 | 0 | README problem definition (targets, kill criteria) | Opus | 1 | | |
| 2026-10-16 | 1b | Env scaffolding, SF client (client credentials, read-only guard), smoke tests | Opus | 1 | | |
| 2026-10-16 | 2 | Synthetic data generator + loaders + acceptance check | Opus | 2 | Postgres test DB setup (role/db in one txn) — env issue, not code | |
| 2026-10-16 | 1b | Salesforce connectivity | Opus | 2 | SF_DOMAIN held the username; added fail-fast check | |
| 2026-10-16 | 2 | Salesforce data load (572 records) | Opus | 2 | Task.Type not exposed on Developer Edition; loader drops it | |
| 2026-10-16 | 3,4,7 | v1 loop: sync + match, rules, Slack review (no LLM) | Opus | 2 | first eval 95/100: keyword 'paid by Friday', late threshold 3 | |
| 2026-10-16 | 7 | v1.1: change action, role mentions, DM inbox, evidence links | Opus | 2 | renewal card dead button: validator ignored Opportunity dates (L-003) | |
| 2026-10-16 | 5 | Layer 2 with DeepSeek: classifier, cited briefs, validators, injection test | Opus + DeepSeek | 3 | 56/58 briefs rejected by too-narrow validator; then 7/316 injected counts slipped through (L-003 again) | |
| 2026-10-16 | 6 | Layer 3 agent: read-only MCP server, bounded tool loop, traceability validator | Opus + DeepSeek | 3 | MCP list results split per item broke ID parsing (L-003 again); model computed counts (10 extra users, 11 invoices): tool returns extra_users, one repair round | |
| 2026-10-16 | eval | Holdout set (40 packets + labels) from PLAYBOOK.md, tool-less | Pi (GPT-6.1-sol) | 2 | run 1 produced nothing in 2 h: `pi -p` waited for stdin EOF; then 4 batches in ~2 min, 0 check problems, 0 labels overturned | different model wrote the test set: exposed rules/Jev overfit to Opus's generator |
| 2026-10-16 | — | Agent workflow: CLAUDE.md, rules, settings + hook, LESSONS, verify.sh, accept scripts, skills; uv | Opus | 1 | | |

## Retry protocol

Retry with a clean context containing only: the spec, the failing result, and the review notes.
Never carry the failed conversation forward. Record each retry above.

## Per-model summary (fill at step 8)

| Model | Task types | Tasks | Send-backs | Notes |
|---|---|---|---|---|
| Opus | | | | |
| Pi | | | | |
