"""Load data/out/salesforce.json into the Developer Edition org.

Incremental by default: accounts already loaded (by data/sf_id_map.json) keep their Salesforce
Ids and records; accounts in the org but not in the current set are deleted with their children;
new accounts are inserted. Works because generator sets are nested (same ref -> same data).
--rebuild deletes every record from a previous run (Accounts with Site = 'ardemo' and their
children) and inserts fresh. This is the only code path that writes to Salesforce.

Writes data/sf_id_map.json (generator ref -> Salesforce Id). That file is for evaluation only;
the runtime system must link records through entity matching (step 3), not through this map.

--update-descriptions patches only Account.Description in place (keeps every Salesforce Id, so
decisions and Slack cards that cite them stay traceable). Needs data/sf_id_map.json.

Usage: python scripts/load_salesforce.py [--rebuild] [--delete-only] [--update-descriptions]
                                         [--dry-run]
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


def delete_existing(sf: SalesforceClient, account_ids: list[str] | None = None) -> None:
    """Delete demo accounts and their children: all of them, or only `account_ids`."""
    if account_ids is None:
        acc, child = f"Site = '{MARKER}'", f"Account.Site = '{MARKER}'"
    elif not account_ids:
        return
    else:
        quoted = ",".join(f"'{i}'" for i in account_ids)
        acc, child = f"Id IN ({quoted})", f"AccountId IN ({quoted})"
    for obj in ("Task", "Case", "Opportunity", "Contact", "Account"):
        cond = acc if obj == "Account" else child
        ids = [r["Id"] for r in sf.query(f"SELECT Id FROM {obj} WHERE {cond}")]
        if ids:
            print(f"Deleting {len(ids):4d} {obj}")
            sf.delete_many(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="delete every demo record, reload")
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
    map_path = config.ROOT / "data" / "sf_id_map.json"
    id_map: dict[str, str] = {}
    if args.rebuild or args.delete_only:
        delete_existing(sf)
        if args.delete_only:
            map_path.unlink(missing_ok=True)
            return
    else:
        old = json.loads(map_path.read_text()) if map_path.exists() else {}
        in_org = {r["Id"] for r in sf.query(f"SELECT Id FROM Account WHERE Site = '{MARKER}'")}
        keep = {a["ref"] for a in accounts if old.get(a["ref"]) in in_org}
        kept_ids = {old[r] for r in keep}
        delete_existing(sf, sorted(in_org - kept_ids))
        # keep the map entries of kept accounts and their contacts (CON-<acc>-<n>)
        id_map = {k: v for k, v in old.items()
                  if k in keep or (k.startswith("CON-") and f"ACC-{k.split('-')[1]}" in keep)}
        print(f"Kept {len(keep)} Account (already loaded)")
        accounts = [a for a in accounts if a["ref"] not in keep]

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

    map_path.write_text(json.dumps(id_map, indent=2) + "\n")
    print(f"Wrote {map_path}")


if __name__ == "__main__":
    main()
