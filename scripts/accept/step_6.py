"""Step 6 acceptance.

HANDOFF: 只读 MCP server（查工单、付款、发票明细、沟通记录）；步数与预算上限；输出结论、建议和证据 ID。验收：争议案例给出可追溯的建议

Offline, on the stored agent runs (run `uv run python -m arfollow.run --offline --agent` first):
- every dispute follow-up has a run that is valid, finished within MAX_STEPS and MAX_TOKENS;
- disputed invoice and recommendation match ground truth (labels + billing, not the agent's tools);
- the validator is proven both ways (L-003): real answers pass; every injected error (amount,
  date, computed count, unretrieved or unknown citation, tool name as citation, wrong invoice)
  is rejected, 100 %;
- agent share: dispute runs / all follow-ups (layer 3 stays the exception).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arfollow import agent, config, store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step_5 import bump_date, bump_money  # noqa: E402


def truth(billing, labels: dict, customer_id: str) -> tuple[str, str]:
    """Disputed invoice = the overdue one billing the most users; recommendation from labels."""
    lab = labels[customer_id]
    seats = {i: billing.execute("SELECT quantity FROM invoice_lines WHERE invoice_id = ? AND "
                                "description = 'Additional users'", (i,)).fetchone()[0]
             for i in lab["overdue_invoice_ids"]}
    full = any("covers the full" in n for n in lab["notes"])
    return max(seats, key=seats.get), ("close_case_resume_collection" if full
                                       else "issue_credit_note")


def injections(out: dict, other_invoice: str) -> list[tuple[str, dict]]:
    res = []

    def variant(kind, fn):
        b = json.loads(json.dumps(out))
        if fn(b) is not False:
            res.append((kind, b))
    for kind, bump in (("amount", bump_money), ("date", bump_date)):
        n = next((i for i, f in enumerate(out["findings"]) if bump(f["text"])), None)
        if n is not None:
            variant(kind, lambda b, n=n, bump=bump: b["findings"][n].update(
                text=bump(b["findings"][n]["text"])))
    variant("count", lambda b: b["findings"][0].update(
        text=b["findings"][0]["text"] + " This is the 47th disputed invoice."))
    variant("unretrieved citation", lambda b: b["findings"][0]["cites"].append(other_invoice))
    variant("unknown citation", lambda b: b["findings"][0]["cites"].append("INV-999999"))
    variant("tool name as citation", lambda b: b["findings"][0]["cites"].append("compare_seats"))
    variant("wrong invoice", lambda b: b.update(disputed_invoice=other_invoice))
    variant("no citation", lambda b: b["findings"][0].update(cites=[]))
    return res


def main() -> int:
    conn, billing = store.app(), store.billing()
    labels = json.loads((config.DATA_DIR / "labels.json").read_text())["customers"]
    disputes = conn.execute("SELECT * FROM followups WHERE action = 'pause_and_resolve_dispute'"
                            ).fetchall()
    total = conn.execute("SELECT count(*) FROM followups").fetchone()[0]
    problems, tried, caught, correct = [], 0, 0, 0
    for f in disputes:
        r = agent.latest_run(conn, f["id"])
        if r is None:
            problems.append(f"{f['id']}: no agent run (run `arfollow.run --offline --agent`)")
            continue
        out, transcript = json.loads(r["output"]), json.loads(r["transcript"])
        if r["steps"] > agent.MAX_STEPS or r["tokens"] > agent.MAX_TOKENS or r["stop"] != "done":
            problems.append(f"{f['id']}: limits: steps {r['steps']}, tokens {r['tokens']}, "
                            f"stop {r['stop']}")
        if p := agent.validate_investigation(out, transcript):
            problems.append(f"{f['id']}: real answer rejected: {p}")
            continue
        inv, rec = truth(billing, labels, f["customer_id"])
        if (out["disputed_invoice"], out["recommendation"]) == (inv, rec):
            correct += 1
        else:
            problems.append(f"{f['id']}: got {out['disputed_invoice']}/{out['recommendation']}, "
                            f"expected {inv}/{rec}")
        other = billing.execute("SELECT id FROM invoices WHERE customer_id != ? LIMIT 1",
                                (f["customer_id"],)).fetchone()[0]
        for kind, bad in injections(out, other):
            tried += 1
            if agent.validate_investigation(bad, transcript):
                caught += 1
            else:
                problems.append(f"{f['id']}: injected {kind} passed the validator")
    runs = [agent.latest_run(conn, f["id"]) for f in disputes]
    runs = [r for r in runs if r]
    if runs:
        print(f"Disputes {len(disputes)}: correct {correct}/{len(disputes)}; steps max "
              f"{max(r['steps'] for r in runs)}/{agent.MAX_STEPS}; tokens max "
              f"{max(r['tokens'] for r in runs)}/{agent.MAX_TOKENS}; repaired "
              f"{sum(r['repairs'] > 0 for r in runs)}")
    print(f"Injected errors caught {caught}/{tried}; agent share {len(disputes)}/{total}")
    for p in problems:
        print(f"FAIL: {p}")
    print("step 6 PASS" if not problems else "step 6 FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
