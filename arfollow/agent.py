"""Step 6 (layer 3): an agent investigates dispute follow-ups through the read-only MCP server.

Only disputes reach it: their path differs per case (which invoice, is the case vague, is there
already a credit note). Everything else stays in layers 1-2. Hard limits: MAX_STEPS tool rounds,
MAX_TOKENS per run; hitting either ends the run as needs_human.

Validator (traceability): the answer parses as `Investigation`; every cited ID and the disputed
invoice appeared in this run's tool results; every amount, date and other number in a finding
appears in those results too. Arithmetic goes through the local `calculate` tool (code, exact
cents), whose operands must be values already retrieved; its result then counts as retrieved.
A rejected answer gets one repair round (the problems are sent
back, no tools). The transcript of tool calls is stored, so each claim can be
traced to the exact tool output it came from.
"""
import ast
import asyncio
import json
import re
import sqlite3
import sys
import time
from typing import Literal

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel, Field, ValidationError

from . import llm, review
from .llm_steps import NUMBER, log_call
from .rules import money
from .store import now

MAX_STEPS = 8
MAX_TOKENS = 60_000
MAX_REPAIRS = 1

SYSTEM = """You investigate a disputed accounts-receivable case with read-only tools.
Choose which records to read; stop as soon as the records settle the dispute.
Then reply with JSON only:
{"disputed_invoice": "INV-...",
 "recommendation": "issue_credit_note" | "close_case_resume_collection" | "needs_human",
 "findings": [{"text": "...", "cites": ["<record id>", ...]}]}
- issue_credit_note: the over-billing is real and not yet fully credited.
- close_case_resume_collection: credit notes already cover the over-billing; close the CRM case
  and collect the balance net of credit notes (state it, computed with calculate).
- needs_human: the records do not settle it.
3 to 5 findings, each citing the record IDs it relies on (INV-, CN-, PAY-, MSG-, Salesforce Id)
as seen in tool results; never cite tool names. Copy money, dates and counts exactly as the
tools return them. For any sum, difference or product (e.g. a balance after a credit note) call
calculate; never do arithmetic yourself."""

CALCULATE = {"type": "function", "function": {
    "name": "calculate",
    "description": "Exact arithmetic with + - * and parentheses. Operands are money exactly as a "
                   "tool returned it (e.g. 'AUD 149.00 - AUD 80.00') or counts from tool results.",
    "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                   "required": ["expression"]}}}


def calculate(expression: str, retrieved: str) -> dict:
    """Code, not the model, does the arithmetic: integer cents, + - * only, and every operand
    must already appear in `retrieved` (earlier tool results), so the result stays traceable."""
    curs = set(re.findall(r"([A-Z]{3}) [\d,]+\.\d{2}", expression))
    if len(curs) > 1:
        raise ValueError(f"mixed currencies {sorted(curs)}")
    seen_money = set(review.MONEY.findall(retrieved))
    plain = review.DATE.sub("", review.MONEY.sub("", retrieved))
    seen_nums = {x.rstrip(".,") for x in NUMBER.findall(plain)}
    if bad := [a for a in review.MONEY.findall(expression) if a not in seen_money]:
        raise ValueError(f"operands not in retrieved records: {bad}")
    nums = [n.rstrip(".,") for n in NUMBER.findall(review.MONEY.sub("", expression))]
    if bad := [n for n in nums if n not in seen_nums]:
        raise ValueError(f"operands not in retrieved records: {bad}")
    # money -> ("m", cents) marker the evaluator understands; exact, no floats
    expr = review.MONEY.sub(lambda m: f"M({m[0][4:].replace(',', '').replace('.', '')})",
                            expression)

    def ev(n) -> tuple[int, bool]:  # (value, is money)
        if isinstance(n, ast.Constant) and type(n.value) is int:
            return n.value, False
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "M"
                and len(n.args) == 1 and isinstance(n.args[0], ast.Constant)):
            return int(n.args[0].value), True
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            v, m = ev(n.operand)
            return -v, m
        if isinstance(n, ast.BinOp) and type(n.op) in (ast.Add, ast.Sub, ast.Mult):
            (a, am), (b, bm) = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Mult):
                if am and bm:
                    raise ValueError("cannot multiply money by money")
                return a * b, am or bm
            if am != bm:
                raise ValueError("cannot add a count to money")
            return (a + b if isinstance(n.op, ast.Add) else a - b), am
        raise ValueError(f"unsupported syntax: {ast.dump(n)[:60]}")
    try:
        value, is_money = ev(ast.parse(expr, mode="eval").body)
    except SyntaxError as e:
        raise ValueError(f"cannot parse {expression!r}") from e
    return {"expression": expression,
            "result": money(value, curs.pop()) if is_money else value}


class Finding(BaseModel):
    text: str = Field(min_length=1)
    cites: list[str] = Field(min_length=1)


class Investigation(BaseModel):
    disputed_invoice: str
    recommendation: Literal["issue_credit_note", "close_case_resume_collection", "needs_human"]
    findings: list[Finding] = Field(min_length=1, max_length=6)


def seen_ids(transcript: list[dict]) -> set[str]:
    """Record IDs the tools returned (any id/Id/invoice field, at any depth)."""
    out = set()

    def walk(x):
        if isinstance(x, dict):
            out.update(str(v) for k, v in x.items() if k in ("id", "Id", "invoice") and v)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for t in transcript:
        try:
            walk(json.loads(t["result"]))
        except ValueError:
            pass
    return out


