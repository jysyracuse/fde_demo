"""Step 1b acceptance: the bot posts one message to the review channel."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_sdk import WebClient  # noqa: E402

from arfollow import config  # noqa: E402


def main() -> None:
    client = WebClient(token=config.require("SLACK_BOT_TOKEN"))
    auth = client.auth_test()
    print(f"Bot: {auth['user']} in workspace {auth['team']}")
    resp = client.chat_postMessage(
        channel=config.require("SLACK_CHANNEL_ID"),
        text=f":white_check_mark: AR Followup smoke test — {datetime.now():%Y-%m-%d %H:%M}",
    )
    print(f"Posted message ts={resp['ts']}")


if __name__ == "__main__":
    main()
