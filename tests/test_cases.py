import pytest

from arfollow import review, rules


def carded(conn, billing) -> dict:
    return {f["customer_id"]: f for f in rules.build_followups(conn, billing)}


def test_case_holds_the_customer_until_new_evidence_or_the_agreed_date(stores):
    conn, billing, fus = stores
    f = next(x for x in fus if x["action"] == "escalate")
    cus = f["customer_id"]
    case = review.open_case(conn, f["id"], "billing_ops_manager", "UBILLING", "UAR")
    assert cus not in carded(conn, billing)  # handed off, nothing new: no daily re-ping

    with pytest.raises(ValueError, match="only"):
        review.handback(conn, billing, case, "date_agreed", "UAR", "2099-01-01")
    with pytest.raises(ValueError, match="after"):
        review.handback(conn, billing, case, "date_agreed", "UBILLING", rules.as_of(billing))
    review.handback(conn, billing, case, "date_agreed", "UBILLING", "2099-01-01")
    assert cus not in carded(conn, billing)  # waiting for the agreed date

    conn.execute("UPDATE cases SET wait_until = '2000-01-01' WHERE id = ?", (case,))
    back = carded(conn, billing)[cus]  # agreed date passed, still overdue: escalate again
    assert back["action"] == "escalate" and back["signals"]["chronic_late"]["agreed_date_passed"]

    conn.execute("UPDATE cases SET wait_until = NULL, evidence = '[]' WHERE id = ?", (case,))
    assert cus in carded(conn, billing)  # something new arrived: card despite the open case

    review.handback(conn, billing, case, "no_agreement", "UBILLING")
    assert cus in carded(conn, billing)
    with pytest.raises(ValueError, match="not open"):
        review.handback(conn, billing, case, "no_agreement", "UBILLING")


def test_missed_agreed_date_escalates_an_otherwise_standard_customer(stores):
    conn, billing, fus = stores
    f = next(x for x in fus if x["action"] == "standard_reminder")
    case = review.open_case(conn, f["id"], "billing_ops_manager", "UBILLING", "UAR")
    conn.execute("UPDATE cases SET wait_until = '2000-01-01' WHERE id = ?", (case,))
    assert carded(conn, billing)[f["customer_id"]]["action"] == "escalate"


def test_case_closes_when_the_customer_is_no_longer_overdue(stores):
    conn, billing, fus = stores
    overdue = {f["customer_id"] for f in fus}
    paid_up = next(r[0] for r in billing.execute("SELECT id FROM customers")
                   if r[0] not in overdue)
    rules.save_followups(conn, [{**fus[0], "id": "FU-test", "customer_id": paid_up}])
    case = review.open_case(conn, "FU-test", "account_manager", "UACCOUNT", "UAR")
    review.open_case(conn, fus[1]["id"], "billing_ops_manager", "UBILLING", "UAR")
    assert review.close_settled_cases(conn, billing) == 1
    assert conn.execute("SELECT outcome FROM cases WHERE id = ?", (case,)).fetchone()[0] == \
        "no_longer_overdue"
