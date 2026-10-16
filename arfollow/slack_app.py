"""Slack review loop (Socket Mode): a digest, one card per follow-up, buttons that decide.

Run the listener with `uv run python -m arfollow.slack_app`; post a digest with
`uv run python -m arfollow.run --post`.
"""
import json
import sqlite3
from collections import Counter, defaultdict

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import agent, config, llm_steps, review, store
from .rules import ACTIONS, REMINDERS, money

LABEL = {
    "pause_and_resolve_dispute": ":double_vertical_bar: Pause dunning, resolve dispute",
    "verify_payment_no_dunning": ":mag: Verify payment, no reminder",
    "notify_account_owner": ":handshake: Notify account owner (renewal)",
    "retention_handoff": ":warning: Retention handoff",
    "escalate": ":rotating_light: Escalate",
    "gentle_reminder": ":credit_card: Gentle reminder (card expired)",
    "standard_reminder": ":envelope: Standard reminder",
}
APPROVE_TEXT = {
    "pause_and_resolve_dispute": "Confirm pause", "verify_payment_no_dunning": "Assign check",
    "notify_account_owner": "Notify AE", "retention_handoff": "Hand off",
    "escalate": "Escalate", "gentle_reminder": "Send", "standard_reminder": "Send",
}
# Who takes over an internal action. Unset env var -> the approving reviewer (demo default).
ROLE = {
    "pause_and_resolve_dispute": ("billing ops", "SLACK_USER_BILLING_OPS"),
    "verify_payment_no_dunning": ("billing ops", "SLACK_USER_BILLING_OPS"),
    "notify_account_owner": ("account owner", "SLACK_USER_ACCOUNT_OWNER"),
    "retention_handoff": ("retention", "SLACK_USER_RETENTION"),
    "escalate": ("finance lead", "SLACK_USER_FINANCE_LEAD"),
}
KIND = {"CUS": "Customer", "INV": "Invoices", "PAY": "Payments", "CH": "Charges",
        "CN": "Credit notes", "MSG": "Replies"}


def evidence_text(conn: sqlite3.Connection, billing: sqlite3.Connection, ids: list[str]) -> str:
    """Evidence IDs grouped by kind, with the key fact for each; Salesforce IDs link to the org."""
    sf_host = config.optional("SF_DOMAIN").removeprefix("https://").rstrip("/")
    groups = defaultdict(list)
    for i in ids:
        prefix = i.split("-")[0]
        if prefix == "INV" and (r := billing.execute(
                "SELECT due_date, amount_cents, currency FROM invoices WHERE id = ?",
                (i,)).fetchone()):
            groups["Invoices"].append(f"{i} due {r[0]} {money(r[1], r[2])}")
        elif prefix == "PAY" and (r := billing.execute(
                """SELECT p.received_at, p.amount_cents, p.currency, p.status,
                     CAST(julianday(p.received_at) - julianday(i.due_date) AS INTEGER)
                   FROM payments p LEFT JOIN invoices i ON i.id = p.invoice_id WHERE p.id = ?""",
                (i,)).fetchone()):
            when = f"{r[4]} days late" if r[4] and r[4] > 0 else r[3]
            groups["Payments"].append(f"{i} paid {r[0]} {money(r[1], r[2])} ({when})")
        elif prefix == "CH" and (r := billing.execute(
                "SELECT attempted_at, failure_code FROM charge_attempts WHERE id = ?",
                (i,)).fetchone()):
            groups["Charges"].append(f"{i} failed {r[0]} ({r[1]})")
        elif prefix == "MSG" and (r := conn.execute(
                "SELECT received_at, intent FROM messages WHERE id = ?", (i,)).fetchone()):
            groups["Replies"].append(f"{i} {r[0][:10]} ({r[1]})")
        elif prefix in KIND:
            groups[KIND[prefix]].append(i)
        elif r := conn.execute("SELECT object, data FROM sf_records WHERE id = ?", (i,)).fetchone():
            d = json.loads(r[1])
            label = (d.get("Name") or d.get("Subject") or r[0])[:45]
            groups[f"Salesforce {r[0]}"].append(f"<https://{sf_host}/{i}|{label}>" if sf_host
                                                else f"{i} {label}")
        else:
            groups["Other"].append(i)
    return "\n".join(f"*{k}:* " + ", ".join(v) for k, v in groups.items())[:2900]


