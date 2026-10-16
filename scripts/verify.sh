#!/usr/bin/env bash
# Definition of done: every command passes. Add a step to ACCEPT once its script is implemented.
set -euo pipefail
cd "$(dirname "$0")/.."
ACCEPT=(0 2 3 4 5 6 7)

uv run ruff check .
uv run pytest -q
for n in "${ACCEPT[@]}"; do uv run python "scripts/accept/step_$n.py"; done
echo "verify: ALL PASS"
