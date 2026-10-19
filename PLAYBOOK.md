# AR follow-up playbook

Which follow-up action an overdue customer gets. Audience: AR reviewers, and the models in
`scripts/eval_actions.py`, which read the `## Actions` and `## Checks` sections of this file as
their option descriptions (keep the `### key` headings and the one-paragraph bodies).

## How to decide

Work through the checks in order. **The first check that matches decides the action.** The order
follows the cost of getting it wrong: chasing a customer who disputes the bill, has already paid,
or is mid-renewal does the most damage, so those checks come first. A reminder is the default
only when nothing else applies. If the evidence contradicts itself or fits no action, pick
`needs_human`.

Facts that need arithmetic or date comparison (days overdue, late-payment counts, whether a
card or a promise date has passed, usage trend) are computed in code and given in the case.

## Checks

### dispute
The customer disputes what they were charged: an open Salesforce case whose subject starts
with "[Billing]" and is not closed, or a reply that contests an amount, a user count or a line
on an invoice. A product support case (e.g. bank feed, login) is not a dispute.

### payment_claimed
The customer says they have already paid (a reply such as "we paid on 3 October", "remittance
attached"), or an unapplied payment (no customer link) has the same amount as one of this
customer's overdue invoices and its reference points to this customer or that invoice (invoice
number, company name). An unapplied payment whose reference names a different invoice or
company does not count.

### renewal_open
The customer has a renewal opportunity in Salesforce that is not closed (stage is not "Closed
Won" or "Closed Lost").

### churn_risk
The customer has asked to cancel, close the account or move to another provider, or active
users have fallen sharply: latest month below 60 % of six months ago (`usage.latest_pct_of_first
< 60`).

### chronic_late
The customer pays late as a habit: 4 or more invoices paid more than 10 days late in the past
12 months (`payment_history.paid_over_10_days_late >= 4`), or a payment promise recorded in
Salesforce whose promised date has passed (`promised_date_passed: true`).

### card_expired
The latest charge attempt on an overdue invoice failed with failure code `expired_card`.

## Actions

### pause_and_resolve_dispute
Check `dispute` matched. Stop all reminders; the Billing Ops Manager checks the invoice lines,
contract and credit notes and resolves the dispute before anyone contacts the customer about payment.

### verify_payment_no_dunning
Check `payment_claimed` matched (and `dispute` did not). Send no reminder; the Billing Ops Manager
finds and applies the payment, or confirms it has not arrived.

### notify_account_owner
Check `renewal_open` matched (and no earlier check). No reminder from AR; the Account Manager handles the overdue balance inside the renewal conversation.

### retention_handoff
Check `churn_risk` matched (and no earlier check). No reminder; hand to the Account Manager, because dunning a leaving customer speeds the churn.

### escalate
Check `chronic_late` matched (and no earlier check). Skip the polite reminder; escalate to the
Billing Ops Manager to decide on stronger action (call, payment plan, suspension).

### gentle_reminder
Check `card_expired` matched (and no earlier check). Friendly email asking the customer to update
the card; most of these are involuntary and resolve once the card is replaced.

### standard_reminder
The customer is overdue and none of the checks above matched. Normal reminder email listing the
overdue invoices.

### needs_human
The evidence contradicts itself, is missing something the decision depends on, or fits none of
the actions above. A reviewer decides.
