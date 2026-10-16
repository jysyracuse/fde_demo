"""Human review (step 7): turn a Slack button press into a traceable decision.

approve / edit on a reminder writes an outbox row (the simulated "send"); on any other action it
records the internal action as done. pause stops the follow-up. Each writes decision + audit rows.
"""
import calendar
import json
import re
import sqlite3
from collections import defaultdict

from .rules import ACTIONS, REMINDERS, money, redraft
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


def decide(conn: sqlite3.Connection, billing: sqlite3.Connection, followup_id: str,
           decision: str, user_id: str, user_name: str, text: str | None = None) -> dict:
    f = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if f is None:
        raise ValueError(f"unknown follow-up {followup_id}")
    if f["status"] != "open":
        return {"already": f["status"], "followup": dict(f)}
    final = text if decision == "edit" and text else f["draft_body"]
    if decision != "pause" and (bad := check_numbers(final, conn, billing,
                                                     json.loads(f["evidence"]), f["currency"])):
        raise ValueError(f"amounts/dates not in source records: {', '.join(bad)}")

    did = conn.execute(
        """INSERT INTO decisions (ts, followup_id, decision, user_id, user_name, action, evidence,
             text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now(), followup_id, decision, user_id, user_name, f["action"], f["evidence"], final)
    ).lastrowid
    status = "paused" if decision == "pause" else "approved"
    conn.execute("UPDATE followups SET status = ? WHERE id = ?", (status, followup_id))
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
    did = conn.execute(
        """INSERT INTO decisions (ts, followup_id, decision, user_id, user_name, action, evidence,
             text) VALUES (?, ?, 'override', ?, ?, ?, ?, ?)""",
        (now(), followup_id, user_id, user_name, new_action, f["evidence"],
         f"{f['action']} -> {new_action}: {reason}")).lastrowid
    conn.execute("UPDATE followups SET action = ?, draft_to = ?, draft_subject = ?, "
                 "draft_body = ? WHERE id = ?",
                 (new_action, d["draft_to"], d["draft_subject"], d["draft_body"], followup_id))
    audit(conn, f"slack:{user_id}", "decision_override", followup_id, decision_id=did,
          from_action=f["action"], to_action=new_action, reason=reason)
    conn.commit()
    return {"decision_id": did, "from": f["action"], "to": new_action}
