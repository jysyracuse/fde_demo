"""Delete every message this bot posted in SLACK_CHANNEL_ID (cards, digests, thread replies).

Used with a data reset: old cards point at follow-ups that no longer exist. Deletes only the
bot's own messages, and keeps the digests and cards of the current app.db (and their threads);
messages people wrote stay. Irreversible: run only when the user asks. Private channel: the bot
needs the groups:history scope.

Usage: uv run python scripts/clear_slack.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arfollow import config, store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    args = ap.parse_args()
    client = WebClient(token=config.require("SLACK_BOT_TOKEN"))
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    channel, bot = config.require("SLACK_CHANNEL_ID"), client.auth_test()["bot_id"]
    current = {ts for row in store.app().execute(
        "SELECT slack_ts, slack_thread FROM followups") for ts in row if ts}
    mine, cursor = [], None
    while True:
        page = client.conversations_history(channel=channel, limit=200, cursor=cursor)
        for m in page["messages"]:
            if m["ts"] in current:
                continue
            if m.get("reply_count"):
                replies = client.conversations_replies(channel=channel, ts=m["ts"], limit=200)
                mine += [r["ts"] for r in replies["messages"][1:] if r.get("bot_id") == bot]
            if m.get("bot_id") == bot:
                mine.append(m["ts"])
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    print(f"Old bot messages in channel: {len(mine)} (current cards kept)")
    if args.dry_run:
        return
    for ts in mine:
        client.chat_delete(channel=channel, ts=ts)
    print(f"Deleted {len(mine)}")


if __name__ == "__main__":
    main()
