"""Step 2 acceptance check on data/out/.

- every case category has >= 5 customers
- Salesforce storage estimate < 5 MB (Developer Edition limit; most objects count ~2 KB each)
- ground truth is consistent with the billing data (overdue invoices are open and past due)
- deliberate inconsistencies are actually present
"""
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arfollow.config import DATA_DIR  # noqa: E402

SF_LIMIT_MB = 5.0
KB_PER_RECORD = 2


def main() -> int:
    load = lambda n: json.loads((DATA_DIR / n).read_text())  # noqa: E731
    billing, sfdata = load("billing.json"), load("salesforce.json")
    labels, inbox, inbox_labels = load("labels.json"), load("inbox.json"), load("inbox_labels.json")
    as_of = date.fromisoformat(billing["as_of"])
    problems: list[str] = []

    cats = Counter(v["category"] for v in labels["customers"].values())
    print("Customers by category:")
    for cat, n in sorted(cats.items()):
        print(f"  {cat:22s} {n}")
        if cat != "healthy" and n < 5:
            problems.append(f"{cat} has only {n} customers")

    accts = sfdata["accounts"]
    sf_records = len(accts) + sum(len(a[k]) for a in accts
                                  for k in ("contacts", "opportunities", "cases", "tasks"))
    mb = sf_records * KB_PER_RECORD / 1024
    print(f"\nSalesforce records: {sf_records}  (~{mb:.2f} MB of {SF_LIMIT_MB} MB)")
    if mb >= SF_LIMIT_MB * 0.8:
        problems.append(f"Salesforce estimate {mb:.2f} MB too close to the limit")

    inv = {i["id"]: i for i in billing["invoices"]}
    for cid, lab in labels["customers"].items():
        for iid in lab["overdue_invoice_ids"]:
            i = inv[iid]
            if i["status"] != "open" or date.fromisoformat(i["due_date"]) >= as_of:
                problems.append(f"{cid}: {iid} labelled overdue but is "
                                f"{i['status']} / {i['due_date']}")
        if lab["category"] == "healthy" and lab["overdue_invoice_ids"]:
            problems.append(f"{cid}: healthy but has overdue invoices")
        if lab["category"] != "healthy" and not lab["overdue_invoice_ids"]:
            problems.append(f"{cid}: {lab['category']} without overdue invoice")

    lines = Counter()
    for ln in billing["invoice_lines"]:
        lines[ln["invoice_id"]] += round(ln["amount"] * 100)
    bad = [i for i in inv.values() if lines[i["id"]] != round(i["amount"] * 100)]
    if bad:
        problems.append(f"{len(bad)} invoices whose lines don't sum to the total")

    name_drift = sum(1 for v in labels["customers"].values()
                     if v["crm_name"] != v["billing_name"])
    missing_id = sum(1 for v in labels["customers"].values() if v["crm_account_number"] is None)
    other_fmt = sum(1 for cid, v in labels["customers"].items()
                    if v["crm_account_number"] not in (None, cid))
    personal = sum(1 for v in inbox_labels.values() if v["sender_is_personal"])
    print(f"\nInconsistencies: name drift {name_drift}, missing billing ID {missing_id}, "
          f"ID in other format {other_fmt}, replies from personal email {personal}")
    unapplied = sum(1 for p in billing["payments"] if p["status"] == "unapplied")
    print(f"Unapplied payments {unapplied}, credit notes {len(billing['credit_notes'])}, "
          f"failed charges {sum(1 for c in billing['charge_attempts'] if c['result'] == 'failed')}")
    intents = Counter(v["intent"] for v in inbox_labels.values())
    print(f"Inbox: {len(inbox)} messages, intents {dict(sorted(intents.items()))}")
    if min(name_drift, missing_id, other_fmt, personal) == 0:
        problems.append("a deliberate inconsistency type is missing")

    if problems:
        print("\nFAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
