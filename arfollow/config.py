"""Environment-backed settings. Fails loudly when a required value is missing."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data" / "out"
BILLING_DB = DATA_DIR / "billing.db"  # source data; runtime opens it read-only
APP_DB = ROOT / "data" / "app.db"       # the system's own store: snapshot, follow-ups, decisions
ORG_DB = ROOT / "data" / "org.db"       # who holds which role; survives demo data resets

COMPANY = "notacompany"
AR_EMAIL = "billing@notacompany.example"


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.endswith("-"):  # catches the xoxb-/xapp- placeholders
        raise SystemExit(f"Missing {name} in .env (see docs/SETUP.md)")
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default
