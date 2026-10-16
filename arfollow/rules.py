"""Step 4 (layer 1): detect overdue exposure, derive signals, pick an action. No LLM.

Every follow-up carries the record IDs its signals came from (evidence), and amounts come from
billing in integer cents, so drafts cannot drift from the source.
"""
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date

from . import config
from .store import audit
from .sync import norm_name

# Precedence when signals overlap: a wrong reminder during a dispute or renewal costs the most.
PRECEDENCE = [
    ("open_billing_dispute", "pause_and_resolve_dispute"),
    ("payment_claimed", "verify_payment_no_dunning"),
    ("open_renewal", "notify_account_owner"),
    ("churn_risk", "retention_handoff"),
    ("chronic_late", "escalate"),
    ("card_expired", "gentle_reminder"),
]
REMINDERS = {"gentle_reminder", "standard_reminder"}
ACTIONS = [a for _, a in PRECEDENCE] + ["standard_reminder"]
LATE_DAYS, CHRONIC_LATE_COUNT = 10, 4  # > 3 late payments in 12 months

# shortcut: keyword baseline for reply intent; layer 2 (step 5) replaces it with an LLM classifier
INTENTS = [
    ("cancel", r"\bcancel|not renewing|close the account|another platform"),
    ("dispute", r"\bwrong\b|doesn't match|won't be paying|extra user|corrected invoice"),
    ("promise_to_pay", r"\bwe'll pay|will get it paid|payment run|retry the charge|settle the "),
    ("paid", r"\bpaid\b|remittance|\bsettled\b|payment of .+ went out"),
]


def classify(body: str) -> str:
    text = body.lower()
    return next((intent for intent, pat in INTENTS if re.search(pat, text)), "unclear")


def money(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100:,}.{cents % 100:02d}"


def as_of(billing: sqlite3.Connection) -> str:
    return billing.execute("SELECT value FROM meta WHERE key = 'as_of'").fetchone()[0]


# --- inbox -------------------------------------------------------------------------------------
def link_messages(conn: sqlite3.Connection, billing: sqlite3.Connection) -> dict[str, int]:
    """Link each reply to a customer: invoice ID > contact email > billing email > signature."""
    inv_owner = dict(billing.execute("SELECT id, customer_id FROM invoices"))
    billing_email = {r["billing_email"]: r["id"] for r in billing.execute(
        "SELECT id, billing_email FROM customers")}
    acc_to_cust = {r["account_id"]: r["customer_id"] for r in conn.execute(
        "SELECT customer_id, account_id FROM matches WHERE status = 'auto'")}
    contact_email = {}
    for r in conn.execute("SELECT account_id, data FROM sf_records WHERE object = 'Contact'"):
        if email := json.loads(r["data"]).get("Email"):
            contact_email[email.lower()] = acc_to_cust.get(r["account_id"])
    by_name = {norm_name(r["legal_name"]): r["id"]
               for r in billing.execute("SELECT id, legal_name FROM customers")}
    for r in conn.execute("SELECT id, data FROM sf_records WHERE object = 'Account'"):
        if cid := acc_to_cust.get(r["id"]):
            by_name[norm_name(json.loads(r["data"])["Name"])] = cid

    llm = {r["message_id"]: r["intent"] for r in conn.execute(
        "SELECT message_id, intent FROM reply_labels WHERE valid = 1 ORDER BY id")}
    rows = []
    for m in json.loads((config.DATA_DIR / "inbox.json").read_text()):
        text = f"{m['subject']}\n{m['body']}"
        inv = re.search(r"INV-\d+", text)
        signature = m["body"].strip().splitlines()[-1]
        for method, cid in (("invoice_id", inv and inv_owner.get(inv[0])),
                            ("contact_email", contact_email.get(m["from_email"].lower())),
                            ("billing_email", billing_email.get(m["from_email"])),
                            ("signature", by_name.get(norm_name(signature)))):
            if cid:
                break
        else:
            method = None
        intent = llm.get(m["id"]) or classify(m["body"])
        rows.append((m["id"], cid, method, intent, m["received_at"], m["from_email"],
                     m["subject"], m["body"]))
    conn.execute("DELETE FROM messages")
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return {"messages": len(rows), "unlinked": sum(r[1] is None for r in rows)}


