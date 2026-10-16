"""Step 6: narrow, read-only MCP server the layer-3 agent investigates with.

Read-only by architecture: both databases are opened with SQLite `mode=ro` and no tool writes.
Every tool is scoped to one customer or invoice and returns record IDs, so the agent's evidence
is traceable. Money is pre-formatted ("AUD 80.00") and arithmetic is done here, not by the model.

Run: uv run python -m arfollow.mcp_server   (stdio; the agent launches it as a subprocess)
"""
import json
import sqlite3
from collections import Counter

from mcp.server.mcpserver import MCPServer

from . import config, store
from .rules import money

server = MCPServer("arfollow-readonly", instructions="Read-only AR records for one customer.")


def _app() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.APP_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _currency(billing: sqlite3.Connection, customer_id: str) -> str:
    r = billing.execute("SELECT currency FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown customer {customer_id}")
    return r["currency"]


def _sf(customer_id: str, obj: str) -> list[dict]:
    conn = _app()
    m = conn.execute("SELECT account_id FROM matches WHERE customer_id = ? AND status = 'auto'",
                     (customer_id,)).fetchone()
    if m is None:
        return []
    rows = conn.execute("SELECT data FROM sf_records WHERE object = ? AND account_id = ?",
                        (obj, m["account_id"]))
    drop = ("AccountId", "WhatId", "WhoId", "ContactId")
    return [{k: v for k, v in json.loads(r["data"]).items() if k not in drop} for r in rows]


@server.tool()
def get_customer(customer_id: str) -> dict:
    """Billing profile of a customer (CUS-...): plan, payment method, currency, MRR."""
    b = store.billing()
    r = b.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown customer {customer_id}")
    return {"id": r["id"], "name": r["legal_name"], "plan": r["plan"], "currency": r["currency"],
            "mrr": money(r["mrr_cents"], r["currency"]), "payment_method": r["payment_method"],
            "customer_since": r["customer_since"]}


@server.tool()
def list_invoices(customer_id: str) -> list[dict]:
    """The customer's 12 most recent invoices (newest first) with status."""
    b = store.billing()
    cur = _currency(b, customer_id)
    rows = b.execute("SELECT * FROM invoices WHERE customer_id = ? ORDER BY issue_date DESC "
                     "LIMIT 12", (customer_id,))
    return [{"id": r["id"], "issued": r["issue_date"], "due": r["due_date"],
             "amount": money(r["amount_cents"], cur), "status": r["status"]} for r in rows]


@server.tool()
def get_invoice_lines(invoice_id: str) -> dict:
    """Line items of one invoice (INV-...): description, quantity, unit price, amount."""
    b = store.billing()
    inv = b.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    cur = inv["currency"]
    lines = b.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
                      (invoice_id,))
    return {"invoice": invoice_id, "amount": money(inv["amount_cents"], cur), "lines": [
        {"description": r["description"], "quantity": r["quantity"],
         "unit_price": money(r["unit_price_cents"], cur), "amount": money(r["amount_cents"], cur)}
        for r in lines]}


@server.tool()
def compare_seats(invoice_id: str) -> dict:
    """Users billed on an invoice vs the usual count on the customer's earlier invoices, the
    resulting over-billing, credit notes already issued against it, and what remains uncredited."""
    b = store.billing()
    inv = b.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    cur = inv["currency"]
    seats = """SELECT l.quantity, l.unit_price_cents FROM invoice_lines l
               WHERE l.invoice_id = ? AND l.description = 'Additional users'"""
    billed = b.execute(seats, (invoice_id,)).fetchone()
    prior = [b.execute(seats, (r["id"],)).fetchone()["quantity"] for r in b.execute(
        "SELECT id FROM invoices WHERE customer_id = ? AND issue_date < ? ORDER BY issue_date "
        "DESC LIMIT 6", (inv["customer_id"], inv["issue_date"]))]
    usual = Counter(prior).most_common(1)[0][0] if prior else billed["quantity"]
    over = max(billed["quantity"] - usual, 0) * billed["unit_price_cents"]
    credits = [dict(r) for r in b.execute(
        "SELECT id, amount_cents, reason, issued_at FROM credit_notes WHERE invoice_id = ?",
        (invoice_id,))]
    credited = sum(c["amount_cents"] for c in credits)
    return {"invoice": invoice_id, "users_billed": billed["quantity"],
            "usual_users_on_earlier_invoices": usual, "earlier_invoices_compared": len(prior),
            "extra_users": max(billed["quantity"] - usual, 0),
            "unit_price": money(billed["unit_price_cents"], cur),
            "over_billed": money(over, cur),
            "credit_notes": [{"id": c["id"], "amount": money(c["amount_cents"], cur),
                              "reason": c["reason"], "issued": c["issued_at"]} for c in credits],
            "credited": money(credited, cur),
            "uncredited_over_billing": money(max(over - credited, 0), cur)}


@server.tool()
def list_payments(customer_id: str) -> list[dict]:
    """The customer's 12 most recent payments, with the invoice each was applied to."""
    b = store.billing()
    rows = b.execute("SELECT * FROM payments WHERE customer_id = ? ORDER BY received_at DESC "
                     "LIMIT 12", (customer_id,))
    return [{"id": r["id"], "received": r["received_at"], "invoice": r["invoice_id"],
             "amount": money(r["amount_cents"], r["currency"]), "status": r["status"]}
            for r in rows]


@server.tool()
def list_credit_notes(customer_id: str) -> list[dict]:
    """Credit notes issued to the customer."""
    b = store.billing()
    cur = _currency(b, customer_id)
    rows = b.execute("SELECT * FROM credit_notes WHERE customer_id = ?", (customer_id,))
    return [{"id": r["id"], "invoice": r["invoice_id"], "amount": money(r["amount_cents"], cur),
             "reason": r["reason"], "issued": r["issued_at"]} for r in rows]


@server.tool()
def list_crm_cases(customer_id: str) -> list[dict]:
    """Salesforce support cases (tickets) on the customer's matched account."""
    return _sf(customer_id, "Case")


@server.tool()
def list_crm_activities(customer_id: str) -> list[dict]:
    """Salesforce activity history (calls, emails) on the customer's matched account."""
    return sorted(_sf(customer_id, "Task"), key=lambda t: t.get("ActivityDate") or "",
                  reverse=True)[:12]


@server.tool()
def list_replies(customer_id: str) -> list[dict]:
    """Customer email replies in the AR inbox."""
    rows = _app().execute("SELECT id, received_at, subject, body FROM messages "
                          "WHERE customer_id = ? ORDER BY received_at", (customer_id,))
    return [{"id": r["id"], "received": r["received_at"][:10], "subject": r["subject"],
             "body": r["body"]} for r in rows]


if __name__ == "__main__":
    server.run("stdio")
