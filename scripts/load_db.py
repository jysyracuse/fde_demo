"""Load data/out/billing.json into SQLite (data/out/billing.db). Rebuilds the file each run.

Money fields become INTEGER cents (amount -> amount_cents) so later checks compare exactly.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arfollow import config  # noqa: E402

TABLES = ["customers", "invoices", "invoice_lines", "payments", "charge_attempts",
          "credit_notes", "usage_monthly"]
MONEY = {"amount", "mrr", "unit_price"}


def load(data_dir: Path, db_path: Path) -> dict[str, int]:
    data = json.loads((data_dir / "billing.json").read_text())
    db_path.unlink(missing_ok=True)
    counts = {}
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript((config.ROOT / "db" / "billing.sql").read_text())
        for table in TABLES:
            rows = data[table]
            counts[table] = len(rows)
            if not rows:  # e.g. --demo has no credit notes
                continue
            cols = [f"{c}_cents" if c in MONEY else c for c in rows[0]]
            conn.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [[round(v * 100) if k in MONEY else v for k, v in r.items()] for r in rows])
        conn.execute("INSERT INTO meta VALUES ('as_of', ?)", (data["as_of"],))
    conn.close()
    return counts


def main() -> None:
    for table, n in load(config.DATA_DIR, config.BILLING_DB).items():
        print(f"{table:16s} {n:5d} rows")
    print(f"Wrote {config.BILLING_DB}")


if __name__ == "__main__":
    main()
