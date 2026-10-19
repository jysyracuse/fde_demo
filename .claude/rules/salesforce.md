---
paths:
  - "arfollow/salesforce.py"
  - "scripts/*salesforce*.py"
---
# Salesforce

- Runtime code constructs `SalesforceClient()` with no arguments (read-only).
  `allow_write=True` appears only in `scripts/load_salesforce.py`.
- Run `load_salesforce.py` only after the user approves (it deletes and rewrites org data).
  Use `--dry-run` first to show counts.
- Scope demo queries and deletes by the marker `Account.Site = 'ardemo'`.
- `data/sf_id_map.json` is for evaluation only; runtime links records through entity matching.
- `SF_DOMAIN` is the My Domain host; client credentials fails on login/test.salesforce.com.
- Developer Edition: 5 MB data storage; `Task.Type` is not exposed to the API.
- Keep `tests/test_salesforce_guard.py` passing when touching the client.
