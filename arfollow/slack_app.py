"""Slack review loop (Socket Mode): a digest, one card per follow-up, buttons that decide.

Run the listener with `uv run python -m arfollow.slack_app`; post a digest with
`uv run python -m arfollow.run --post`.
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import agent, config, llm_steps, review, store
from .rules import ACTIONS, REMINDERS, as_of, money

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
    "notify_account_owner": "Notify AM", "retention_handoff": "Hand off",
    "escalate": "Escalate", "gentle_reminder": "Send", "standard_reminder": "Send",
}
# Who takes over an internal action: org.db roles (scripts/roles.py); unset -> the approver.
ROLE = {
    "pause_and_resolve_dispute": "billing_ops_manager",
    "verify_payment_no_dunning": "billing_ops_manager", "escalate": "billing_ops_manager",
    "notify_account_owner": "account_manager", "retention_handoff": "account_manager",
}
ROLE_NAME = {"billing_ops_manager": "Billing Ops Manager", "account_manager": "Account Manager"}
# Handback outcomes the owner reports (ONTOLOGY.md §6). date_agreed asks for the date and keeps
# the case open until then; every other outcome closes it and the next run decides afresh.
HANDBACK = {
    "pause_and_resolve_dispute": {"upheld": "Dispute upheld, credit issued",
                                  "rejected": "Invoice correct, AR may remind"},
    "verify_payment_no_dunning": {"found": "Payment found and applied",
                                  "not_arrived": "Not arrived, AR may remind"},
    "escalate": {"date_agreed": "Payment date agreed…", "no_agreement": "No agreement, back to AR"},
    "notify_account_owner": {"renewal_settled": "Settled in the renewal",
                             "renewal_lost": "Renewal lost or stalled, back to AR"},
    "retention_handoff": {"saved": "Customer stays, back to AR",
                          "leaving": "Leaving, AR collects final balance"},
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
        {"type": "button", "action_id": "approve", "value": f"{f['id']}|{review.version(f)}",
         "style": "primary",
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


def cases_text(conn: sqlite3.Connection, billing: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM cases WHERE closed_at IS NULL ORDER BY opened_at").fetchall()
    if not rows:
        return ""
    who = names(billing)
    lines = [f"• {who.get(c['customer_id'], c['customer_id'])} `{c['customer_id']}`: "
             f"<@{c['owner']}> ({LABEL[c['action']].split(' ', 1)[1]}) since {c['opened_at'][:10]}"
             + (f", waiting until {c['wait_until']}" if c["wait_until"] else "")
             for c in rows]
    return f"\nWith owners, no new card ({len(rows)}):\n" + "\n".join(lines)


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
            + cases_text(conn, billing)
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


def handback_blocks(text: str, case_id: int, action: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": f"handback_{k}", "value": f"{case_id}:{k}",
                 "text": {"type": "plain_text", "text": label}}
                for k, label in HANDBACK[action].items()]}]


def dm_owner(client: WebClient, owner: str, f: dict, msg: str, case_id: int) -> None:
    try:
        link = client.chat_getPermalink(channel=f["slack_channel"], message_ts=f["slack_ts"])
        text = f"{msg}\n<{link['permalink']}|Open the card>"
        client.chat_postMessage(channel=owner, text=text,
                                blocks=handback_blocks(text, case_id, f["action"]))
    except SlackApiError as e:  # the thread mention still stands
        say(client, f, f"DM to <@{owner}> failed ({e.response['error']}); the thread mention "
                       "stands.")


def handle(client: WebClient, user: dict, decision: str, fid: str, text: str | None = None,
           seen: str | None = None):
    conn, billing = store.app(), store.billing()
    uid, uname = user["id"], user.get("username") or user.get("name", "")
    try:
        review.authorize(store.org(), uid)
        res = review.decide(conn, billing, fid, decision, uid, uname, text, seen)
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
        role = ROLE[f["action"]]
        owner = store.role_user(store.org(), role) or uid
        case_id = review.open_case(conn, fid, role, owner, uid)
        quoted = "\n".join(f">{line}" for line in res["text"].splitlines())
        msg = (f"{LABEL[f['action']]}: <@{owner}> ({ROLE_NAME[role]}), please take over *{name}* "
               f"(`{f['customer_id']}`), case #{case_id}. Approved by <@{uid}>. No new card "
               f"until you hand it back or something new arrives.\n{quoted}")
        if owner != uid:  # the thread is the record; the DM carries the handback buttons
            dm_owner(client, owner, f, msg, case_id)
        else:  # role unset: the approver holds it, buttons in the thread
            client.chat_postMessage(channel=f["slack_channel"], thread_ts=f["slack_thread"],
                                    text=msg, blocks=handback_blocks(msg, case_id, f["action"]))
            return
    say(client, f, msg)


def handle_override(client: WebClient, user: dict, fid: str, action: str, reason: str):
    conn, billing = store.app(), store.billing()
    uid, uname = user["id"], user.get("username") or user.get("name", "")
    try:
        review.authorize(store.org(), uid)
        res = review.override(conn, billing, fid, action, uid, uname, reason)
    except ValueError as e:
        store.audit(conn, f"slack:{uid}", "decision_override_rejected", fid, reason=str(e))
        conn.commit()
        f = conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone()
        say(client, f, f":no_entry: <@{uid}> `{fid}` not changed: {e}")
        return
    f = dict(conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone())
    name, blocks = render(conn, billing, f)
    client.chat_update(channel=f["slack_channel"], ts=f["slack_ts"], text=f"{name}: {action}",
                       blocks=blocks)
    say(client, f, f":leftwards_arrow_with_hook: <@{uid}> changed *{name}* from "
                   f"{LABEL[res['from']]} to {LABEL[res['to']]} (decision #{res['decision_id']}). "
                   f"Reason: {reason}. New draft is on the card for approval.")


def handle_handback(client: WebClient, uid: str, case_id: int, outcome: str, where: dict,
                    wait_until: str | None = None) -> None:
    conn, billing = store.app(), store.billing()
    try:
        c = review.handback(conn, billing, case_id, outcome, uid, wait_until)
    except ValueError as e:
        client.chat_postMessage(channel=uid, text=f":no_entry: Not handed back: {e}")
        return
    f = conn.execute("SELECT * FROM followups WHERE id = ?", (c["followup_id"],)).fetchone()
    name = names(billing)[c["customer_id"]]
    label = HANDBACK[c["action"]][outcome].rstrip("…")
    if wait_until:
        label += f": {wait_until}. No new card before then unless something new arrives"
    done = f":leftwards_arrow_with_hook: Case #{case_id} *{name}* handed back by <@{uid}>: {label}."
    client.chat_update(channel=where["channel"], ts=where["ts"], text=done,
                       blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": done}}])
    nxt = "" if wait_until else " Closed; the next run decides afresh from the source data."
    say(client, f, f"{done}{nxt} <@{c['opened_by']}>")


def modal(callback: str, fid: str, title: str, blocks: list[dict], submit: str) -> dict:
    return {"type": "modal", "callback_id": callback, "private_metadata": fid,
            "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": submit}, "blocks": blocks}


def gone(client: WebClient, body: dict, fid: str) -> bool:
    """A card from an earlier data set (app.db reset): mark it outdated instead of failing."""
    if store.app().execute("SELECT 1 FROM followups WHERE id = ?", (fid,)).fetchone():
        return False
    text = f":warning: `{fid}` is not in the current data set. This card is outdated."
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=text,
                       blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
    return True


def make_app() -> App:
    app = App(token=config.require("SLACK_BOT_TOKEN"))

    @app.action("approve")
    @app.action("pause")
    def on_decide(ack, body, action, client):
        ack()
        fid, _, seen = action["value"].partition("|")  # approve carries the card's version
        if not gone(client, body, fid):
            handle(client, body["user"], action["action_id"], fid,
                   seen=seen if action["action_id"] == "approve" else None)

    @app.action("edit")
    def on_edit(ack, body, action, client):
        ack()
        if gone(client, body, action["value"]):
            return
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
        if gone(client, body, action["value"]):
            return
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

    @app.action(re.compile(r"^handback_"))
    def on_handback(ack, body, action, client):
        ack()
        case_id, outcome = action["value"].split(":")
        where = {"channel": body["channel"]["id"], "ts": body["message"]["ts"]}
        if outcome == "date_agreed":
            client.views_open(trigger_id=body["trigger_id"], view=modal(
                "handback_date", json.dumps({"case": int(case_id), **where}),
                "Payment date agreed", [
                    {"type": "input", "block_id": "date", "label": {
                        "type": "plain_text", "text": "Customer will pay by"},
                     "element": {"type": "datepicker", "action_id": "value"}}], "Hand back"))
            return
        handle_handback(client, body["user"]["id"], int(case_id), outcome, where)

    @app.view("handback_date")
    def on_handback_date(ack, body, view, client):
        meta = json.loads(view["private_metadata"])
        day = view["state"]["values"]["date"]["value"]["selected_date"]
        if day <= as_of(store.billing()):
            ack(response_action="errors", errors={"date": "Pick a date after today"})
            return
        ack()
        handle_handback(client, body["user"]["id"], meta["case"], "date_agreed",
                        {"channel": meta["channel"], "ts": meta["ts"]}, day)

    return app


if __name__ == "__main__":
    print("Listening for review buttons (Socket Mode). Ctrl+C to stop.")
    SocketModeHandler(make_app(), config.require("SLACK_APP_TOKEN")).start()