def validate_investigation(out: dict, transcript: list[dict]) -> list[str]:
    try:
        inv = Investigation.model_validate(out)
    except ValidationError as e:
        return [f"schema: {err['loc']} {err['msg']}" for err in e.errors()][:3]
    ids = seen_ids(transcript)
    blob = "\n".join(t["result"] for t in transcript)
    amounts = set(review.MONEY.findall(blob))
    dates = {iso for _, iso in review.dates_in(blob)}
    plain = review.DATE.sub("", review.MONEY.sub("", blob))
    numbers = {x.rstrip(".,") for x in NUMBER.findall(plain)}
    problems = []
    if inv.disputed_invoice not in ids:
        problems.append(f"disputed_invoice {inv.disputed_invoice} not in tool results")
    for n, f in enumerate(inv.findings, 1):
        if unseen := [c for c in f.cites if c not in ids]:
            problems.append(f"finding {n} cites records not retrieved: {unseen}")
        bad = [a for a in review.MONEY.findall(f.text) if a not in amounts]
        bad += [w for w, iso in review.dates_in(f.text) if iso not in dates]
        rest = review.DATE.sub("", review.MONEY.sub("", f.text))
        bad += [x for x in NUMBER.findall(rest) if x.rstrip(".,") not in numbers]
        if bad:
            problems.append(f"finding {n}: {bad} not in tool results")
    return problems


def _json(text: str | None) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m[0]) if m else {}


async def investigate(f: dict, conn: sqlite3.Connection) -> dict:
    """One bounded run. Returns the agent_runs row (not yet inserted)."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "arfollow.mcp_server"])
    t0, tokens, steps, transcript, out, stop = time.monotonic(), 0, 0, [], {}, "done"
    repairs, problems = 0, []
    model = llm.MODELS["investigate"][1]
    async with Client(params) as client:
        tools = [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in (await client.list_tools()).tools] + [CALCULATE]
        signals = json.loads(f["signals"])
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({
            "follow_up": f["id"], "customer_id": f["customer_id"],
            "overdue_invoices": [i for i in json.loads(f["evidence"]) if i.startswith("INV-")],
            "signals": signals})}]
        while True:
            last = steps >= MAX_STEPS or repairs > 0
            if steps == MAX_STEPS and not repairs:
                messages.append({"role": "user", "content": "Step limit reached. Answer now."})
            try:
                msg, meta = await asyncio.to_thread(
                    llm.chat, "investigate", messages, None if last else tools, json_mode=False)
            except Exception as e:  # noqa: BLE001  route to a human with the error
                stop = "error"
                log_call(conn, {"task": "investigate", "model": model, "ms": 0, "error": str(e)},
                         f["id"])
                break
            log_call(conn, meta, f["id"])
            model = meta["model"]
            tokens += (meta.get("prompt_tokens") or 0) + (meta.get("completion_tokens") or 0)
            if tokens > MAX_TOKENS:
                stop = "budget"
                break
            calls = msg.get("tool_calls") or []
            if not calls:
                out = _json(msg.get("content"))
                if steps >= MAX_STEPS:
                    stop = "step_limit"
                problems = validate_investigation(out, transcript)
                if not problems or repairs >= MAX_REPAIRS:
                    break
                repairs += 1
                messages += [{"role": "assistant", "content": msg.get("content") or ""},
                             {"role": "user", "content": "Validation failed: " + "; ".join(
                                 problems) + ". Return the full JSON again, fixing only these."}]
                continue
            steps += 1
            keep = ("role", "content", "tool_calls")
            messages.append({k: v for k, v in msg.items() if k in keep})
            for call in calls:
                name, args = call["function"]["name"], call["function"]["arguments"]
                try:
                    if name == "calculate":  # local code tool, not data access
                        result = json.dumps(calculate(
                            json.loads(args)["expression"],
                            "\n".join(t["result"] for t in transcript)))
                    else:
                        r = await client.call_tool(name, json.loads(args or "{}"))
                        texts = [c.text for c in r.content if hasattr(c, "text")]
                        # MCP sends a list as one block per item: rejoin as a JSON array
                        result = texts[0] if len(texts) == 1 else f"[{','.join(texts)}]"
                except Exception as e:  # noqa: BLE001  bad arguments: the model sees the error
                    result = f"error: {e}"
                transcript.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    if stop not in ("done", "step_limit"):
        problems = [f"run ended: {stop}"]
    return {"ts": now(), "followup_id": f["id"], "model": model, "steps": steps,
            "repairs": repairs,
            "tokens": tokens, "ms": round((time.monotonic() - t0) * 1000), "stop": stop,
            "transcript": json.dumps(transcript), "output": json.dumps(out),
            "valid": int(not problems), "problems": json.dumps(problems)}


def run_disputes(conn: sqlite3.Connection, followup_ids: list[str] | None = None) -> dict:
    """Investigate every open dispute follow-up (or the given ones) and store the runs."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM followups WHERE action = 'pause_and_resolve_dispute' ORDER BY rank")
        if followup_ids is None or r["id"] in followup_ids]

    async def all_runs():
        return await asyncio.gather(*(investigate(f, conn) for f in rows))
    runs = asyncio.run(all_runs())
    for r in runs:
        conn.execute(f"INSERT INTO agent_runs ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                     tuple(r.values()))
    conn.commit()
    total = conn.execute("SELECT count(*) FROM followups").fetchone()[0]
    return {"investigated": len(runs), "valid": sum(r["valid"] for r in runs),
            "agent_share": f"{len(runs)}/{total}"}


def latest_run(conn: sqlite3.Connection, followup_id: str):
    return conn.execute("SELECT * FROM agent_runs WHERE followup_id = ? ORDER BY id DESC LIMIT 1",
                        (followup_id,)).fetchone()
