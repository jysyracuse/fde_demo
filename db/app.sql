-- The system's own store (the only database it writes). Idempotent: applied on every connect.
-- decisions, outbox and audit_log are append-only, enforced by triggers.

CREATE TABLE IF NOT EXISTS sf_records (     -- read-only Salesforce snapshot, replaced each sync
    object      TEXT NOT NULL,              -- Account / Contact / Opportunity / Case / Task
    id          TEXT PRIMARY KEY,
    account_id  TEXT,
    data        TEXT NOT NULL               -- JSON record as returned by the REST API
) STRICT;

CREATE TABLE IF NOT EXISTS matches (        -- billing customer -> Salesforce account
    customer_id  TEXT PRIMARY KEY,
    account_id   TEXT,
    method       TEXT NOT NULL,             -- account_number / name+domain / ...
    status       TEXT NOT NULL CHECK (status IN ('auto', 'review')),
    candidates   TEXT NOT NULL              -- JSON list of candidate account IDs
) STRICT;

CREATE TABLE IF NOT EXISTS messages (       -- inbox, linked and classified
    id           TEXT PRIMARY KEY,
    customer_id  TEXT,
    link_method  TEXT,
    intent       TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    from_email   TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS followups (
    id             TEXT PRIMARY KEY,        -- FU-<as_of>-<customer_id>
    as_of          TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    account_id     TEXT,
    action         TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    days_overdue   INTEGER NOT NULL,
    signals        TEXT NOT NULL,           -- JSON
    evidence       TEXT NOT NULL,           -- JSON list of record IDs
    draft_to       TEXT,
    draft_subject  TEXT,
    draft_body     TEXT NOT NULL,           -- email draft (reminders) or internal note
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'paused')),
    slack_channel  TEXT,
    slack_thread   TEXT,                    -- digest message ts (thread parent)
    slack_ts       TEXT                     -- this follow-up's card
) STRICT;

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    followup_id  TEXT NOT NULL REFERENCES followups(id),
    decision     TEXT NOT NULL CHECK (decision IN ('approve', 'edit', 'pause', 'override')),
    user_id      TEXT NOT NULL,
    user_name    TEXT NOT NULL,
    action       TEXT NOT NULL,             -- the follow-up action that was approved or paused
    evidence     TEXT NOT NULL,             -- JSON record IDs the decision relied on
    text         TEXT NOT NULL              -- final draft / note as approved
) STRICT;

CREATE TABLE IF NOT EXISTS outbox (         -- "sent" email: a row here + a copy in Slack, never SMTP
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    followup_id  TEXT NOT NULL REFERENCES followups(id),
    decision_id  INTEGER NOT NULL REFERENCES decisions(id),
    to_email     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status = 'sent_simulated')
) STRICT;

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,             -- system:<step> or slack:<user_id>
    event        TEXT NOT NULL,
    followup_id  TEXT,
    detail       TEXT NOT NULL              -- JSON
) STRICT;

CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outbox_no_update BEFORE UPDATE ON outbox
BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outbox_no_delete BEFORE DELETE ON outbox
BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Layer 2 (LLM steps). Each row is one stateless call or one validated output; history is kept.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY,
    ts                 TEXT NOT NULL,
    task               TEXT NOT NULL,           -- classify_reply / brief / investigate
    model              TEXT NOT NULL,           -- as returned by the provider
    ref                TEXT,                    -- message or follow-up ID
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    ms                 INTEGER NOT NULL,
    error              TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS reply_labels (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    model         TEXT NOT NULL,
    intent        TEXT,
    promise_date  TEXT,
    quote         TEXT,
    valid         INTEGER NOT NULL,
    problems      TEXT NOT NULL                 -- JSON list; empty when valid
) STRICT;

CREATE TABLE IF NOT EXISTS briefs (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    followup_id  TEXT NOT NULL,
    model        TEXT NOT NULL,
    content      TEXT NOT NULL,                 -- JSON {"sentences": [{"text", "cites"}]}
    valid        INTEGER NOT NULL,
    problems     TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS agent_runs (      -- layer 3: one investigation per row
    id                 INTEGER PRIMARY KEY,
    ts                 TEXT NOT NULL,
    followup_id        TEXT NOT NULL,
    model              TEXT NOT NULL,
    steps              INTEGER NOT NULL,        -- tool-call rounds used
    repairs            INTEGER NOT NULL,        -- answers re-asked after validator rejection
    tokens             INTEGER NOT NULL,        -- prompt + completion, all calls
    ms                 INTEGER NOT NULL,
    stop               TEXT NOT NULL,           -- done / step_limit / budget / error
    transcript         TEXT NOT NULL,           -- JSON [{tool, args, result}]: what the agent saw
    output             TEXT NOT NULL,           -- JSON final answer
    valid              INTEGER NOT NULL,
    problems           TEXT NOT NULL
) STRICT;
