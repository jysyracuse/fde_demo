"""Step 7 acceptance.

HANDOFF: Slack 批准、修改、暂停按钮；审计日志记录谁、何时、批准了什么、依据是什么。验收：任一决定可追溯

Checks, on a copy of app.db so real data is untouched: approve / edit / pause each produce a
decision; an edit with a wrong amount is rejected; then every decision (including real ones made
in Slack) has who, when, what, evidence that resolves, an audit row, and an outbox row when a
reminder was approved; decisions cannot be altered afterwards.
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arfollow import config, review, rules, store  # noqa: E402


def main() -> int:
    if not config.APP_DB.exists():
        print("FAIL: no app.db. Run `uv run python -m arfollow.run` first.")
        return 1
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp, "app.db")
        shutil.copy(config.APP_DB, db)
        conn, billing = store.app(db), store.billing()
        rules.save_followups(conn, rules.build_followups(conn, billing))
        open_ = {r["action"]: r["id"] for r in conn.execute(
            "SELECT id, action FROM followups WHERE status = 'open' ORDER BY rank DESC")}
        if not {"standard_reminder", "gentle_reminder", "pause_and_resolve_dispute",
                "escalate"} <= set(open_):
            print("FAIL: need open follow-ups of each kind to exercise the buttons")
            return 1
        review.decide(conn, billing, open_["standard_reminder"], "approve", "UTEST", "accept")
        fid = open_["gentle_reminder"]
        body = conn.execute("SELECT draft_body FROM followups WHERE id = ?", (fid,)).fetchone()[0]
        try:
            review.decide(conn, billing, fid, "edit", "UTEST", "accept", body + "\nNZD 1.00")
            problems.append("edit with an amount not in the source was accepted")
        except ValueError:
            pass
        review.decide(conn, billing, fid, "edit", "UTEST", "accept", "Edited.\n" + body)
        review.decide(conn, billing, open_["pause_and_resolve_dispute"], "pause", "UTEST", "accept")
        fid = open_["escalate"]  # override, then approve the redrafted reminder
        review.override(conn, billing, fid, "standard_reminder", "UTEST", "accept", "customer called")
        review.decide(conn, billing, fid, "approve", "UTEST", "accept")
        for action, fid in open_.items():  # every action's own draft must be approvable
            if action in ("standard_reminder", "gentle_reminder", "pause_and_resolve_dispute",
                          "escalate"):
                continue
            try:
                review.decide(conn, billing, fid, "approve", "UTEST", "accept")
            except ValueError as e:
                problems.append(f"system draft for {action} rejected on approve: {e}")

        decisions = conn.execute("SELECT * FROM decisions").fetchall()
        for d in decisions:
            who = f"decision {d['id']}"
            if not (d["user_id"] and d["ts"] and d["action"] and d["text"]):
                problems.append(f"{who}: missing who/when/what")
            if bad := review.unresolved(conn, billing, json.loads(d["evidence"])):
                problems.append(f"{who}: evidence does not resolve: {bad}")
            if not conn.execute("SELECT 1 FROM audit_log WHERE json_extract(detail, "
                                "'$.decision_id') = ?", (d["id"],)).fetchone():
                problems.append(f"{who}: no audit row")
            if d["decision"] in ("approve", "edit") and d["action"] in rules.REMINDERS and not conn.execute(
                    "SELECT 1 FROM outbox WHERE decision_id = ?", (d["id"],)).fetchone():
                problems.append(f"{who}: approved reminder without outbox row")
        try:
            conn.execute("UPDATE decisions SET text = 'tampered'")
            problems.append("decisions table accepted an UPDATE")
        except sqlite3.IntegrityError:
            pass
        print(f"Decisions checked: {len(decisions)} (incl. "
              f"{sum(d['user_id'] != 'UTEST' for d in decisions)} made in Slack)")
    for p in problems:
        print(f"FAIL: {p}")
    print("step 7 PASS" if not problems else "step 7 FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
