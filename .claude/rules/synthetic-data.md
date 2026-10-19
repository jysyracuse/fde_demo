---
paths:
  - "scripts/generate_data.py"
  - "scripts/check_data.py"
---
# Synthetic data

- Draw all randomness from `self.rng`; keep `AS_OF` fixed. Same seed + same `AS_OF` must give
  byte-identical output.
- Every extra `rng` call shifts all later data: after a change, regenerate and re-run checks.
- `labels.json` / `inbox_labels.json` are the evaluation ground truth; update them in the same
  change as the data they describe.
- Hold the acceptance bar: >= 5 customers per case type, Salesforce estimate < 80 % of 5 MB,
  every deliberate inconsistency type present.
- Verify with `uv run python scripts/accept/step_2.py`. Reloading Salesforce needs user approval.
