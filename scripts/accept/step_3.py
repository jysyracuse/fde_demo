"""Step 3 acceptance.

HANDOFF: 只读同步 Salesforce 到本地；实体匹配，对不上的列出待人确认；统一案例视图。验收：任一客户能关联出发票、付款、工单、商机、沟通记录

Checks on the local snapshot (run `uv run python -m arfollow.run` first; this script is offline):
every customer is auto-linked or queued for review; no auto-link is wrong (ground truth from
data/sf_id_map.json, evaluation only); every linked customer reaches its invoices, payments and
exactly the cases, opportunities and tasks it has in the source data.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arfollow import config, store  # noqa: E402

OBJECTS = {"cases": "Case", "opportunities": "Opportunity", "tasks": "Task", "contacts": "Contact"}


def main() -> int:
    if not config.APP_DB.exists():
        print("FAIL: no snapshot. Run `uv run python -m arfollow.run` (read-only sync) first.")
        return 1
    conn, billing = store.app(), store.billing()
    labels = json.loads((config.DATA_DIR / "labels.json").read_text())["customers"]
    idmap = json.loads((config.ROOT / "data" / "sf_id_map.json").read_text())
    source = {a["ref"]: a for a in
              json.loads((config.DATA_DIR / "salesforce.json").read_text())["accounts"]}
    matches = {r["customer_id"]: dict(r) for r in conn.execute("SELECT * FROM matches")}
    problems = []
    if set(matches) != set(labels):
        problems.append(f"{len(set(labels) - set(matches))} customers neither linked nor queued")
    for cid, m in matches.items():
        if m["status"] != "auto":
            continue
        ref = labels[cid]["sf_ref"]
        if m["account_id"] != idmap[ref]:
            problems.append(f"{cid}: auto-linked to the wrong account {m['account_id']}")
            continue
        for key, obj in OBJECTS.items():
            got = conn.execute("SELECT count(*) FROM sf_records WHERE object = ? AND account_id = ?",
                               (obj, m["account_id"])).fetchone()[0]
            if got != len(source[ref][key]):
                problems.append(f"{cid}: {obj} {got} linked, {len(source[ref][key])} in source")
        for table in ("invoices", "payments"):
            if not billing.execute(f"SELECT 1 FROM {table} WHERE customer_id = ?", (cid,)).fetchone():
                problems.append(f"{cid}: no {table}")
    msgs = conn.execute("SELECT count(*), sum(customer_id IS NULL) FROM messages").fetchone()
    auto = sum(m["status"] == "auto" for m in matches.values())
    print(f"Linked {auto}, review queue {len(matches) - auto}; messages {msgs[0]}, "
          f"unlinked {msgs[1] or 0}")
    for p in problems:
        print(f"FAIL: {p}")
    print("step 3 PASS" if not problems else "step 3 FAIL (fix sync.match_customers)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
