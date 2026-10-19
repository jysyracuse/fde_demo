-- Organisation directory (stands in for HR / the Slack directory): who holds which role.
-- Separate from app.db so a demo data reset keeps it. Managed with scripts/roles.py.
CREATE TABLE IF NOT EXISTS roles (
    role       TEXT PRIMARY KEY
               CHECK (role IN ('ar_specialist', 'billing_ops_manager', 'account_manager')),
    slack_user TEXT NOT NULL,
    email      TEXT,
    updated_at TEXT NOT NULL
) STRICT;
