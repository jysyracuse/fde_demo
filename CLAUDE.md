# AR Follow-up Agent (demo)

Goal: a layered, auditable AR follow-up system (fixed workflow → LLM steps → agent → human review)
over Salesforce + simulated billing + Slack, built as a portfolio piece. Kickoff spec: `docs/HANDOFF.md`.

## Commands

```bash
uv sync                                   # install from uv.lock; add deps with `uv add [--dev]`
scripts/verify.sh                         # lint + tests + implemented acceptance scripts = DONE
uv run python scripts/accept/step_N.py    # one step's acceptance check (exit 0 = pass)
uv run python scripts/generate_data.py    # deterministic synthetic data -> data/out/
uv run python scripts/check_data.py       # step 2 data check
uv run python scripts/smoke_salesforce.py # read-only SF connectivity check
uv run python scripts/load_db.py          # billing.json -> data/out/billing.db (SQLite)
uv run python -m arfollow.run [--offline] [--llm] [--post]  # sync, match, rules; --llm = layer 2
uv run python -m arfollow.slack_app       # button listener (Socket Mode); keep running
```

## Layout

```
arfollow/          config, salesforce (read-only client), store (SQLite), sync (snapshot +
                   entity match), rules (layer 1), llm + llm_steps (layer 2, validated),
                   review (decisions), slack_app, run
scripts/           generators, loaders, smoke tests
scripts/accept/    step_N.py acceptance checks (stubs carry the HANDOFF criterion)
tests/             pytest
db/billing.sql     simulated NetSuite schema (SQLite, money in INTEGER cents)
db/app.sql         system store: snapshot, matches, follow-ups, append-only decisions/outbox/audit
docs/agent/        STATE.md (handoff between sessions), LESSONS.md (mistake log)
.audit/            decisions.tsv (append-only decision log, committed)
.claude/rules/     path-scoped rules; load automatically for matching files
```

## Safety boundaries

- Salesforce is read-only. Only `scripts/load_salesforce.py` writes, and only after the user approves.
- Never send email. "Send" means an outbox row in SQLite plus a copy posted in the Slack review
  thread. The whole loop (digest, review, approve, "sent", reply) runs in Slack.
- Amounts and dates in any output must equal the source record; otherwise reject and route to a human.
- Every citation is a record ID that exists in the source data.
- Secrets stay out of git, logs and tool output. Print presence or prefix, never the value.

## Autonomy

- Reversible actions (edit code, edit docs, local commit): do them, then report.
- Irreversible or outward actions (write Salesforce, `git push`, post to Slack, send any message,
  delete data): ask first, every time. Approval for one action does not cover the next.
- Standing approval (user, 2026-10-16): post to Slack `#ar-followup` (`SLACK_CHANNEL_ID`) any time
  during development, including DMs to the approving reviewer (simulated customer inbox).
  Every other channel or workspace still needs asking.
- Done = `scripts/verify.sh` passes. Report failures with the output.

## Session protocol

- Start: read `docs/agent/STATE.md`. Resolve any open question in `docs/HANDOFF.md` that the
  next step depends on before starting it; record the answer in `.audit/decisions.tsv`.
- During: append forks, verification results and rollbacks to `.audit/decisions.tsv`
  (evidence = pointer: commit, `file:line`, command; never prose).
- End: update `docs/agent/STATE.md`. At the end of a step, summarise `.audit/decisions.tsv`
  into the task table in `docs/DEVLOG.md`.

## Rule table

Iron rules = the safety boundaries above + rules promoted from `docs/agent/LESSONS.md`.
Change this table, `.claude/rules/`, `.claude/settings.json` or hooks only with user approval.
New rules come only from a lesson seen at least twice (`/reflect`, then `/promote`).

| Rule | Enforced by | Lesson |
|---|---|---|
| Salesforce read-only at runtime | `SalesforceClient` `allow_write` guard + `tests/test_salesforce_guard.py`; settings `ask` on `load_salesforce.py` | boundary |
| Never send email | architecture: no email client; `tests/test_no_email.py` fails on any email-library import | boundary |
| Amounts/dates match source | drafts built from billing cents; `review.check_numbers` rejects edits and LLM briefs; injection test 100 % (step_5, step_7) | boundary |
| Citations exist in source | `review.unresolved` for decisions (step_7); brief cites must be input records (`llm_steps.validate_brief`, step_5) | boundary |
| No secrets in git/logs | `.gitignore`; settings `deny Read(./.env)`; hook `.claude/hooks/block-env.sh` (Bash); logs: text-only | boundary |
| No force push | settings `deny` on `git push --force/-f` | boundary |
| Ask before irreversible/outward actions | text-only (except `load_salesforce.py` ask rule) | boundary |
| Validators proven both ways: every real output passes, every injected error fails | check scripts in `verify.sh`: step_5 (real briefs all valid + injection 100 %), step_7 (approve every action's own draft) | L-003 |

## Index

- `docs/HANDOFF.md` — kickoff spec, frozen (read-only). Later decisions live in
  `.audit/decisions.tsv`, README and STATE and override it where they differ
- `docs/agent/STATE.md` — current progress and next step
- `docs/agent/LESSONS.md` — mistake log and promotion process
- `docs/DEVLOG.md` — model-workflow log (interview material)
- `docs/SETUP.md` — manual Salesforce / Slack setup
- `docs/ONTOLOGY.md` — objects, links, roles, actions, data sources, detection rules
