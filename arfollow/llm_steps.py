"""Step 5 (layer 2): reply classification and cited briefs. Every output passes a validator or
is rejected (stored with its problems, never shown as fact).

Validators:
- reply: intent in the allowed set; the quote is a verbatim substring of the reply; a promise date
  is a real date on or after the day the reply arrived.
- brief: every sentence cites at least one provided record; cited IDs exist; every amount and date
  appears in a cited record; every other number appears somewhere in the input facts.
"""
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import review
from .llm import complete_json
from .rules import money
from .store import now

INTENTS = ["paid", "dispute", "promise_to_pay", "cancel", "unclear"]
WORKERS = 8

CLASSIFY_SYSTEM = """You classify a customer's reply to an accounts-receivable email.
Return JSON: {"intent": "...", "promise_date": "YYYY-MM-DD" or null, "quote": "..."}.
intent is one of:
- paid: says the payment was already made or settled
- dispute: disagrees with the invoice (amount, users, PO) or refuses to pay until it is corrected
- promise_to_pay: commits to pay in the future (a date, a weekday, a payment run, a card retry)
- cancel: wants to cancel, close the account or not renew (even if offering to pay what is owed)
- unclear: anything else (out of office, questions, thanks)
promise_date: only for promise_to_pay, resolved against the received date; null if no date follows.
quote: the shortest exact substring of the reply that shows the intent."""

BRIEF_SYSTEM = """You write a context brief for an accounts-receivable reviewer.
Use ONLY the facts given. Return JSON {"sentences": [{"text": "...", "cites": ["<id>", ...]}]}
with 3 to 5 sentences. Each sentence cites the ids of the facts it uses.
Write money exactly as given (e.g. "AUD 3,050.00") and dates as YYYY-MM-DD.
Do not compute new numbers (no sums, differences or day counts) beyond those in the facts.
Describe the situation; do not recommend an action."""


# --- reply classification ----------------------------------------------------------------------
def validate_reply(out: dict, body: str, received: str) -> list[str]:
    problems = []
    if out.get("intent") not in INTENTS:
        problems.append(f"intent {out.get('intent')!r} not allowed")
    squash = lambda s: " ".join(str(s).split())  # noqa: E731
    if not out.get("quote") or squash(out["quote"]) not in squash(body):
        problems.append("quote is not a verbatim part of the reply")
    if pd := out.get("promise_date"):
        try:
            if date.fromisoformat(pd) < date.fromisoformat(received[:10]):
                problems.append(f"promise_date {pd} before the reply was received")
        except ValueError:
            problems.append(f"promise_date {pd!r} is not a date")
    return problems


def classify_replies(conn: sqlite3.Connection) -> dict:
    msgs = [dict(r) for r in conn.execute("SELECT * FROM messages")]

    def one(m):
        user = f"Received: {m['received_at']}\nSubject: {m['subject']}\n\n{m['body']}"
        try:
            out, meta = complete_json("classify_reply", CLASSIFY_SYSTEM, user)
            return m, out, meta, validate_reply(out, m["body"], m["received_at"])
        except Exception as e:  # noqa: BLE001  network/JSON failure: record and route to a human
            return m, {}, {"task": "classify_reply", "model": "?", "ms": 0, "error": str(e)}, [
                f"call failed: {e}"]

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(one, msgs))
    for m, out, meta, problems in results:
        log_call(conn, meta, m["id"])
        conn.execute("""INSERT INTO reply_labels (ts, message_id, model, intent, promise_date,
                          quote, valid, problems) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (now(), m["id"], meta["model"], out.get("intent"), out.get("promise_date"),
                      out.get("quote"), int(not problems), json.dumps(problems)))
    conn.commit()
    return {"replies": len(results), "rejected": sum(bool(p) for *_, p in results)}


# --- briefs ------------------------------------------------------------------------------------
def facts(conn: sqlite3.Connection, billing: sqlite3.Connection, f: dict) -> list[dict]:
    """The only material the model sees: the follow-up's evidence records, keyed by ID."""
    cur = f["currency"]
    signals = json.loads(f["signals"])
    for v in signals.values():  # counts the model may state must exist as facts
        if isinstance(v, dict):
            v.update({f"{k}_count": len(x) for k, x in list(v.items()) if isinstance(x, list)})
    out = [{"id": f["id"], "type": "follow_up", "amount_overdue": money(f["amount_cents"], cur),
            "days_overdue_oldest": f["days_overdue"], "signals": signals}]
    for i in json.loads(f["evidence"]):
        p = i.split("-")[0]
        if p == "CUS":
            r = billing.execute("SELECT * FROM customers WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "customer", "name": r["legal_name"], "plan": r["plan"],
                        "mrr": money(r["mrr_cents"], cur), "payment_method": r["payment_method"],
                        "customer_since": r["customer_since"]})
        elif p == "INV":
            r = billing.execute("SELECT * FROM invoices WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "invoice", "issued": r["issue_date"], "due": r["due_date"],
                        "amount": money(r["amount_cents"], cur), "status": r["status"]})
        elif p == "PAY":
            r = billing.execute("SELECT * FROM payments WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "payment", "received": r["received_at"],
                        "amount": money(r["amount_cents"], r["currency"]), "status": r["status"],
                        "invoice": r["invoice_id"], "reference": r["reference"]})
        elif p == "CH":
            r = billing.execute("SELECT * FROM charge_attempts WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "card_charge", "attempted": r["attempted_at"],
                        "result": r["result"], "failure_code": r["failure_code"]})
        elif p == "CN":
            r = billing.execute("SELECT * FROM credit_notes WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "credit_note", "invoice": r["invoice_id"],
                        "amount": money(r["amount_cents"], cur), "reason": r["reason"]})
        elif p == "MSG":
            r = conn.execute("SELECT * FROM messages WHERE id = ?", (i,)).fetchone()
            out.append({"id": i, "type": "customer_reply", "received": r["received_at"][:10],
                        "body": r["body"]})
        elif r := conn.execute("SELECT object, data FROM sf_records WHERE id = ?", (i,)).fetchone():
            d = {k: v for k, v in json.loads(r["data"]).items()
                 if v is not None and k not in ("Id", "AccountId", "WhatId", "WhoId", "ContactId")}
            out.append({"id": i, "type": f"crm_{r['object'].lower()}", **d})
    return out