# --- follow-ups --------------------------------------------------------------------------------
def build_followups(conn: sqlite3.Connection, billing: sqlite3.Connection) -> list[dict]:
    today = as_of(billing)
    d_today = date.fromisoformat(today)
    customers = {r["id"]: dict(r) for r in billing.execute("SELECT * FROM customers")}
    overdue = defaultdict(list)
    for r in billing.execute("SELECT * FROM invoices WHERE status = 'open' AND due_date < ? "
                             "ORDER BY due_date", (today,)):
        overdue[r["customer_id"]].append(dict(r))
    failed = {r["invoice_id"]: (r["id"], r["failure_code"]) for r in billing.execute(
        """SELECT ca.id, ca.invoice_id, ca.failure_code FROM charge_attempts ca
           JOIN invoices i ON i.id = ca.invoice_id AND i.status = 'open'
           WHERE ca.result = 'failed' AND ca.attempted_at =
             (SELECT max(attempted_at) FROM charge_attempts WHERE invoice_id = ca.invoice_id)""")}
    late = defaultdict(list)
    for r in billing.execute(
            """SELECT p.id, i.customer_id FROM payments p JOIN invoices i ON i.id = p.invoice_id
               WHERE julianday(p.received_at) - julianday(i.due_date) > ?""", (LATE_DAYS,)):
        late[r["customer_id"]].append(r["id"])
    unapplied = [dict(r) for r in billing.execute(
        "SELECT * FROM payments WHERE status = 'unapplied'")]
    credit, credited = defaultdict(list), defaultdict(list)
    for r in billing.execute("SELECT id, customer_id, invoice_id, amount_cents FROM credit_notes"):
        credit[r["customer_id"]].append(r["id"])
        credited[r["invoice_id"]].append(dict(r))
    usage = defaultdict(list)
    for r in billing.execute("SELECT customer_id, active_users FROM usage_monthly ORDER BY month"):
        usage[r["customer_id"]].append(r["active_users"])
    account = {r["customer_id"]: r["account_id"] for r in conn.execute(
        "SELECT customer_id, account_id FROM matches WHERE status = 'auto'")}
    sf = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT object, id, account_id, data FROM sf_records"):
        sf[r["account_id"]][r["object"]].append({"Id": r["id"], **json.loads(r["data"])})
    replies = defaultdict(list)
    for r in conn.execute("SELECT * FROM messages WHERE customer_id IS NOT NULL "
                          "ORDER BY received_at"):
        replies[r["customer_id"]].append(dict(r))

    out = []
    for cid, invoices in overdue.items():
        c, acc = customers[cid], account.get(cid)
        crm = sf[acc] if acc else {}
        for i in invoices:  # what is owed is the invoice less credit notes against it (L-005)
            i["credits"] = credited[i["id"]]
            i["balance_cents"] = i["amount_cents"] - sum(x["amount_cents"] for x in i["credits"])
        ev = [cid] + ([acc] if acc else []) + [i["id"] for i in invoices]
        ev += [x["id"] for i in invoices for x in i["credits"]]
        sig: dict[str, object] = {}

        disputes = [x for x in crm.get("Case", [])
                    if x["Subject"].startswith("[Billing]") and not x["IsClosed"]]
        intents = {m["intent"] for m in replies[cid]}
        if disputes or "dispute" in intents:
            sig["open_billing_dispute"] = [x["Subject"] for x in disputes] or ["reply: dispute"]
            ev += [x["Id"] for x in disputes] + credit[cid]
        overdue_nums = {i["id"][4:]: i for i in invoices}
        pay_match = [p for p in unapplied if any(
            n in (p["reference"] or "") and i["amount_cents"] == p["amount_cents"]
            for n, i in overdue_nums.items())]
        if pay_match or "paid" in intents:
            sig["payment_claimed"] = [p["id"] for p in pay_match] or ["reply: paid"]
            ev += [p["id"] for p in pay_match]
        renewals = [o for o in crm.get("Opportunity", [])
                    if "Renewal" in (o.get("Type") or "") and not o["IsClosed"]]
        if renewals:
            o = renewals[0]
            ae = re.search(r"Account executive: ([^.]+)", o.get("Description") or "")
            sig["open_renewal"] = {"opportunity": o["Name"], "stage": o["StageName"],
                                   "close": o["CloseDate"], "ae": ae[1] if ae else None}
            ev.append(o["Id"])
        u = usage[cid]
        declining = len(u) >= 4 and u[-1] < 0.6 * u[0] and u[-2] < 0.8 * u[0]
        if "cancel" in intents or declining:
            sig["churn_risk"] = {"usage": u, "cancel_reply": "cancel" in intents}
        promises = [t for t in crm.get("Task", []) if "payment promise" in t["Subject"]]
        broken = [t for t in promises if (m := re.search(r"by (\d{4}-\d{2}-\d{2})",
                  t.get("Description") or "")) and date.fromisoformat(m[1]) < d_today]
        if broken or len(late[cid]) >= CHRONIC_LATE_COUNT:
            sig["chronic_late"] = {"late_payments": len(late[cid]),
                                   "broken_promises": [t["Id"] for t in broken]}
            ev += [t["Id"] for t in broken] + late[cid][-3:]
        fails = [failed[i["id"]] for i in invoices if i["id"] in failed]
        if fails:
            sig["charge_failure"] = fails[-1][1]
            ev += [f[0] for f in fails]
            if fails[-1][1] == "expired_card":
                sig["card_expired"] = c["card_expiry"]
        ev += [m["id"] for m in replies[cid]]

        action = next((a for s, a in PRECEDENCE if s in sig), "standard_reminder")
        amount = sum(i["balance_cents"] for i in invoices)
        days = (d_today - date.fromisoformat(invoices[0]["due_date"])).days
        out.append({"id": f"FU-{today}-{cid}", "as_of": today, "customer_id": cid,
                    "account_id": acc, "action": action, "amount_cents": amount,
                    "currency": c["currency"], "days_overdue": days, "signals": sig,
                    "evidence": list(dict.fromkeys(ev)),
                    **draft(action, c, crm, invoices, sig),
                    "_ctx": (c, crm, invoices),
                    "_score": amount * (1 + days / 30) * (2 if c["plan"] == "Enterprise" else 1)})
    out.sort(key=lambda f: -f["_score"])
    for n, f in enumerate(out, 1):
        f["rank"] = n
    return out


