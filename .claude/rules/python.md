---
paths:
  - "**/*.py"
---
# Python

- Run everything through `uv run`. Add dependencies with `uv add` (dev tools: `uv add --dev`).
- Scripts import the package with the existing `sys.path.insert(0, ...)` bootstrap plus
  `# noqa: E402`, as in `scripts/check_data.py`.
- Read settings through `arfollow.config.require` / `optional`. When showing config, print
  presence or a short prefix only.
- Line length 100 (ruff config in `pyproject.toml`).
- Compare money in integer cents (`round(x * 100)`); pass dates as ISO strings / `date`.
- New non-trivial logic ships with a test in `tests/`.
