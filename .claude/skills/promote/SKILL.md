---
name: promote
description: Turn recurring lessons in docs/agent/LESSONS.md into enforced iron rules (asks first).
disable-model-invocation: true
---
# Promote

For each `Status: recurring` entry in `docs/agent/LESSONS.md`:

1. Choose the highest enforcement level that can catch it (ladder in LESSONS.md). Stay at text
   only when the rule needs judgement no check can make.
2. Present the plan and wait for user approval: rule wording, enforcement, files to change.
3. Implement the check. Reproduce the original mistake and show the check failing on it; then
   show `scripts/verify.sh` passing.
4. Register it in the CLAUDE.md Rule table (`Rule | Enforced by | Lesson`). Set the lesson's
   `Status: promoted` and `Rule:` to the table row.
5. If the check makes the mistake structurally impossible, delete any text rule for it and set
   `Status: retired`.
6. Append each step's result to `.audit/decisions.tsv` (phase `promote`).

Done when: every recurring entry is promoted, retired, or declined by the user (recorded in the
entry).
