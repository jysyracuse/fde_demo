"""Daily run: sync -> match -> link replies -> build follow-ups -> (optional) Slack digest.

Usage: uv run python -m arfollow.run [--offline] [--llm] [--agent] [--post] [--limit N]
"""
import argparse
from collections import Counter

from . import config, rules, store, sync


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="reuse the last Salesforce snapshot")
    ap.add_argument("--llm", action="store_true", help="layer 2: classify replies, write briefs")
    ap.add_argument("--agent", action="store_true", help="layer 3: investigate disputes")
    ap.add_argument("--post", action="store_true", help="post the digest to SLACK_CHANNEL_ID")
    # shortcut: cards for the top N only; the rest stay open in app.db. Page through for real use.
    ap.add_argument("--limit", type=int, default=10, help="follow-up cards to post (default 10)")
    args = ap.parse_args()

    conn, billing = store.app(), store.billing()
    if not args.offline:
        from .salesforce import SalesforceClient
        print("Salesforce snapshot:", sync.sync_salesforce(conn, SalesforceClient()))
    print("Entity matching:", sync.match_customers(conn, billing))
    print("Inbox:", rules.link_messages(conn, billing))
    if args.llm:
        from . import llm_steps
        print("LLM reply labels:", llm_steps.classify_replies(conn))
        rules.link_messages(conn, billing)  # re-link so rules use the validated LLM intents
    followups = rules.build_followups(conn, billing)
    rules.save_followups(conn, followups)
    if args.llm:
        print("LLM briefs:", llm_steps.write_briefs(conn, billing))
    if args.agent:
        from . import agent
        print("Agent investigations:", agent.run_disputes(conn))
    print(f"Follow-ups as of {rules.as_of(billing)}: {len(followups)}")
    for action, n in Counter(f["action"] for f in followups).most_common():
        print(f"  {action:28s} {n}")
    if args.post:
        from slack_sdk import WebClient

        from .slack_app import post_digest
        client = WebClient(token=config.require("SLACK_BOT_TOKEN"))
        n = post_digest(conn, billing, client, config.require("SLACK_CHANNEL_ID"), args.limit)
        print(f"Posted digest with {n} cards" if n else "Nothing new to post")


if __name__ == "__main__":
    main()
