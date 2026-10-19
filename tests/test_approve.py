import pytest

from arfollow import review, store


def test_only_the_ar_specialist_on_record_may_decide(tmp_path):
    org = store.org(tmp_path / "org.db")
    with pytest.raises(ValueError, match="no AR Specialist"):  # fail closed
        review.authorize(org, "UAR")
    org.execute("INSERT INTO roles VALUES ('ar_specialist', 'UAR', NULL, 'now')")
    review.authorize(org, "UAR")
    with pytest.raises(ValueError, match="only"):
        review.authorize(org, "UBILLING")


def test_approving_a_stale_card_is_refused(stores):
    conn, billing, fus = stores
    f = next(x for x in fus if x["action"] == "escalate")
    shown = review.version(conn.execute("SELECT * FROM followups WHERE id = ?",
                                        (f["id"],)).fetchone())
    review.override(conn, billing, f["id"], "standard_reminder", "UAR", "ar", "customer called")
    with pytest.raises(ValueError, match="out of date"):
        review.decide(conn, billing, f["id"], "approve", "UAR", "ar", seen=shown)
    current = review.version(conn.execute("SELECT * FROM followups WHERE id = ?",
                                          (f["id"],)).fetchone())
    assert review.decide(conn, billing, f["id"], "approve", "UAR", "ar",
                         seen=current)["outbox_id"]


def test_two_clicks_one_decision(stores, tmp_path, monkeypatch):
    conn, billing, fus = stores
    fid = next(x["id"] for x in fus if x["action"] == "standard_reminder")
    other = store.app(tmp_path / "app.db")  # a second reviewer's click, on its own connection
    real, raced = review.check_numbers, []

    def racing_check(*a, **k):  # runs after the first click read the row as open
        if not raced:
            raced.append(None)
            raced[0] = review.decide(other, billing, fid, "approve", "UAR2", "ar2")
        return real(*a, **k)

    monkeypatch.setattr(review, "check_numbers", racing_check)
    with pytest.raises(ValueError, match="someone else"):
        review.decide(conn, billing, fid, "approve", "UAR", "ar")
    assert raced[0]["outbox_id"]
    assert conn.execute("SELECT count(*) FROM decisions WHERE followup_id = ?",
                        (fid,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox WHERE followup_id = ?",
                        (fid,)).fetchone()[0] == 1