NUMBER = re.compile(r"(?<![\w-])\d[\d,]*(?:\.\d+)?")


def validate_brief(out: dict, fact_list: list[dict], conn: sqlite3.Connection,
                   billing: sqlite3.Connection, currency: str) -> list[str]:
    sentences = out.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        return ["no sentences"]
    ids = {x["id"] for x in fact_list}
    fact_numbers = {x.rstrip(".,") for x in NUMBER.findall(
        review.DATE.sub("", review.MONEY.sub("", json.dumps(fact_list, ensure_ascii=False))))}
    evidence = [i for i in ids if not i.startswith("FU-")]
    problems = []
    for n, s in enumerate(sentences, 1):
        text, cites = str(s.get("text", "")), s.get("cites") or []
        if not cites:
            problems.append(f"sentence {n} cites nothing")
        if unknown := [c for c in cites if c not in ids]:
            problems.append(f"sentence {n} cites unknown records {unknown}")
        records = [c for c in cites if c in ids and not c.startswith("FU-")]
        if any(c.startswith("FU-") for c in cites):
            records = evidence  # derived facts draw on all evidence
        if bad := review.check_numbers(text, conn, billing, records, currency):
            problems.append(f"sentence {n}: {bad} not in the cited records")
        rest = review.DATE.sub("", review.MONEY.sub("", text))
        stray = [x for x in NUMBER.findall(rest) if x.rstrip(".,") not in fact_numbers]
        if stray:
            problems.append(f"sentence {n}: numbers {stray} not in the input facts")
    return problems


def write_briefs(conn: sqlite3.Connection, billing: sqlite3.Connection,
                 followup_ids: list[str] | None = None) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM followups ORDER BY rank")
            if followup_ids is None or r["id"] in followup_ids]
    inputs = [(f, facts(conn, billing, f)) for f in rows]

    def one(item):
        f, fl = item
        try:
            out, meta = complete_json("brief", BRIEF_SYSTEM, json.dumps(fl, ensure_ascii=False))
            return f, out, meta
        except Exception as e:  # noqa: BLE001
            return f, {}, {"task": "brief", "model": "?", "ms": 0, "error": str(e)}

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(one, inputs))
    rejected = 0
    for (f, out, meta), (_, fl) in zip(results, inputs):
        log_call(conn, meta, f["id"])
        problems = ([f"call failed: {meta['error']}"] if meta.get("error")
                    else validate_brief(out, fl, conn, billing, f["currency"]))
        rejected += bool(problems)
        conn.execute("""INSERT INTO briefs (ts, followup_id, model, content, valid, problems)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                     (now(), f["id"], meta["model"], json.dumps(out), int(not problems),
                      json.dumps(problems)))
    conn.commit()
    return {"briefs": len(results), "rejected": rejected}


def log_call(conn: sqlite3.Connection, meta: dict, ref: str) -> None:
    conn.execute("""INSERT INTO llm_calls (ts, task, model, ref, prompt_tokens, completion_tokens,
                      ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (now(), meta["task"], meta["model"], ref, meta.get("prompt_tokens"),
                  meta.get("completion_tokens"), meta["ms"], meta.get("error")))


def latest_brief(conn: sqlite3.Connection, followup_id: str):
    return conn.execute("SELECT * FROM briefs WHERE followup_id = ? ORDER BY id DESC LIMIT 1",
                        (followup_id,)).fetchone()
