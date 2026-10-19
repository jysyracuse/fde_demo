"""Role directory (data/org.db): who takes over each internal action in Slack.

  uv run python scripts/roles.py                                  list roles
  uv run python scripts/roles.py billing_ops_manager a@x.example  set: Slack user by email
  uv run python scripts/roles.py ar_specialist U0123              set by Slack user ID
  uv run python scripts/roles.py billing_ops_manager --clear      unset: hand-offs go to approver

Roles: ar_specialist (AR Specialist), billing_ops_manager (Billing Ops Manager),
account_manager (Account Manager).

ar_specialist is the only user who may decide follow-ups; unset = no one may.

Needs the bot scopes users:read and users:read.email to look up by email.
"""
import argparse
import sys
from pathlib import Path

from slack_sdk import WebClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arfollow import config, store  # noqa: E402

ROLES = ["ar_specialist", "billing_ops_manager", "account_manager"]  # ONTOLOGY.md §5


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("role", nargs="?", choices=ROLES)
    ap.add_argument("email", nargs="?")
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    conn = store.org()
    if args.role and args.clear:
        conn.execute("DELETE FROM roles WHERE role = ?", (args.role,))
    elif args.role:
        if not args.email:
            ap.error("give the person's Slack email or user ID, or --clear")
        client = WebClient(token=config.require("SLACK_BOT_TOKEN"))
        user = (client.users_info(user=args.email) if "@" not in args.email
                else client.users_lookupByEmail(email=args.email))["user"]
        email = args.email if "@" in args.email else user.get("profile", {}).get("email")
        conn.execute("INSERT INTO roles VALUES (?, ?, ?, ?) ON CONFLICT (role) DO UPDATE SET "
                     "slack_user = excluded.slack_user, email = excluded.email, "
                     "updated_at = excluded.updated_at",
                     (args.role, user["id"], email, store.now()))
    conn.commit()
    held = {r["role"]: r for r in conn.execute("SELECT * FROM roles")}
    for role in ROLES:
        r = held.get(role)
        print(f"{role:20s} " + (f"{r['slack_user']}  {r['email']}" if r
                                else "(unset: no one may decide)" if role == "ar_specialist"
                                else "(unset: the approving reviewer)"))


if __name__ == "__main__":
    main()
