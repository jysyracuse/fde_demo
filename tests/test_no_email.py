"""Iron rule "never send email": the codebase contains no email-sending client.

"Send" means an outbox row in SQLite plus a copy in the Slack review thread.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMAIL_LIBS = re.compile(
    r"^\s*(import|from)\s+(smtplib|aiosmtplib|yagmail|sendgrid|postmarker|resend|mailgun)\b", re.M)


def test_no_email_client_imports():
    hits = [f"{p.relative_to(ROOT)}"
            for d in ("arfollow", "scripts", "tests")
            for p in (ROOT / d).rglob("*.py")
            if EMAIL_LIBS.search(p.read_text())]
    assert not hits, (f"email client imported in {hits}. Write an outbox row and post it to the "
                      "Slack thread instead; the system never sends email.")
