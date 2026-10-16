-- Simulated billing system (stands in for NetSuite). Owned by billing, not by the CRM.
-- Note: no Salesforce IDs here on purpose. Linking to CRM accounts is step 3's entity matching.
-- SQLite, STRICT tables. Money is INTEGER cents (*_cents) so amounts compare exactly; dates are
-- ISO-8601 TEXT. The runtime opens this file read-only (mode=ro); only load_db.py writes it.

CREATE TABLE customers (
    id                 TEXT PRIMARY KEY,         -- CUS-00042
    legal_name         TEXT NOT NULL,            -- may differ from the CRM account name
    billing_email      TEXT NOT NULL,
    country            TEXT NOT NULL,
    currency           TEXT NOT NULL,
    plan               TEXT NOT NULL,            -- Starter / Growth / Scale / Enterprise
    mrr_cents          INTEGER NOT NULL,
    payment_method     TEXT NOT NULL CHECK (payment_method IN ('card', 'bank_transfer')),
    card_expiry        TEXT,                     -- last day of expiry month, card only
    customer_since     TEXT NOT NULL
) STRICT;

CREATE TABLE invoices (
    id            TEXT PRIMARY KEY,              -- INV-104233
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    issue_date    TEXT NOT NULL,
    due_date      TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('open', 'paid', 'void'))
) STRICT;
CREATE INDEX invoices_customer_due ON invoices (customer_id, due_date);

CREATE TABLE invoice_lines (
    id                TEXT PRIMARY KEY,
    invoice_id        TEXT NOT NULL REFERENCES invoices(id),
    description       TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    amount_cents      INTEGER NOT NULL
) STRICT;

-- invoice_id is NULL for unapplied payments (received but not yet reconciled to an invoice)
CREATE TABLE payments (
    id            TEXT PRIMARY KEY,              -- PAY-...
    customer_id   TEXT REFERENCES customers(id),
    invoice_id    TEXT REFERENCES invoices(id),
    amount_cents  INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    method        TEXT NOT NULL,
    reference     TEXT,
    status        TEXT NOT NULL CHECK (status IN ('applied', 'unapplied'))
) STRICT;

CREATE TABLE charge_attempts (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    invoice_id    TEXT NOT NULL REFERENCES invoices(id),
    attempted_at  TEXT NOT NULL,
    result        TEXT NOT NULL CHECK (result IN ('succeeded', 'failed')),
    failure_code  TEXT                           -- expired_card / insufficient_funds / do_not_honor
) STRICT;

CREATE TABLE credit_notes (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    invoice_id    TEXT NOT NULL REFERENCES invoices(id),
    amount_cents  INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    issued_at     TEXT NOT NULL
) STRICT;

-- Product analytics, monthly active users (churn signal)
CREATE TABLE usage_monthly (
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    month         TEXT NOT NULL,
    active_users  INTEGER NOT NULL,
    PRIMARY KEY (customer_id, month)
) STRICT;

-- Export metadata: as_of is the demo clock ("today") every detection rule uses.
CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;
