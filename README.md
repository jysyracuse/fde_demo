# AR Follow-up Agent (demo)

A layered system that helps a finance / customer-success team at a fictional SaaS company
follow up on failed subscription charges and overdue invoices. Salesforce (read-only) +
a simulated billing system (SQLite) + Slack for human review. All data is synthetic.

> Fictional company: **notacompany**.

## 0. Problem definition

### Where the time actually goes

Finding the overdue list is a SQL query. The cost is in **handling each case**:

| Step per case | Manual today (assumed) |
|---|---|
| Gather context across CRM, billing, inbox | 6 min |
| Decide what to do (remind / pause / escalate / hand off) | 3 min |
| Write the follow-up or internal note | 4 min |
| Read and act on the customer's reply | 2 min |
| **Total** | **15 min / case** |

Assumed volume: **40 open cases / day** → **10 person-hours / day**.
These are assumptions to be replaced by a measured baseline (shadow the team on 10 real cases)
before any real rollout.

### Outcome targets (measured on the labelled set in `VALIDATION.md`)

| Metric | Target |
|---|---|
| Human time per case (review + approve) | **≤ 4 min** median (from 15) |
| Recommended action matches label | **≥ 90 %** overall, **100 %** for dispute and renewal cases (wrong action there damages the relationship) |
| Reply classification accuracy | **≥ 90 %** |
| Injected wrong amounts / dates that reach a human as "valid" | **0** (100 % interception) |
| Citations that exist and support the claim | **≥ 98 %** |
| Cases that reach the agent layer | **≤ 20 %** |
| Model cost per case | **≤ US$0.10** average |

### Kill criteria (stop or downgrade the layer)

- Reply classification stays **< 85 %** after two prompt/schema iterations → drop the LLM classifier,
  route all replies to a human with only the rule-based routing.
- Agent recommendations on complex cases are **not ≥ 10 pp better** than the layer-2 brief alone →
  remove the agent layer; complex cases go to a human with the brief.
- Any injected numeric error passes the validator **once** in the eval → no drafted emails ship
  until fixed; the system runs in brief-only mode.
- Average cost **> US$0.50 / case**, or median human time **> 8 min / case** (< ~50 % saving) →
  not worth operating; write up why.

## Architecture (summary)

| Layer | What | Tech |
|---|---|---|
| 1. Fixed workflow | sync, find overdue / failed charges, rank, rule exclusions, daily Slack digest | cron + SQL + rules, **no LLM** |
| 2. LLM steps | cited context brief, reply classification, email draft | LLM + structured output + validators |
| 3. Agent | investigate disputed / complex cases | agent + narrow read-only MCP server, step and budget caps |
| Human review | approve / edit / pause, everything audited | Slack buttons (Socket Mode) + SQLite |

Safety boundaries: Salesforce is read-only; the system writes only to its own SQLite database; it never
sends email ("send" = outbox row + copy in the Slack thread; the whole loop runs in Slack); amounts and dates must match source data or the output is
rejected; every claim cites a record ID that the validator checks.

## Repo layout

```
db/billing.sql             billing system schema (simulated NetSuite)
arfollow/                  library code (config, Salesforce client, db)
scripts/smoke_*.py         step 1b smoke tests
scripts/generate_data.py   step 2: deterministic synthetic data -> data/out/
scripts/load_db.py         load billing data into SQLite (data/out/billing.db)
scripts/load_salesforce.py load CRM data (idempotent: deletes previous demo records)
scripts/check_data.py      step 2 data check
scripts/accept/step_N.py   per-step acceptance (exit 0 = pass)
scripts/verify.sh          lint + tests + acceptance = definition of done
docs/agent/                agent state and lessons log
docs/SETUP.md              manual setup (Salesforce, Slack)
docs/ONTOLOGY.md           objects, links, roles, actions, detection
docs/DEVLOG.md             model-workflow log (Opus + Pi)
```

## Quick start

```bash
cp .env.example .env            # fill in after docs/SETUP.md
uv sync                         # creates .venv from uv.lock
uv run python scripts/smoke_salesforce.py && uv run python scripts/smoke_slack.py
uv run python scripts/generate_data.py
uv run python scripts/check_data.py
uv run python scripts/load_db.py
uv run python scripts/load_salesforce.py
uv run python scripts/load_db.py
uv run python -m arfollow.slack_app &   # review buttons
uv run python -m arfollow.run --llm --post    # sync + rules + layer 2 + Slack digest
```

## Progress

- [x] 0. Problem definition
- [x] 1a. Salesforce Developer Edition
- [x] 1b. Environment — Salesforce + Slack verified
- [x] 2. Synthetic data — generated and loaded to Salesforce and SQLite
- [x] 3. Ontology & sync — 100/100 customers linked, 0 wrong
- [x] 4. Layer 1 workflow — rules, ranking, Slack digest (v1, no LLM)
- [x] 5. Layer 2 LLM steps — reply classifier + cited briefs (DeepSeek), validators catch 316/316 injected errors; drafts still templates
- [x] 6. Layer 3 agent — disputes only (8/58 = 14 %), read-only MCP server, 8/8 correct and traceable, 63/63 injected errors caught; agent-vs-brief comparison (kill criterion) not measured yet
- [~] 7. Human review & audit — approve / edit / change action / hold, role hand-offs, append-only audit
- [ ] 8. Evaluation & docs
