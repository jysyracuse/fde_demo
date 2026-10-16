"""Load data/out/salesforce.json into the Developer Edition org.

Idempotent: first deletes every record from a previous run (Accounts with Site = 'ardemo' and
their children), then inserts fresh. This is the only code path that writes to Salesforce.

Writes data/sf_id_map.json (generator ref -> Salesforce Id). That file is for evaluation only;
the runtime system must link records through entity matching (step 3), not through this map.

--update-descriptions patches only Account.Description in place (keeps every Salesforce Id, so
decisions and Slack cards that cite them stay traceable). Needs data/sf_id_map.json.

Usage: python scripts/load_salesforce.py [--delete-only] [--update-descriptions] [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arfollow import config  # noqa: E402
from arfollow.salesforce import SalesforceClient  # noqa: E402

MARKER = "ardemo"


def clean(fields: dict) -> dict:
    """Drop generator-only keys (ref, contact_ref, _private)."""
    return {k: v for k, v in fields.items()
            if k not in ("ref", "contact_ref") and not k.startswith("_") and v is not None}


def delete_existing(sf: SalesforceClient) -> None:
    for obj, filt in [("Task", "Account.Site"), ("Case", "Account.Site"),
                      ("Opportunity", "Account.Site"), ("Contact", "Account.Site"),
                      ("Account", "Site")]:
        ids = [r["Id"] for r in sf.query(f"SELECT Id FROM {obj} WHERE {filt} = '{MARKER}'")]
        if ids:
            print(f"Deleting {len(ids):4d} {obj}")
            sf.delete_many(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete-only", action="store_true")
    ap.add_argument("--update-descriptions", action="store_true",
                    help="patch Account.Description in place; no delete, no insert")
    ap.add_argument("--dry-run", action="store_true", help="print counts, touch nothing")
    args = ap.parse_args()

    data = json.loads((config.DATA_DIR / "salesforce.json").read_text())
    accounts = data["accounts"]
    counts = {k: sum(len(a[k]) for a in accounts)
              for k in ("contacts", "opportunities", "cases", "tasks")}
    print(f"Accounts {len(accounts)}, " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if args.dry_run:
        return

    sf = SalesforceClient(allow_write=True)
    sf.authenticate()
    if args.update_descriptions:
        id_map = json.loads((config.ROOT / "data" / "sf_id_map.json").read_text())
        rows = [{"Id": id_map[a["ref"]], "Description": a["fields"]["Description"]}
                for a in accounts]
        print(f"Updated {sf.update_many('Account', rows)} Account descriptions")
        return
    delete_existing(sf)
    if args.delete_only:
        return

    id_map: dict[str, str] = {}
    acc_ids = sf.create_many("Account", [clean(a["fields"]) for a in accounts])
    for a, sid in zip(accounts, acc_ids):
        id_map[a["ref"]] = sid
    print(f"Inserted {len(acc_ids)} Account")

    contacts = [(a, c) for a in accounts for c in a["contacts"]]
    con_ids = sf.create_many("Contact", [{**clean(c), "AccountId": id_map[a["ref"]]}
                                         for a, c in contacts])
    for (_, c), sid in zip(contacts, con_ids):
        id_map[c["ref"]] = sid
    print(f"Inserted {len(con_ids)} Contact")

    def children(key: str, sobject: str, who_field: str | None) -> None:
        rows = []
        for a in accounts:
            for rec in a[key]:
                row = clean(rec)
                if sobject == "Task":
                    row.pop("Type", None)  # not exposed in Developer Edition; Subject keeps it
                    row["WhatId"] = id_map[a["ref"]]
                else:
                    row["AccountId"] = id_map[a["ref"]]
                if who_field and rec.get("contact_ref"):
                    row[who_field] = id_map[rec["contact_ref"]]
                rows.append(row)
        if rows:
            n = len(sf.create_many(sobject, rows))
            print(f"Inserted {n} {sobject}")

    children("opportunities", "Opportunity", None)
    children("cases", "Case", "ContactId")
    children("tasks", "Task", "WhoId")

    out = config.ROOT / "data" / "sf_id_map.json"
    out.write_text(json.dumps(id_map, indent=2) + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