def draft(action: str, c: dict, crm: dict, invoices: list[dict], sig: dict) -> dict:
    cur = c["currency"]
    lines = "\n".join(
        f"  {i['id']}  due {i['due_date']}  {money(i['balance_cents'], cur)}"
        + "".join(f" ({money(i['amount_cents'], cur)} less credit note {x['id']} "
                  f"{money(x['amount_cents'], cur)})" for x in i["credits"])
        for i in invoices)
    total = money(sum(i["balance_cents"] for i in invoices), cur)
    contacts = crm.get("Contact", []) if crm else []
    first = contacts[0]["FirstName"] if contacts else "there"
    if action in REMINDERS:
        if action == "gentle_reminder" and sig.get("card_expired"):  # override may lack it
            body = (f"Hi {first},\n\nWe couldn't process the card payment for your "
                    f"{config.COMPANY} subscription. The card on file appears to have expired."
                    f"\n\n{lines}\n\nYou can update your card in the billing portal and we'll "
                    "retry the charge. Thanks!")
        else:
            body = (f"Hi {first},\n\nA friendly reminder that the following invoices are past "
                    f"due:\n\n{lines}\n\nTotal outstanding: {total}. If you've already paid, "
                    "please reply with the remittance details.")
        body += f"\n\n{config.COMPANY} Accounts\n{config.AR_EMAIL}"
        subject = f"{config.COMPANY}: payment for {invoices[0]['id']}"
        return {"draft_to": c["billing_email"], "draft_subject": subject, "draft_body": body}

    # A reviewer can override to an action whose signal is absent: fall back to a plain note.
    if action == "pause_and_resolve_dispute" and (r := sig.get("open_billing_dispute")):
        note = (f"Pause dunning. Open billing dispute: {'; '.join(r)}. "
                "Check invoice lines and credit notes before contacting the customer.")
    elif action == "verify_payment_no_dunning" and (r := sig.get("payment_claimed")):
        note = (f"Customer indicates payment ({', '.join(r)}). "
                "Verify against unapplied payments before any reminder.")
    elif action == "notify_account_owner" and (r := sig.get("open_renewal")):
        note = (f"Renewal in progress: {r['opportunity']} ({r['stage']}, closes {r['close']}). "
                f"Notify AE {r['ae'] or 'unknown'}; hold dunning.")
    elif action == "retention_handoff" and (r := sig.get("churn_risk")):
        note = (f"Churn risk (monthly active users {r['usage']}, cancel reply: "
                f"{r['cancel_reply']}). Hand off to retention.")
    elif action == "escalate" and (r := sig.get("chronic_late")):
        note = (f"Chronic late payer ({r['late_payments']} late payments, "
                f"{len(r['broken_promises'])} broken promise). Escalate to finance lead.")
    else:
        note = f"{action.replace('_', ' ').capitalize()} (set by reviewer)."
    return {"draft_to": None, "draft_subject": None,
            "draft_body": f"{note}\nOutstanding {total}:\n{lines}"}


def redraft(conn: sqlite3.Connection, billing: sqlite3.Connection, followup_id: str,
            action: str) -> dict:
    """Draft for a reviewer-chosen action, from the same source records."""
    f = next(x for x in build_followups(conn, billing) if x["id"] == followup_id)
    c, crm, invoices = f["_ctx"]
    return draft(action, c, crm, invoices, f["signals"])


def save_followups(conn: sqlite3.Connection, followups: list[dict]) -> None:
    """Upsert; a follow-up already decided or overridden by a reviewer keeps its state."""
    for f in followups:
        conn.execute(
            """INSERT INTO followups (id, as_of, customer_id, account_id, action, rank,
                 amount_cents, currency, days_overdue, signals, evidence, draft_to, draft_subject,
                 draft_body)
               VALUES (:id, :as_of, :customer_id, :account_id, :action, :rank, :amount_cents,
                 :currency, :days_overdue, :signals, :evidence, :draft_to, :draft_subject,
                 :draft_body)
               ON CONFLICT (id) DO UPDATE SET action = excluded.action, rank = excluded.rank,
                 amount_cents = excluded.amount_cents,
                 signals = excluded.signals, evidence = excluded.evidence,
                 draft_body = excluded.draft_body
               WHERE followups.status = 'open' AND NOT EXISTS (SELECT 1 FROM decisions d
                 WHERE d.followup_id = followups.id AND d.decision = 'override')""",
            {**f, "signals": json.dumps(f["signals"]), "evidence": json.dumps(f["evidence"])})
    audit(conn, "system:rules", "followups_built", count=len(followups))
    conn.commit()
