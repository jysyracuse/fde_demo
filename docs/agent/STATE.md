# State

Update before ending every session. The next session starts here.

## Progress (2026-10-16)

- Step 0 done (README targets, baseline, kill criteria).
- Step 1b done: `smoke_salesforce.py` and `smoke_slack.py` pass (private channel `#ar-followup`).
  ANTHROPIC_API_KEY not set yet.
- Step 2 done: data generated, check passes, loaded to Salesforce (100 Account, 208 Contact,
  44 Opportunity, 26 Case, 194 Task). Billing data in SQLite (`data/out/billing.db`).
- Agent workflow set up: CLAUDE.md, rules, settings + hook, LESSONS, verify.sh, accept scripts,
  `/promote` (`/reflect` SKILL.md not yet written: blocked by permission; user to add). Project uses uv. No Docker: Postgres replaced by SQLite, Mailpit dropped,
  the loop runs entirely in Slack. Ontology design in `docs/ONTOLOGY.md`. (Decided 2026-10-16;
  HANDOFF is the frozen kickoff spec and still says Postgres/Mailpit.)

- v1 loop (no LLM) done: `arfollow.run` (sync, match, rules, digest) + `arfollow.slack_app`
  (approve / edit / pause). Steps 3, 4 and 7 acceptance pass. Digest posted 2026-10-16.

- v1.1: Change action (override + redraft), role @mentions (env SLACK_USER_*, default approver),
  simulated customer inbox via DM to the approver, evidence with labels and SF links.
- Step 5 done with DeepSeek (`arfollow/llm.py`, `llm_steps.py`): reply intent 42/42, promise
  dates 12/12, briefs 58/58 valid, injected errors caught 316/316. Email drafts still templates.
- Step 6 done: `arfollow/mcp_server.py` (9 read-only tools, SQLite mode=ro) + `arfollow/agent.py`
  (DeepSeek tool calling, MAX_STEPS 8, MAX_TOKENS 60k, one repair round). Disputes 8/8 correct,
  all valid, injected errors 63/63. Investigation shown on dispute cards. `run --agent`.

## In progress

- User to update the Slack app manifest (App Home messages tab) and click Send to test the DM.
- L-003 promoted 2026-10-16 (Rule table: validators proven both ways).

## Next

1. Proposed CLAUDE.md edit awaiting approval: `--agent` in Commands, agent/mcp_server in Layout.
2. Step 6 done (agent, disputes only; arithmetic via `calculate` code tool). L-005 fixed (net of credit notes). Measure the README kill criterion: agent vs brief alone.
3. Harder eval set: the synthetic replies are templated, so keyword and LLM both score 100 %.
