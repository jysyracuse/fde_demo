"""Step 4 acceptance.

HANDOFF: 筛选、排序、规则排除、每日 Slack 汇总。验收：争议和续约案例被正确暂停或转交，全程无 LLM

Checks: dispute and renewal customers get exactly their expected action (100 %); overall action
accuracy meets the README target (>= 90 %); layer-1 modules import no LLM or agent library;
every follow-up amount equals the open overdue balance net of credit notes (independent SQL, L-005).
The digest itself is posted by `uv run python -m arfollow.run --post`.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from arfollow import config, rules, store  # noqa: E402

MUST_BE_EXACT = {"invoice_dispute", "renewal_in_progress"}
LAYER1 = ["arfollow/rules.py", "arfollow/sync.py", "arfollow/store.py", "arfollow/run.py"]
LLM = re.compile(r"^\s*(import|from)\s+(anthropic|openai|mcp)\b", re.M)


def main() -> int:
    if not config.APP_DB.exists():
        print("FAIL: no snapshot. Run `uv run python -m arfollow.run` first.")
        return 1
    conn, billing = store.app(), store.billing()
    built = rules.build_followups(conn, billing)
    pred = {f["customer_id"]: f["action"] for f in built}
    labels = json.loads((config.DATA_DIR / "labels.json").read_text())["customers"]
    per = defaultdict(lambda: [0, 0])
    wrong = []
    for cid, lab in labels.items():
        ok = pred.get(cid, "none") == lab["expected_action"]
        per[lab["category"]][0] += ok
        per[lab["category"]][1] += 1
        if not ok:
            wrong.append(f"{cid} {lab['category']}: got {pred.get(cid, 'none')}")
    for cat, (a, n) in sorted(per.items()):
        print(f"  {cat:22s} {a}/{n}")
    total = sum(a for a, _ in per.values()) / len(labels)
    print(f"Overall action accuracy {total:.0%}")
    problems = [w for w in wrong if w.split()[1].rstrip(":") in MUST_BE_EXACT]
    if total < 0.9:
        problems.append(f"overall accuracy {total:.0%} < 90 %: {wrong}")
    net = dict(billing.execute(
        """SELECT customer_id, sum(amount_cents - coalesce((SELECT sum(c.amount_cents)
             FROM credit_notes c WHERE c.invoice_id = i.id), 0))
           FROM invoices i WHERE status = 'open' AND due_date < ? GROUP BY customer_id""",
        (rules.as_of(billing),)).fetchall())
    problems += [f"{f['customer_id']} amount {f['amount_cents']} != net balance "
                 f"{net.get(f['customer_id'])}" for f in built
                 if f["amount_cents"] != net.get(f["customer_id"])]
    problems += [f"{p} imports an LLM library; layer 1 must be rules only"
                 for p in LAYER1 if LLM.search((ROOT / p).read_text())]
    for p in problems:
        print(f"FAIL: {p}")
    print("step 4 PASS" if not problems else "step 4 FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
