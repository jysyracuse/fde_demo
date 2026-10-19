"""Human review (step 7): turn a Slack button press into a traceable decision.

approve / edit on a reminder writes an outbox row (the simulated "send"); on any other action it
records the internal action as done. pause stops the follow-up. Each writes decision + audit rows.
An approved internal action opens a case held by the role owner until the owner hands it back.
"""
import calendar
import hashlib
import json
import re
import sqlite3
from collections import defaultdict

from .rules import ACTIONS, REMINDERS, as_of, money, redraft
from .store import audit, now

BILLING_IDS = {"CUS": "customers", "INV": "invoices", "PAY": "payments", "CH": "charge_attempts",
               "CN": "credit_notes"}


def unresolved(conn: sqlite3.Connection, billing: sqlite3.Connection, ids: list[str]) -> list[str]:
    """Evidence IDs that do not exist in billing, the inbox or the Salesforce snapshot."""
    def exists(i: str) -> bool:
        table = BILLING_IDS.get(i.split("-")[0])
        if table:
            row = billing.execute(f"SELECT 1 FROM {table} WHERE id = ?", (i,)).fetchone()
            return row is not None
        if i.startswith("MSG-"):
            return conn.execute("SELECT 1 FROM messages WHERE id = ?", (i,)).fetchone() is not None
        return conn.execute("SELECT 1 FROM sf_records WHERE id = ?", (i,)).fetchone() is not None
    return [i for i in ids if not exists(i)]


MONEY = re.compile(r"[A-Z]{3} [\d,]+\.\d{2}")
_MONTH = "|".join(calendar.month_name[1:])
DATE = re.compile(rf"(?<![\w-])(\d{{4}})-(\d{{2}})-(\d{{2}})(?!\d)"     # 2026-10-15, not inside IDs
                  rf"|\b(\d{{1,2}}) ({_MONTH}),? (\d{{4}})\b"           # 15 October 2026
                  rf"|\b({_MONTH}) (\d{{1,2}}),? (\d{{4}})\b")          # October 15, 2026


