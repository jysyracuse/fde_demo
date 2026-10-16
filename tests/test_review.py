import json
import sqlite3

from arfollow.review import BILLING_IDS, check_numbers
from arfollow.rules import classify, draft, money


def stores(invoices, sf=()):
    billing = sqlite3.connect(":memory:")
    billing.row_factory = sqlite3.Row
    for t in BILLING_IDS.values():
        billing.execute(f"CREATE TABLE {t} (id)")
    billing.execute("DROP TABLE invoices")
    billing.execute("CREATE TABLE invoices (id, customer_id, issue_date, due_date, amount_cents, "
                    "currency, status)")
    billing.executemany("INSERT INTO invoices VALUES (?, 'C', ?, ?, ?, 'NZD', 'open')", invoices)
    app = sqlite3.connect(":memory:")
    app.row_factory = sqlite3.Row
    app.execute("CREATE TABLE sf_records (object, id, account_id, data)")
    app.execute("CREATE TABLE messages (id, received_at, subject, body)")
    app.execute("INSERT INTO messages VALUES ('MSG-1', '2026-10-11T09:04:00', 'RE', "
                "'Payment of NZD 99.00 goes out on 15 October 2026.')")
    app.executemany("INSERT INTO sf_records VALUES ('Opportunity', ?, 'A', ?)", sf)
    return app, billing


def test_money_formats_cents_exactly():
    assert money(123456789, "NZD") == "NZD 1,234,567.89"
    assert money(5, "AUD") == "AUD 0.05"


def test_check_numbers_accepts_source_values_and_rejects_others():
    app, billing = stores([("INV-1", "2026-09-01", "2026-09-15", 14900),
                           ("INV-2", "2026-10-01", "2026-10-15", 10000)],
                          [("006A", json.dumps({"CloseDate": "2026-11-07"}))])
    ev = ["CUS-1", "INV-1", "INV-2", "006A", "MSG-1"]
    ok = ("Pay NZD 149.00 due 2026-09-15, total NZD 249.00; renewal closes 2026-11-07. "
          "Reply on 2026-10-11 promised NZD 99.00 on October 15, 2026 (FU-2026-01-01-C).")
    assert check_numbers(ok, app, billing, ev, "NZD") == []
    assert check_numbers("Pay NZD 194.00 by 2026-09-16 or 16 October 2026", app, billing, ev,
                         "NZD") == ["NZD 194.00", "2026-09-16", "16 October 2026"]


def test_check_numbers_accepts_balance_net_of_cited_credit_note():
    app, billing = stores([("INV-1", "2026-09-01", "2026-09-15", 14900),
                           ("INV-2", "2026-10-01", "2026-10-15", 6900)])
    billing.execute("DROP TABLE credit_notes")
    billing.execute("CREATE TABLE credit_notes (id, invoice_id, amount_cents)")
    billing.execute("INSERT INTO credit_notes VALUES ('CN-1', 'INV-1', 8000)")
    ev = ["INV-1", "INV-2", "CN-1"]
    assert check_numbers("Owed NZD 69.00 on INV-1, NZD 138.00 in total", app, billing, ev,
                         "NZD") == []
    assert check_numbers("Owed NZD 139.00", app, billing, ev, "NZD") == ["NZD 139.00"]
    # without the credit note cited, the net figure is not supported
    assert check_numbers("NZD 138.00", app, billing, ["INV-1", "INV-2"], "NZD") == ["NZD 138.00"]


def test_reply_intent_baseline():
    assert classify("Sorry, this slipped through. Will get it paid by Friday.") == "promise_to_pay"
    assert classify("We paid INV-1 by bank transfer on 3 October 2026.") == "paid"
    assert classify("Please cancel our account at the end of this month.") == "cancel"


def test_gentle_reminder_claims_expired_card_only_with_that_signal():
    c = {"currency": "USD", "billing_email": "ap@x.example"}
    inv = [{"id": "INV-1", "due_date": "2026-09-23", "amount_cents": 362500,
            "balance_cents": 362500, "credits": []}]
    card = {"card_expired": "2026-08"}
    assert "expired" in draft("gentle_reminder", c, {}, inv, card)["draft_body"]
    # reviewer override on a bank-transfer customer: no card claim
    assert "expired" not in draft("gentle_reminder", c, {}, inv, {})["draft_body"]
