"""Step 5 acceptance.

HANDOFF: 简报（引用记录 ID）、数字校验、回复分类（已付、争议、承诺付款含日期、取消、无法判断）、起草邮件。验收：故意注入错误数字时被拦截

Offline, on the stored LLM outputs (run `uv run python -m arfollow.run --llm` first):
- injection: every valid brief, with one amount, date, citation or count corrupted, is rejected
  (100 % interception); every valid reply label with a forged quote or past promise date too;
- reply intent accuracy >= 90 % (README target), reported against the keyword baseline.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arfollow import config, review, store  # noqa: E402
from arfollow.llm_steps import facts, validate_brief, validate_reply  # noqa: E402
from arfollow.rules import classify  # noqa: E402


def bump_money(text: str) -> str | None:
    m = review.MONEY.search(text)
    if not m:
        return None
    cur, num = m[0].split(" ")
    cents = round(float(num.replace(",", "")) * 100) + 100
    return text.replace(m[0], f"{cur} {cents // 100:,}.{cents % 100:02d}", 1)


def bump_date(text: str) -> str | None:
    found = review.dates_in(text)
    if not found:
        return None
    written, iso = found[0]
    return text.replace(written, (date.fromisoformat(iso) + timedelta(days=1)).isoformat(), 1)


def injections(brief: dict) -> list[tuple[str, dict]]:
    out = []
    for kind, fn in (("amount", bump_money), ("date", bump_date)):
        for n, s in enumerate(brief["sentences"]):
            if new := fn(s["text"]):
                b = json.loads(json.dumps(brief))
                b["sentences"][n]["text"] = new
                out.append((kind, b))
                break
    b = json.loads(json.dumps(brief))
    b["sentences"][0]["cites"].append("INV-999999")
    out.append(("citation", b))
    b = json.loads(json.dumps(brief))
    b["sentences"][0]["text"] += " The customer has 47 open disputes."
    out.append(("count", b))
    return out


def main() -> int:
    conn, billing = store.app(), store.billing()
    briefs = conn.execute("SELECT * FROM briefs WHERE id IN "
                          "(SELECT max(id) FROM briefs GROUP BY followup_id)").fetchall()
    replies = conn.execute("""SELECT r.*, m.body, m.received_at
                              FROM reply_labels r JOIN messages m ON m.id = r.message_id
                              WHERE r.id IN (SELECT max(id) FROM reply_labels
                                             GROUP BY message_id)""").fetchall()
    if not briefs or not replies:
        print("FAIL: no LLM outputs. Run `uv run python -m arfollow.run --llm` first.")
        return 1
    problems, tried, caught = [], 0, 0
    fus = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM followups")}
    valid = [b for b in briefs if b["valid"]]
    for b in valid:
        f = fus[b["followup_id"]]
        fl = facts(conn, billing, f)
        for kind, bad in injections(json.loads(b["content"])):
            tried += 1
            if validate_brief(bad, fl, conn, billing, f["currency"]):
                caught += 1
            else:
                problems.append(f"{b['followup_id']}: injected {kind} error passed the validator")
    for r in replies:
        if not r["valid"]:
            continue
        for kind, forged in (("quote", {**dict(r), "quote": "We paid in full yesterday."}),
                             ("promise", {**dict(r), "promise_date": "2020-01-01"})):
            tried += 1
            if validate_reply(forged, r["body"], r["received_at"]):
                caught += 1
            else:
                problems.append(f"{r['message_id']}: forged {kind} passed the validator")
    print(f"Briefs valid {len(valid)}/{len(briefs)}; injected errors caught {caught}/{tried}")

    truth = json.loads((config.DATA_DIR / "inbox_labels.json").read_text())
    llm = sum(r["intent"] == truth[r["message_id"]]["intent"] for r in replies) / len(replies)
    kw = sum(classify(r["body"]) == truth[r["message_id"]]["intent"] for r in replies)
    dates = [r for r in replies if truth[r["message_id"]]["promise_date"]]
    exact = sum(r["promise_date"] == truth[r["message_id"]]["promise_date"] for r in dates)
    print(f"Reply intent: LLM {llm:.0%}, keyword baseline {kw}/{len(replies)}; "
          f"promise dates exact {exact}/{len(dates)}")
    if llm < 0.9:
        problems.append(f"reply intent accuracy {llm:.0%} < 90 % (kill criterion: 85 %)")
    for p in problems:
        print(f"FAIL: {p}")
    print("step 5 PASS" if not problems else "step 5 FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
