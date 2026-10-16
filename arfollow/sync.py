"""Step 3: read-only Salesforce snapshot into app.db, then link billing customers to accounts.

Billing and CRM share no reliable key. A customer links to an account automatically only when
two independent signals agree; everything else goes to the review queue (status 'review').
"""
import json
import re
import sqlite3
from collections import defaultdict

from .salesforce import SalesforceClient
from .store import audit

SOQL = {
    "Account": "SELECT Id, Name, AccountNumber, Website, Rating, Industry FROM Account",
    "Contact": "SELECT Id, AccountId, FirstName, LastName, Email, Title FROM Contact",
    "Opportunity": "SELECT Id, AccountId, Name, Type, StageName, CloseDate, Amount, IsClosed, "
                   "Description FROM Opportunity",
    "Case": "SELECT Id, AccountId, ContactId, Subject, Description, Status, IsClosed, Priority "
            "FROM Case",
    "Task": "SELECT Id, WhatId, WhoId, Subject, ActivityDate, Status, Description FROM Task",
}
LEGAL = {"pty", "ltd", "limited", "inc", "llc"}
SPELLING = {"harbor": "harbour", "center": "centre", "company": "co"}


def sync_salesforce(conn: sqlite3.Connection, sf: SalesforceClient) -> dict[str, int]:
    counts = {}
    conn.execute("DELETE FROM sf_records")
    for obj, soql in SOQL.items():
        rows = list(sf.query(soql))
        conn.executemany("INSERT INTO sf_records VALUES (?, ?, ?, ?)", [
            (obj, r["Id"], r["Id"] if obj == "Account" else r.get("AccountId") or r.get("WhatId"),
             json.dumps(r)) for r in rows])
        counts[obj] = len(rows)
    audit(conn, "system:sync", "salesforce_snapshot", counts=counts)
    conn.commit()
    return counts


# --- entity matching ----------------------------------------------------------------------------
def norm_name(name: str) -> str:
    """'HARBOR PHYSIO PTY. LTD.' and 'The Harbour Physio (AU)' -> 'harbour physio'."""
    words = re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", " ", name.lower()))
    words = [SPELLING.get(w, w) for w in words if w not in LEGAL]
    return " ".join(words[1:] if words[:1] == ["the"] else words)


def norm_account_number(value: str | None) -> str | None:
    """'CUS1042', 'CUS-01042' -> 'CUS-01042'."""
    m = re.fullmatch(r"\s*CUS-?0*(\d+)\s*", value or "", re.I)
    return f"CUS-{int(m[1]):05d}" if m else None


def domain(email_or_url: str | None) -> str | None:
    m = re.search(r"(?:@|//(?:www\.)?)([a-z0-9.-]+)", (email_or_url or "").lower())
    return m[1] if m else None


def match_customers(conn: sqlite3.Connection, billing: sqlite3.Connection) -> dict[str, int]:
    accounts = {r["id"]: json.loads(r["data"])
                for r in conn.execute("SELECT id, data FROM sf_records WHERE object = 'Account'")}
    domains = defaultdict(set)
    for acc_id, acc in accounts.items():
        domains[acc_id].add(domain(acc.get("Website")))
    for r in conn.execute("SELECT account_id, data FROM sf_records WHERE object = 'Contact'"):
        domains[r["account_id"]].add(domain(json.loads(r["data"]).get("Email")))
    by_number, by_name, by_domain = defaultdict(set), defaultdict(set), defaultdict(set)
    for acc_id, acc in accounts.items():
        if n := norm_account_number(acc.get("AccountNumber")):
            by_number[n].add(acc_id)
        by_name[norm_name(acc["Name"])].add(acc_id)
        for d in domains[acc_id] - {None}:
            by_domain[d].add(acc_id)

    rows = []
    for c in billing.execute("SELECT id, legal_name, billing_email FROM customers"):
        num = by_number.get(c["id"], set())
        name = by_name.get(norm_name(c["legal_name"]), set())
        dom = by_domain.get(domain(c["billing_email"]), set())
        votes = [s for s in (num, name, dom) if len(s) == 1]
        agreed = {next(iter(s)) for s in votes}
        conflict = len(agreed) > 1 or any(len(s) > 1 for s in (num, name, dom))
        if len(votes) >= 2 and not conflict:
            method = "+".join(k for k, s in (("account_number", num), ("name", name),
                                             ("domain", dom)) if len(s) == 1)
            rows.append((c["id"], agreed.pop(), method, "auto", "[]"))
        else:
            cands = sorted(num | name | dom)
            rows.append((c["id"], None, "insufficient_evidence", "review", json.dumps(cands)))

    claimed = defaultdict(list)  # one account must not link to two customers
    for r in rows:
        if r[1]:
            claimed[r[1]].append(r[0])
    rows = [(cid, None, "account_claimed_twice", "review", json.dumps([acc]))
            if acc and len(claimed[acc]) > 1 else (cid, acc, m, s, cands)
            for cid, acc, m, s, cands in rows]

    conn.execute("DELETE FROM matches")
    conn.executemany("INSERT INTO matches VALUES (?, ?, ?, ?, ?)", rows)
    stats = {s: sum(r[3] == s for r in rows) for s in ("auto", "review")}
    audit(conn, "system:match", "entity_match", **stats)
    conn.commit()
    return stats