def brief_text(conn: sqlite3.Connection, fid: str) -> str | None:
    b = llm_steps.latest_brief(conn, fid)
    if b is None:
        return None
    if not b["valid"]:
        first = json.loads(b["problems"])[0]
        return f":warning: _Brief rejected by validator ({b['model']}):_ {first}"
    lines = [f"• {s['text']} _[{', '.join(s['cites'])}]_"
             for s in json.loads(b["content"])["sentences"]]
    return f"*Brief* _({b['model']}, validated)_\n" + "\n".join(lines)


def investigation_text(conn: sqlite3.Connection, fid: str) -> str | None:
    r = agent.latest_run(conn, fid)
    if r is None:
        return None
    head = f"({r['model']}, {r['steps']} tool rounds, {r['tokens']:,} tokens)"
    if not r["valid"]:
        return f":warning: *Agent investigation rejected* _{head}:_ {json.loads(r['problems'])[0]}"
    out = json.loads(r["output"])
    lines = [f"• {x['text']} _[{', '.join(x['cites'])}]_" for x in out["findings"]]
    return (f"*:mag_right: Agent investigation* _{head}, validated_\n"
            f"Disputed `{out['disputed_invoice']}` → *{out['recommendation'].replace('_', ' ')}*\n"
            + "\n".join(lines))


def card_blocks(f: dict, name: str, evidence: str, outcome: str | None = None,
                brief: str | None = None) -> list[dict]:
    sig = json.loads(f["signals"])
    head = (f"*#{f['rank']} {name}*  `{f['customer_id']}`\n*{LABEL[f['action']]}*\n"
            f"{money(f['amount_cents'], f['currency'])} overdue, oldest {f['days_overdue']} days")
    kind = f"Draft to {f['draft_to']}" if f["draft_to"] else "Internal note"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"*Why:* {', '.join(sig) or 'overdue only'}"}]},
        *([{"type": "section", "text": {"type": "mrkdwn", "text": brief[:2900]}}] if brief else []),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": evidence}]},
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"_{kind}_\n```{f['draft_body'][:2800]}```"}},
    ]
    if outcome:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]})
        return blocks
    edit = "Edit & send" if f["action"] in REMINDERS else "Edit note"
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": "approve", "value": f["id"], "style": "primary",
         "text": {"type": "plain_text", "text": APPROVE_TEXT[f["action"]]}},
        {"type": "button", "action_id": "edit", "value": f["id"],
         "text": {"type": "plain_text", "text": edit}},
        {"type": "button", "action_id": "change", "value": f["id"],
         "text": {"type": "plain_text", "text": "Change action"}},
        {"type": "button", "action_id": "pause", "value": f["id"], "style": "danger",
         "text": {"type": "plain_text", "text": "Hold"}},
    ]})
    return blocks


def render(conn: sqlite3.Connection, billing: sqlite3.Connection, f: dict,
           outcome: str | None = None) -> tuple[str, list[dict]]:
    name = names(billing)[f["customer_id"]]
    ev = evidence_text(conn, billing, json.loads(f["evidence"]))
    extra = "\n\n".join(filter(None, [investigation_text(conn, f["id"]),
                                       brief_text(conn, f["id"])]))
    return name, card_blocks(f, name, ev, outcome, extra or None)


def names(billing: sqlite3.Connection) -> dict[str, str]:
    return dict(billing.execute("SELECT id, legal_name FROM customers"))