def dates_in(text: str) -> list[tuple[str, str]]:
    """(as written, ISO) for every date in text, in any of the supported formats."""
    out = []
    for m in DATE.finditer(text):
        y, mo, d = m.groups()[:3]
        if not y:
            g = m.groups()
            d, mon, y = (g[3], g[4], g[5]) if g[3] else (g[7], g[6], g[8])
            mo = list(calendar.month_name).index(mon)
        out.append((m[0], f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"))
    return out


def check_numbers(text: str, conn: sqlite3.Connection, billing: sqlite3.Connection,
                  evidence: list[str], currency: str) -> list[str]:
    """Amounts and dates in text must appear in a cited record, or be an invoice balance net of
    the cited credit notes, or the total of the invoices (gross or net).

    Cited records span billing, the inbox and the Salesforce snapshot; values count whether
    stored as fields (cents, ISO dates) or written in record text (a reply quoting an amount).
    """
    marks = ",".join("?" * len(evidence))
    records = [dict(r) for t in BILLING_IDS.values()
               for r in billing.execute(f"SELECT * FROM {t} WHERE id IN ({marks})", evidence)]
    records += [dict(r) for r in conn.execute(
        f"SELECT id, received_at, subject, body FROM messages WHERE id IN ({marks})", evidence)]
    records += [json.loads(r[0]) for r in conn.execute(
        f"SELECT data FROM sf_records WHERE id IN ({marks})", evidence)]
    blob = json.dumps(records)
    cents = [v for r in records for k, v in r.items() if k.endswith("_cents")]
    credits = defaultdict(int)
    for r in records:
        if str(r.get("id")).startswith("CN-"):
            credits[r["invoice_id"]] += r["amount_cents"]
    invs = [r for r in records if str(r.get("id")).startswith("INV-")]
    net = [r["amount_cents"] - credits[r["id"]] for r in invs]
    totals = [sum(r["amount_cents"] for r in invs), sum(net)]
    amounts = {money(c, currency) for c in cents + net + totals} | set(MONEY.findall(blob))
    dates = {iso for _, iso in dates_in(blob)}
    bad = [a for a in MONEY.findall(text) if a not in amounts]
    bad += [written for written, iso in dates_in(text) if iso not in dates]
    return bad


def authorize(org: sqlite3.Connection, user_id: str) -> None:
    """Only the AR Specialist on record may decide; no one on record = no one may (fail closed)."""
    approver = org.execute("SELECT slack_user FROM roles WHERE role = 'ar_specialist'").fetchone()
    if approver is None:
        raise ValueError("no AR Specialist on record (scripts/roles.py ar_specialist <email>)")
    if user_id != approver[0]:
        raise ValueError(f"only <@{approver[0]}> (AR Specialist) can decide follow-ups")


def version(f) -> str:
    """What the card showed: approving a card checks that the action and draft are unchanged."""
    return hashlib.sha256(f"{f['action']}\n{f['draft_body']}".encode()).hexdigest()[:12]


def decide(conn: sqlite3.Connection, billing: sqlite3.Connection, followup_id: str,
           decision: str, user_id: str, user_name: str, text: str | None = None,
           seen: str | None = None) -> dict:
    """`seen` = version() of the card that was clicked; None skips the check (tests, scripts)."""
    f = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if f is None:
        raise ValueError(f"unknown follow-up {followup_id}")
    if f["status"] != "open":
        return {"already": f["status"], "followup": dict(f)}
    if seen is not None and seen != version(f):
        raise ValueError("the card is out of date (action or draft changed); use the current card")
    final = text if decision == "edit" and text else f["draft_body"]
    if decision != "pause" and (bad := check_numbers(final, conn, billing,
                                                     json.loads(f["evidence"]), f["currency"])):
        raise ValueError(f"amounts/dates not in source records: {', '.join(bad)}")

    status = "paused" if decision == "pause" else "approved"
    # Claim first: the conditional UPDATE takes the write lock, so of two clicks only one wins.
    if conn.execute("UPDATE followups SET status = ? WHERE id = ? AND status = 'open' "
                    "AND action = ? AND draft_body = ?",
                    (status, followup_id, f["action"], f["draft_body"])).rowcount == 0:
        conn.rollback()
        raise ValueError("decided or changed by someone else just now; use the current card")
    did = conn.execute(
        """INSERT INTO decisions (ts, followup_id, decision, user_id, user_name, action, evidence,
             text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now(), followup_id, decision, user_id, user_name, f["action"], f["evidence"], final)
    ).lastrowid
    outbox_id = None
    if status == "approved" and f["action"] in REMINDERS:
        outbox_id = conn.execute(
            """INSERT INTO outbox (ts, followup_id, decision_id, to_email, subject, body, status)
               VALUES (?, ?, ?, ?, ?, ?, 'sent_simulated')""",
            (now(), followup_id, did, f["draft_to"], f["draft_subject"], final)).lastrowid
    audit(conn, f"slack:{user_id}", f"decision_{decision}", followup_id, decision_id=did,
          action=f["action"], outbox_id=outbox_id)
    conn.commit()
    return {"decision_id": did, "status": status, "outbox_id": outbox_id, "text": final,
            "followup": dict(f)}


def override(conn: sqlite3.Connection, billing: sqlite3.Connection, followup_id: str,
             new_action: str, user_id: str, user_name: str, reason: str) -> dict:
    """Reviewer changes the action. The follow-up stays open with a fresh draft to approve."""
    f = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if f is None or f["status"] != "open":
        raise ValueError(f"{followup_id} is not open")
    if new_action not in ACTIONS or new_action == f["action"]:
        raise ValueError(f"cannot change {f['action']} to {new_action}")
    d = redraft(conn, billing, followup_id, new_action)
    if conn.execute("UPDATE followups SET action = ?, draft_to = ?, draft_subject = ?, "
                    "draft_body = ? WHERE id = ? AND status = 'open' AND action = ?",
                    (new_action, d["draft_to"], d["draft_subject"], d["draft_body"], followup_id,
                     f["action"])).rowcount == 0:
        conn.rollback()
        raise ValueError(f"{followup_id} was decided or changed just now")
    did = conn.execute(
        """INSERT INTO decisions (ts, followup_id, decision, user_id, user_name, action, evidence,
             text) VALUES (?, ?, 'override', ?, ?, ?, ?, ?)""",
        (now(), followup_id, user_id, user_name, new_action, f["evidence"],
         f"{f['action']} -> {new_action}: {reason}")).lastrowid
    audit(conn, f"slack:{user_id}", "decision_override", followup_id, decision_id=did,
          from_action=f["action"], to_action=new_action, reason=reason)
    conn.commit()
    return {"decision_id": did, "from": f["action"], "to": new_action}


# --- cases: hand-offs that outlive the day (ONTOLOGY.md §6) -------------------------------------
def open_case(conn: sqlite3.Connection, followup_id: str, role: str, owner: str,
              approver: str) -> int:
    """After an internal action is approved: the owner holds the customer until the handback.
    A newer card for the same customer supersedes the open case."""
    f = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    for old in conn.execute("SELECT id FROM cases WHERE customer_id = ? AND closed_at IS NULL",
                            (f["customer_id"],)).fetchall():
        conn.execute("UPDATE cases SET outcome = 'superseded', closed_at = ? WHERE id = ?",
                     (now(), old[0]))
    cid = conn.execute(
        """INSERT INTO cases (customer_id, followup_id, action, role, owner, opened_by, opened_at,
             evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (f["customer_id"], followup_id, f["action"], role, owner, approver, now(),
         f["evidence"])).lastrowid
    audit(conn, f"slack:{approver}", "case_opened", followup_id, case_id=cid, role=role,
          owner=owner)
    conn.commit()
    return cid


def handback(conn: sqlite3.Connection, billing: sqlite3.Connection, case_id: int, outcome: str,
             user_id: str, wait_until: str | None = None) -> dict:
    """Only the owner hands back. With a date agreed with the customer the case stays open and
    suppresses cards until then; any other outcome closes it and the next run decides afresh."""
    c = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if c is None or c["closed_at"]:
        raise ValueError(f"case {case_id} is not open")
    if user_id != c["owner"]:
        raise ValueError(f"only <@{c['owner']}> can hand this case back")
    if wait_until is not None and wait_until <= as_of(billing):
        raise ValueError(f"the agreed date {wait_until} must be after {as_of(billing)}")
    if wait_until:
        conn.execute("UPDATE cases SET outcome = ?, wait_until = ? WHERE id = ?",
                     (outcome, wait_until, case_id))
    else:
        conn.execute("UPDATE cases SET outcome = ?, closed_at = ? WHERE id = ?",
                     (outcome, now(), case_id))
    audit(conn, f"slack:{user_id}", "case_handback", c["followup_id"], case_id=case_id,
          outcome=outcome, wait_until=wait_until)
    conn.commit()
    return dict(conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone())


def close_settled_cases(conn: sqlite3.Connection, billing: sqlite3.Connection) -> int:
    """Close open cases whose customer no longer has an overdue invoice."""
    owing = {r[0] for r in billing.execute(
        "SELECT DISTINCT customer_id FROM invoices WHERE status = 'open' AND due_date < ?",
        (as_of(billing),))}
    done = [r for r in conn.execute("SELECT id, customer_id, followup_id FROM cases "
                                    "WHERE closed_at IS NULL") if r["customer_id"] not in owing]
    for r in done:
        conn.execute("UPDATE cases SET outcome = 'no_longer_overdue', closed_at = ? WHERE id = ?",
                     (now(), r["id"]))
        audit(conn, "system:rules", "case_closed", r["followup_id"], case_id=r["id"],
              outcome="no_longer_overdue")
    conn.commit()
    return len(done)
