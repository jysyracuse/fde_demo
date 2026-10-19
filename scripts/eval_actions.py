"""Which follow-up action per overdue customer: rules vs DeepSeek vs Jev vs a human-style judge.

Offline, on a generated set (no Salesforce, no Slack, live app.db untouched):
  uv run python scripts/generate_data.py --set full --out data/eval/full
  uv run python scripts/eval_actions.py data/eval/full --dump     # cases.json for a blind judge
  uv run python scripts/eval_actions.py data/eval/full            # run arms, write results.json

Arms (same case packet, same playbook text from PLAYBOOK.md):
  rules      layer 1 (arfollow.rules), CRM linked by ground truth, reply intent = keyword baseline
             (generated sets only: needs billing.json)
  rules_pkt  the playbook checks as code on the packet fields, reply intent = keyword baseline;
             written before the holdout was seen
  deepseek   one chat call: playbook + case -> {"action", "reason"}
  jev_choice one Jev Choice over the actions, option descriptions = playbook action sections
  jev_checks one Jev Noul per playbook check; code applies the playbook order (first match wins)
  claude     read from <data>/claude.json if present (judged blind from cases.json)
Truth = labels.json expected_action (the generator's design intent), or for a set given as
packets (cases.json + truth.json, e.g. the Pi-written holdout) truth.json action.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from arfollow import config, llm, rules, store  # noqa: E402
from load_db import load  # noqa: E402

PLAYBOOK = config.ROOT / "PLAYBOOK.md"
CHECK_ORDER = [("dispute", "pause_and_resolve_dispute"),
               ("payment_claimed", "verify_payment_no_dunning"),
               ("renewal_open", "notify_account_owner"), ("churn_risk", "retention_handoff"),
               ("chronic_late", "escalate"), ("card_expired", "gentle_reminder")]
ACTIONS = [a for _, a in CHECK_ORDER] + ["standard_reminder", "needs_human"]
NOUL_YES = 0.5


def playbook() -> tuple[str, dict[str, str], dict[str, str]]:
    text = PLAYBOOK.read_text()

    def section(name: str) -> dict[str, str]:
        body = text.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]
        return {k.strip(): " ".join(v.split()) for k, v in
                (p.split("\n", 1) for p in body.split("### ")[1:])}
    return text, section("Checks"), section("Actions")


def money(x: float, cur: str) -> str:
    c = round(x * 100)
    return f"{cur} {c // 100:,}.{c % 100:02d}"


def cases(data: Path) -> tuple[list[dict], dict[str, str]]:
    """One packet per overdue customer: raw evidence plus facts computed in code."""
    b = json.loads((data / "billing.json").read_text())
    sf = {a["ref"]: a for a in json.loads((data / "salesforce.json").read_text())["accounts"]}
    labels = json.loads((data / "labels.json").read_text())["customers"]
    inbox_labels = json.loads((data / "inbox_labels.json").read_text())
    inbox = json.loads((data / "inbox.json").read_text())
    today = date.fromisoformat(b["as_of"])
    inv_by = defaultdict(list)
    for i in b["invoices"]:
        inv_by[i["customer_id"]].append(i)
    pay_by = defaultdict(list)
    for p in b["payments"]:
        pay_by[p["invoice_id"]].append(p)
    ch_by = defaultdict(list)
    for ch in b["charge_attempts"]:
        ch_by[ch["invoice_id"]].append(ch)
    cn_by = defaultdict(list)
    for cn in b["credit_notes"]:
        cn_by[cn["invoice_id"]].append(cn)
    unapplied = [p for p in b["payments"] if p["status"] == "unapplied"]
    usage = defaultdict(list)
    for u in sorted(b["usage_monthly"], key=lambda u: u["month"]):
        usage[u["customer_id"]].append(u["active_users"])
    replies = defaultdict(list)
    for m in sorted(inbox, key=lambda m: m["received_at"]):
        replies[inbox_labels[m["id"]]["customer_id"]].append(
            {"id": m["id"], "received": m["received_at"][:10], "subject": m["subject"],
             "body": m["body"]})

    out, truth = [], {}
    for c in b["customers"]:
        cid, cur = c["id"], c["currency"]
        overdue = sorted((i for i in inv_by[cid] if i["status"] == "open"
                          and date.fromisoformat(i["due_date"]) < today),
                         key=lambda i: i["due_date"])
        if not overdue:
            continue
        inv_rows, total = [], 0.0
        for i in overdue:
            credits = cn_by[i["id"]]
            balance = i["amount"] - sum(x["amount"] for x in credits)
            total += balance
            last = max(ch_by[i["id"]], key=lambda x: x["attempted_at"], default=None)
            inv_rows.append({
                "id": i["id"], "issued": i["issue_date"], "due": i["due_date"],
                "amount": money(i["amount"], cur),
                "days_overdue": (today - date.fromisoformat(i["due_date"])).days,
                "credit_notes": [{"id": x["id"], "amount": money(x["amount"], cur),
                                  "reason": x["reason"]} for x in credits],
                "balance": money(balance, cur),
                "latest_charge_attempt": last and {
                    "date": last["attempted_at"], "result": last["result"],
                    "failure_code": last["failure_code"]}})
        year_ago = today - timedelta(days=365)
        paid = [(i, p) for i in inv_by[cid] for p in pay_by[i["id"]]
                if p["status"] == "applied" and date.fromisoformat(p["received_at"]) >= year_ago]
        late = [1 for i, p in paid if (date.fromisoformat(p["received_at"])
                                       - date.fromisoformat(i["due_date"])).days > 10]
        amounts = {round(i["amount"] * 100) for i in overdue}
        acc = sf[labels[cid]["sf_ref"]]
        tasks = []
        for t in acc["tasks"]:
            recent = date.fromisoformat(t["ActivityDate"]) >= today - timedelta(days=180)
            m = re.search(r"by (\d{4}-\d{2}-\d{2})", t.get("Description") or "")
            if not (recent or m):
                continue
            row = {k: t.get(k) for k in ("Subject", "Status", "ActivityDate", "Description")}
            if m:
                row["promised_date"] = m[1]
                row["promised_date_passed"] = date.fromisoformat(m[1]) < today
            tasks.append(row)
        u = usage[cid]
        out.append({
            "customer_id": cid, "as_of": b["as_of"],
            "customer": {"legal_name": c["legal_name"], "plan": c["plan"],
                         "mrr": money(c["mrr"], cur), "payment_method": c["payment_method"],
                         "card_expiry": c["card_expiry"],
                         "card_status": ("no card on file" if not c["card_expiry"] else
                                         "expired" if c["card_expiry"] < b["as_of"] else "valid"),
                         "customer_since": c["customer_since"]},
            "overdue_invoices": inv_rows, "total_overdue_balance": money(total, cur),
            "payment_history": {"invoices_paid_last_12_months": len(paid),
                                "paid_over_10_days_late": len(late)},
            "unapplied_payments_same_amount": [
                {"id": p["id"], "amount": money(p["amount"], p["currency"]),
                 "received": p["received_at"], "reference": p["reference"]}
                for p in unapplied if round(p["amount"] * 100) in amounts
                and p["currency"] == cur],
            "usage": {"active_users_by_month": u,
                      "latest_pct_of_first": round(100 * u[-1] / u[0]) if u and u[0] else None},
            "crm": {"account_name": acc["fields"]["Name"],
                    "opportunities": [{k: o.get(k) for k in ("Name", "Type", "StageName",
                                                             "CloseDate", "Description")}
                                      for o in acc["opportunities"]],
                    "cases": [{k: x.get(k) for k in ("Subject", "Status", "Priority",
                                                     "Description")} for x in acc["cases"]],
                    "recent_activities": tasks},
            "replies": replies[cid]})
        truth[cid] = labels[cid]["expected_action"]
    return out, truth


def cents(m: str) -> int:
    return round(float(m.split(" ")[1].replace(",", "")) * 100)


def check_packets(packets: list[dict], truth: dict[str, str]) -> list[str]:
    """Invariants of the computed fields (PACKET_FORMAT); a set given as packets is untrusted."""
    problems, inv_ids = [], Counter(i["id"] for p in packets for i in p["overdue_invoices"])
    problems += [f"duplicate invoice {i}" for i, n in inv_ids.items() if n > 1]
    if set(truth) != {p["customer_id"] for p in packets}:
        problems.append("truth keys differ from case ids")
    problems += [f"{k}: unknown action {v}" for k, v in truth.items() if v not in ACTIONS]
    for p in packets:
        cid, today, c = p["customer_id"], date.fromisoformat(p["as_of"]), p["customer"]
        total = 0
        for i in p["overdue_invoices"]:
            if (today - date.fromisoformat(i["due"])).days != i["days_overdue"]:
                problems.append(f"{cid} {i['id']}: days_overdue")
            if i["days_overdue"] <= 0:
                problems.append(f"{cid} {i['id']}: not overdue")
            bal = cents(i["amount"]) - sum(cents(x["amount"]) for x in i["credit_notes"])
            if bal != cents(i["balance"]):
                problems.append(f"{cid} {i['id']}: balance")
            total += bal
        if total != cents(p["total_overdue_balance"]):
            problems.append(f"{cid}: total_overdue_balance")
        want = ("no card on file" if not c["card_expiry"] else
                "expired" if c["card_expiry"] < p["as_of"] else "valid")
        if c["card_status"] != want:
            problems.append(f"{cid}: card_status")
        u = p["usage"]["active_users_by_month"]
        if u and u[0] and round(100 * u[-1] / u[0]) != p["usage"]["latest_pct_of_first"]:
            problems.append(f"{cid}: latest_pct_of_first")
        for t in p["crm"]["recent_activities"]:
            if "promised_date" in t and t.get("promised_date_passed") != (
                    t["promised_date"] < p["as_of"]):
                problems.append(f"{cid}: promised_date_passed")
        late = p["payment_history"]
        if late["paid_over_10_days_late"] > late["invoices_paid_last_12_months"]:
            problems.append(f"{cid}: more late payments than payments")
    return problems


def arm_rules(data: Path) -> dict[str, str]:
    """Layer 1 on this set, offline: Salesforce snapshot and entity links from the files."""
    sf = json.loads((data / "salesforce.json").read_text())["accounts"]
    labels = json.loads((data / "labels.json").read_text())["customers"]
    inbox_labels = json.loads((data / "inbox_labels.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        load(data, Path(tmp, "billing.db"))
        conn = store.app(Path(tmp, "app.db"))
        for a in sf:
            closed = {"opportunities": lambda r: r["StageName"].startswith("Closed"),
                      "cases": lambda r: r["Status"] == "Closed"}
            conn.execute("INSERT INTO sf_records VALUES ('Account', ?, ?, ?)",
                         (a["ref"], a["ref"], json.dumps(a["fields"])))
            for key, obj in (("contacts", "Contact"), ("opportunities", "Opportunity"),
                             ("cases", "Case"), ("tasks", "Task")):
                for n, r in enumerate(a[key]):
                    if key in closed:
                        r = {**r, "IsClosed": closed[key](r)}
                    conn.execute("INSERT INTO sf_records VALUES (?, ?, ?, ?)",
                                 (obj, f"{a['ref']}-{obj}-{n}", a["ref"], json.dumps(r)))
        conn.executemany("INSERT INTO matches VALUES (?, ?, 'truth', 'auto', '[]')",
                         [(cid, v["sf_ref"]) for cid, v in labels.items()])
        for m in json.loads((data / "inbox.json").read_text()):
            conn.execute("INSERT INTO messages VALUES (?, ?, 'truth', ?, ?, ?, ?, ?)",
                         (m["id"], inbox_labels[m["id"]]["customer_id"],
                          rules.classify(m["body"]), m["received_at"], m["from_email"],
                          m["subject"], m["body"]))
        fus = rules.build_followups(conn, store.billing(Path(tmp, "billing.db")))
        conn.close()
    return {f["customer_id"]: f["action"] for f in fus}


def arm_rules_packet(case: dict) -> str:
    """PLAYBOOK.md checks in order, on packet fields; reply intent from the keyword baseline."""
    intents = {rules.classify(m["body"]) for m in case["replies"]}
    crm, inv = case["crm"], case["overdue_invoices"]
    nums = [i["id"][4:] for i in inv]
    pct = case["usage"]["latest_pct_of_first"]
    hit = {
        "dispute": "dispute" in intents or any(
            x["Subject"].startswith("[Billing]") and x["Status"] != "Closed" for x in crm["cases"]),
        "payment_claimed": "paid" in intents or any(
            any(n in (p["reference"] or "") for n in nums)
            for p in case["unapplied_payments_same_amount"]),
        "renewal_open": any("Renewal" in (o["Type"] or "")
                            and not (o["StageName"] or "").startswith("Closed")
                            for o in crm["opportunities"]),
        "churn_risk": "cancel" in intents or (pct is not None and pct < 60),
        "chronic_late": case["payment_history"]["paid_over_10_days_late"] >= 4 or any(
            t.get("promised_date_passed") for t in crm["recent_activities"]),
        "card_expired": any((i["latest_charge_attempt"] or {}).get("failure_code")
                            == "expired_card" for i in inv),
    }
    return next((a for k, a in CHECK_ORDER if hit[k]), "standard_reminder")


def arm_deepseek(case: dict, text: str) -> dict:
    system = (text + "\n\nReturn JSON only: {\"action\": one of " + json.dumps(ACTIONS)
              + ", \"reason\": one sentence naming the evidence}.")
    t0 = time.monotonic()
    out, meta = llm.complete_json("decide_action", system, json.dumps(case, ensure_ascii=False))
    return {"action": out.get("action"), "reason": out.get("reason"),
            "ms": round((time.monotonic() - t0) * 1000),
            "tokens": (meta.get("prompt_tokens") or 0) + (meta.get("completion_tokens") or 0)}


def arm_jev(client, case: dict, checks: dict, actions: dict) -> dict:
    from typesafe_sdk import Choice, Noul
    questions = {"action": Choice(
        instructions="Which follow-up action the playbook prescribes for this overdue customer. "
                     "The checks are applied in order and the first that matches decides: "
                     "dispute, payment claimed, open renewal, churn risk, chronic late payer, "
                     "expired card; otherwise a standard reminder.",
        criteria=actions)}
    questions |= {f"check_{k}": Noul(instructions=v) for k, v in checks.items()}
    t0 = time.monotonic()
    r = client.system_one(state=case, questions=questions)
    ms = round((time.monotonic() - t0) * 1000)
    a = r.choices["action"]
    nouls = {k: round(r.nouls[f"check_{k}"].noul, 3) for k in checks}
    chained = next((act for k, act in CHECK_ORDER if nouls[k] >= NOUL_YES), "standard_reminder")
    usage = r.usage.model_dump() if r.usage else {}
    return {"choice": {"action": a.choice, "confidence": round(a.confidence, 3),
                       "probabilities": {k: round(v, 3) for k, v in a.probabilities.items()
                                         if v >= 0.01}},
            "checks": {"action": chained, "nouls": nouls}, "ms": ms, "usage": usage}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("--dump", action="store_true", help="write cases.json (no truth) and stop")
    args = ap.parse_args()
    text, checks, actions = playbook()
    generated = (args.data / "billing.json").exists()
    if generated:
        packets, truth = cases(args.data)
    else:  # a set given as packets
        packets = json.loads((args.data / "cases.json").read_text())
        truth = {k: v["action"] for k, v in json.loads((args.data / "truth.json").read_text())
                 .items()}
        if problems := check_packets(packets, truth):
            print("FAIL: packets inconsistent\n  " + "\n  ".join(problems))
            return 1
    if args.dump and generated:
        (args.data / "cases.json").write_text(json.dumps(packets, indent=1, ensure_ascii=False))
        print(f"Wrote {len(packets)} cases to {args.data / 'cases.json'}")
        return 0

    from typesafe_sdk import TypeSafeClient
    results = {p["customer_id"]: {"truth": truth[p["customer_id"]]} for p in packets}
    for cid, act in (arm_rules(args.data) if generated else {}).items():
        if cid in results:
            results[cid]["rules"] = act
    for p in packets:
        results[p["customer_id"]]["rules_pkt"] = arm_rules_packet(p)
    with TypeSafeClient(model="jev-latest", api_key=config.require("TYPESAFE_API_KEY")) as jev, \
            ThreadPoolExecutor(8) as pool:
        ds = {p["customer_id"]: pool.submit(arm_deepseek, p, text) for p in packets}
        jv = {p["customer_id"]: pool.submit(arm_jev, jev, p, checks, actions) for p in packets}
        for cid in results:
            results[cid]["deepseek"] = ds[cid].result()
            results[cid]["jev"] = jv[cid].result()
    judge = args.data / "claude.json"
    if judge.exists():
        for cid, v in json.loads(judge.read_text()).items():
            results[cid]["claude"] = v
    (args.data / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    report(results)
    return 0


def pick(r: dict, arm: str) -> str | None:
    v = r.get(arm.split(".")[0])
    return {"deepseek": lambda: v["action"], "jev.choice": lambda: v["choice"]["action"],
            "jev.checks": lambda: v["checks"]["action"],
            "claude": lambda: v["action"] if isinstance(v, dict) else v}.get(
        arm, lambda: v)() if v else None


def report(results: dict) -> None:
    arms = ["rules", "rules_pkt", "deepseek", "jev.choice", "jev.checks", "claude"]
    n = len(results)
    print(f"Cases: {n}\n")
    print(f"{'arm':12s} {'correct':>9s} {'needs_human':>12s}")
    for arm in arms:
        got = [pick(r, arm) for r in results.values()]
        if all(g is None for g in got):
            continue
        ok = sum(g == r["truth"] for g, r in zip(got, results.values()))
        print(f"{arm:12s} {ok:>4d}/{n:<4d} {got.count('needs_human'):>12d}")
    print("\nPer truth category (correct):")
    per = defaultdict(Counter)
    for r in results.values():
        for arm in arms:
            per[r["truth"]][arm] += pick(r, arm) == r["truth"]
    cats = Counter(r["truth"] for r in results.values())
    print(f"{'truth':28s} {'n':>3s} " + " ".join(f"{a:>10s}" for a in arms))
    for t, k in sorted(cats.items()):
        print(f"{t:28s} {k:>3d} " + " ".join(f"{per[t][a]:>10d}" for a in arms))
    ms = [r["jev"]["ms"] for r in results.values()]
    dms = [r["deepseek"]["ms"] for r in results.values()]
    print(f"\nLatency median: deepseek {sorted(dms)[n // 2]} ms, jev {sorted(ms)[n // 2]} ms "
          f"(one jev call answers the choice and all checks)")
    print(f"DeepSeek tokens total: {sum(r['deepseek']['tokens'] for r in results.values())}")


if __name__ == "__main__":
    raise SystemExit(main())