def post_digest(conn: sqlite3.Connection, billing: sqlite3.Connection, client: WebClient,
                channel: str, limit: int) -> int:
    for f in conn.execute("SELECT * FROM followups WHERE status = 'open' AND slack_ts IS NOT NULL"):
        name, blocks = render(conn, billing, dict(f))  # refresh posted cards (new brief, action)
        client.chat_update(channel=f["slack_channel"], ts=f["slack_ts"], text=name, blocks=blocks)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM followups WHERE status = 'open' AND slack_ts IS NULL ORDER BY rank")]
    if not rows:
        return 0
    totals = defaultdict(int)
    for f in rows:
        totals[f["currency"]] += f["amount_cents"]
    by_action = Counter(f["action"] for f in rows)
    review_q = conn.execute("SELECT count(*) FROM matches WHERE status = 'review'").fetchone()[0]
    shown = rows[:limit]
    text = (f"*AR follow-up digest, as of {rows[0]['as_of']}*\n"
            f"{len(rows)} customers need follow-up: "
            + ", ".join(money(c, cur) for cur, c in sorted(totals.items())) + "\n"
            + "\n".join(f"• {LABEL[a]}: {n}" for a, n in by_action.most_common())
            + f"\nEntity matches awaiting review: {review_q}"
            + f"\nTop {len(shown)} by priority in the thread :thread:")
    parent = client.chat_postMessage(channel=channel, text=text)["ts"]
    for f in shown:
        name, blocks = render(conn, billing, f)
        ts = client.chat_postMessage(channel=channel, thread_ts=parent, blocks=blocks,
                                     text=f"{name}: {f['action']}")["ts"]
        conn.execute("UPDATE followups SET slack_channel = ?, slack_thread = ?, slack_ts = ? "
                     "WHERE id = ?", (channel, parent, ts, f["id"]))
    store.audit(conn, "system:slack", "digest_posted", digest_ts=parent, cards=len(shown),
                total=len(rows))
    conn.commit()
    return len(shown)


# --- listener ----------------------------------------------------------------------------------
def say(client: WebClient, f, text: str) -> None:
    client.chat_postMessage(channel=f["slack_channel"], thread_ts=f["slack_thread"], text=text)


def simulated_inbox(client: WebClient, uid: str, f: dict, body: str) -> str:
    """DM the approver a copy of the email, standing in for the customer's inbox."""
    try:
        client.chat_postMessage(channel=uid, text=(
            f":incoming_envelope: *Simulated customer inbox* (not sent)\n"
            f"To: {f['draft_to']}\nFrom: {config.AR_EMAIL}\nSubject: {f['draft_subject']}\n"
            f"```{body[:2800]}```"))
        return "copy in your DMs"
    except SlackApiError as e:  # App Home messages tab off: the thread copy still stands
        return f"DM copy failed ({e.response['error']}); enable the App Home messages tab"


def handle(client: WebClient, user: dict, decision: str, fid: str, text: str | None = None):
    conn, billing = store.app(), store.billing()
    uid, uname = user["id"], user.get("username") or user.get("name", "")
    try:
        res = review.decide(conn, billing, fid, decision, uid, uname, text)
    except ValueError as e:  # rejected: tell the reviewer in the thread instead of failing silently
        store.audit(conn, f"slack:{uid}", f"decision_{decision}_rejected", fid, reason=str(e))
        conn.commit()
        f = conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone()
        say(client, f, f":no_entry: <@{uid}> `{fid}` not {decision}d: {e}")
        return
    f = res["followup"]
    if "already" in res:
        say(client, f, f"`{fid}` was already {res['already']}; nothing changed.")
        return
    f = dict(conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone())
    f["draft_body"] = res["text"]
    verb = {"approve": "Approved", "edit": "Edited and approved", "pause": "Held"}[decision]
    name, blocks = render(conn, billing, f, f"{verb} by <@{uid}> · decision #{res['decision_id']}")
    client.chat_update(channel=f["slack_channel"], ts=f["slack_ts"], text=f"{name}: {verb}",
                       blocks=blocks)
    if res["outbox_id"]:
        where = simulated_inbox(client, uid, f, res["text"])
        msg = (f":outbox_tray: *Sent (simulated)* to {f['draft_to']}, outbox #{res['outbox_id']} "
               f"({where})")
    elif decision == "pause":
        msg = f":double_vertical_bar: *{name}* held by <@{uid}>. No action taken."
    else:
        role, env = ROLE[f["action"]]
        owner = config.optional(env) or uid
        quoted = "\n".join(f">{line}" for line in res["text"].splitlines())
        msg = (f"{LABEL[f['action']]}: <@{owner}> ({role}), please take over *{name}* "
               f"(`{f['customer_id']}`). Approved by <@{uid}>.\n{quoted}")
    say(client, f, msg)


