"""Step 2 acceptance.

HANDOFF: 脚本生成约 100 个客户，覆盖六类案例，写入 Salesforce、Postgres 和收件箱 JSON，含故意的不一致。
验收：可重复运行、不超 5 MB、每类至少 5 个

Checks: two runs are byte-identical; scripts/check_data.py (size, >= 5 per type,
inconsistencies) passes; the SQLite load (simulated NetSuite) keeps every row and every cent.
The Salesforce load is not re-run here: verify.sh stays offline and never writes external systems.
"""
import filecmp
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import check_data  # noqa: E402
import load_db  # noqa: E402


def check_db(data_dir: Path) -> int:
    billing = json.loads((data_dir / "billing.json").read_text())
    db = data_dir / "billing.db"
    counts = load_db.load(data_dir, db)
    conn = sqlite3.connect(db)
    lost = {t: n for t, n in counts.items() if n != len(billing[t])}
    want = sum(round(i["amount"] * 100) for i in billing["invoices"])
    got = conn.execute("SELECT sum(amount_cents) FROM invoices").fetchone()[0]
    conn.close()
    if lost or want != got:
        print(f"FAIL: SQLite load lost rows {lost} or cents ({got} != {want}). "
              "Load money as round(x * 100) into *_cents INTEGER columns.")
        return 1
    print(f"SQLite load OK: {sum(counts.values())} rows, invoice total {got} cents")
    return 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "a"), Path(tmp, "b")
        for out in (a, b):
            subprocess.run([sys.executable, str(SCRIPTS / "generate_data.py"), "--out", str(out)],
                           check=True, capture_output=True)
        names = sorted(p.name for p in a.iterdir())
        _, diff, errs = filecmp.cmpfiles(a, b, names, shallow=False)
        if diff or errs:
            print(f"FAIL: generator not deterministic, differing files: {diff + errs}. "
                  "Draw all randomness from self.rng and keep AS_OF fixed.")
            return 1
        check_data.DATA_DIR = a
        rc = check_data.main()
        rc = rc or check_db(a)
    print("step 2 PASS" if rc == 0 else "step 2 FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
