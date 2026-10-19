"""SQLite connections: billing.db (source, opened read-only), app.db (the system's own store),
org.db (role directory)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def billing(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.BILLING_DB
    if not path.exists():
        raise SystemExit(f"{path} missing: run `uv run python scripts/load_db.py`")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def app(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.APP_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((config.ROOT / "db" / "app.sql").read_text())
    return conn


def org(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.ORG_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript((config.ROOT / "db" / "org.sql").read_text())
    return conn


def role_user(conn: sqlite3.Connection, role: str) -> str | None:
    row = conn.execute("SELECT slack_user FROM roles WHERE role = ?", (role,)).fetchone()
    return row[0] if row else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(conn: sqlite3.Connection, actor: str, event: str, followup_id: str | None = None,
          **detail) -> None:
    conn.execute("INSERT INTO audit_log (ts, actor, event, followup_id, detail) VALUES (?,?,?,?,?)",
                 (now(), actor, event, followup_id, json.dumps(detail, default=str)))