def handle_override(client: WebClient, user: dict, fid: str, action: str, reason: str):
    conn, billing = store.app(), store.billing()
    uid, uname = user["id"], user.get("username") or user.get("name", "")
    res = review.override(conn, billing, fid, action, uid, uname, reason)
    f = dict(conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone())
    name, blocks = render(conn, billing, f)
    client.chat_update(channel=f["slack_channel"], ts=f["slack_ts"], text=f"{name}: {action}",
                       blocks=blocks)
    say(client, f, f":leftwards_arrow_with_hook: <@{uid}> changed *{name}* from "
                   f"{LABEL[res['from']]} to {LABEL[res['to']]} (decision #{res['decision_id']}). "
                   f"Reason: {reason}. New draft is on the card for approval.")


def modal(callback: str, fid: str, title: str, blocks: list[dict], submit: str) -> dict:
    return {"type": "modal", "callback_id": callback, "private_metadata": fid,
            "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": submit}, "blocks": blocks}


def make_app() -> App:
    app = App(token=config.require("SLACK_BOT_TOKEN"))

    @app.action("approve")
    @app.action("pause")
    def on_decide(ack, body, action, client):
        ack()
        handle(client, body["user"], action["action_id"], action["value"])

    @app.action("edit")
    def on_edit(ack, body, action, client):
        ack()
        f = store.app().execute("SELECT draft_body FROM followups WHERE id = ?",
                                (action["value"],)).fetchone()
        client.views_open(trigger_id=body["trigger_id"], view=modal(
            "edit_submit", action["value"], "Edit and approve",
            [{"type": "input", "block_id": "draft",
              "label": {"type": "plain_text", "text": "Draft"},
              "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                          "initial_value": f["draft_body"]}}], "Approve"))

    @app.view("edit_submit")
    def on_edit_submit(ack, body, view, client):
        fid, text = view["private_metadata"], view["state"]["values"]["draft"]["text"]["value"]
        conn = store.app()
        f = conn.execute("SELECT evidence, currency FROM followups WHERE id = ?",
                         (fid,)).fetchone()
        if bad := review.check_numbers(text, conn, store.billing(), json.loads(f["evidence"]),
                                       f["currency"]):
            ack(response_action="errors", errors={"draft": "Not in source records: "
                                                  + ", ".join(bad)})
            return
        ack()
        handle(client, body["user"], "edit", fid, text)

    @app.action("change")
    def on_change(ack, body, action, client):
        ack()
        current = store.app().execute("SELECT action FROM followups WHERE id = ?",
                                      (action["value"],)).fetchone()[0]
        options = [{"text": {"type": "plain_text", "text": LABEL[a].split(": ", 1)[-1][:75]},
                    "value": a} for a in ACTIONS if a != current]
        client.views_open(trigger_id=body["trigger_id"], view=modal(
            "change_submit", action["value"], "Change action", [
                {"type": "input", "block_id": "action", "label": {
                    "type": "plain_text", "text": f"Instead of: {current.replace('_', ' ')}"},
                 "element": {"type": "static_select", "action_id": "value", "options": options}},
                {"type": "input", "block_id": "reason", "label": {
                    "type": "plain_text", "text": "Why (kept in the audit log)"},
                 "element": {"type": "plain_text_input", "action_id": "value"}}], "Change"))

    @app.view("change_submit")
    def on_change_submit(ack, body, view, client):
        ack()
        v = view["state"]["values"]
        handle_override(client, body["user"], view["private_metadata"],
                        v["action"]["value"]["selected_option"]["value"],
                        v["reason"]["value"]["value"])

    return app


if __name__ == "__main__":
    print("Listening for review buttons (Socket Mode). Ctrl+C to stop.")
    SocketModeHandler(make_app(), config.require("SLACK_APP_TOKEN")).start()
