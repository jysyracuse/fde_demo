import sqlite3

import pytest

from arfollow import store


def test_role_lookup_and_unknown_role_rejected(tmp_path):
    conn = store.org(tmp_path / "org.db")
    assert store.role_user(conn, "billing_ops_manager") is None  # caller falls back to the approver
    conn.execute("INSERT INTO roles VALUES ('billing_ops_manager', 'U1', 'b@x.example', 'now')")
    assert store.role_user(conn, "billing_ops_manager") == "U1"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO roles VALUES ('janitor', 'U2', NULL, 'now')")
