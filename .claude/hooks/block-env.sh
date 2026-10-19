#!/usr/bin/env bash
# PreToolUse(Bash): block shell commands that reference .env (secrets).
# Permission rules cover the Read tool only; `cat .env` would bypass them. .env.example is allowed.
# shortcut: text match on the command; obfuscated paths (e.g. ".e""nv") get through. Guardrail, not a sandbox.
exec python3 -c '
import json, re, sys
cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
if re.search(r"(?<![\w.])\.env(?!\.example)(?![\w-])", cmd):
    print("Blocked: command references .env (secrets). Read config through arfollow.config "
          "and print only presence or prefix of values.", file=sys.stderr)
    sys.exit(2)
'
