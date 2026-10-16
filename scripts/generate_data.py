"""Generate deterministic synthetic data for the AR follow-up demo.

Outputs (data/out/):
  billing.json         -> SQLite (simulated NetSuite)
  salesforce.json      -> Salesforce (Account + Contact/Opportunity/Case/Task)
  inbox.json           -> simulated customer replies
  labels.json          -> ground truth per customer (category, expected action)
  inbox_labels.json    -> ground truth per message (intent, promise date)

Same seed + same AS_OF => byte-identical output. Deliberate inconsistencies between systems:
CRM account names drift from billing legal names, ~15% of accounts lack the billing ID,
~10% have it in a different format, some replies come from personal email addresses.
"""
from __future__ import annotations

import argparse
import calendar
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arfollow.config import AR_EMAIL, COMPANY, DATA_DIR  # noqa: E402

AS_OF = date(2026, 10, 15)
SEED = 20261016

# category -> (expected action, count)
CATEGORIES: dict[str, tuple[str, int]] = {
    "card_expired":        ("gentle_reminder", 8),
    "invoice_dispute":     ("pause_and_resolve_dispute", 8),
    "renewal_in_progress": ("notify_account_owner", 8),
    "paid_unreconciled":   ("verify_payment_no_dunning", 8),
    "chronic_late":        ("escalate", 8),
    "churn_risk":          ("retention_handoff", 8),
    "standard_overdue":    ("standard_reminder", 10),
    "healthy":             ("none", 42),
}
OVERDUE_DAYS = {
    "card_expired": (3, 20), "invoice_dispute": (10, 45), "renewal_in_progress": (5, 30),
    "paid_unreconciled": (5, 25), "chronic_late": (35, 75), "churn_risk": (10, 35),
    "standard_overdue": (5, 25),
}
PLANS = {  # base fee, per-seat price, seat range
    "Starter":    (49, 10, (2, 5)),
    "Growth":     (149, 15, (5, 20)),
    "Scale":      (490, 20, (15, 60)),
    "Enterprise": (1500, 25, (50, 200)),
}
COUNTRIES = {  # code: (name, currency, legal suffix, tld, weight)
    "AU": ("Australia", "AUD", "Pty Ltd", "com.au", 45),
    "NZ": ("New Zealand", "NZD", "Limited", "co.nz", 25),
    "GB": ("United Kingdom", "GBP", "Ltd", "co.uk", 15),
    "US": ("United States", "USD", "Inc.", "com", 15),
}
INDUSTRIES = ["Construction", "Retail", "Hospitality", "Healthcare", "Technology",
              "Consulting", "Manufacturing", "Transportation", "Education", "Not For Profit"]
NAME_A = ["Harbour", "Kauri", "Ironbark", "Silver Fern", "Bluegum", "Coastal", "Northgate",
          "Riverbend", "Summit", "Wattle", "Tasman", "Copperleaf", "Southern Cross", "Granite",
          "Lighthouse", "Meadow", "Pinnacle", "Redwood", "Saltwater", "Clearwater", "Orchard",
          "Foxglove", "Highland", "Kestrel", "Marlin", "Driftwood", "Totara", "Banksia"]
NAME_B = ["Logistics", "Dental", "Builders", "Coffee Co", "Design Studio", "Physio",
          "Plumbing", "Accounting", "Brewing", "Landscaping", "Electrical", "Vets",
          "Joinery", "Freight", "Fitness Centre", "Bakery", "Legal", "Pharmacy", "Media",
          "Engineering", "Property", "Wellness Centre"]
FIRST = ["Aroha", "Ben", "Chloe", "Daniel", "Emily", "Finn", "Grace", "Hamish", "Isla",
         "Jack", "Kiri", "Liam", "Mia", "Noah", "Olivia", "Priya", "Quinn", "Ruby", "Sam",
         "Tane", "Uma", "Wei", "Xavier", "Yasmin", "Zoe", "Mere", "Oscar", "Hana"]
LAST = ["Anderson", "Brown", "Chen", "Davies", "Edwards", "Fraser", "Gupta", "Hughes",
        "Ihaka", "Jones", "Kumar", "Lee", "Mitchell", "Nguyen", "O'Brien", "Patel",
        "Robinson", "Singh", "Taylor", "Walker", "Wilson", "Young", "Ngata", "Murphy"]


# --- helpers -----------------------------------------------------------------
def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def fmt_date(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def money(x: float) -> float:
    return round(x + 1e-9, 2)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class Customer:
    idx: int
    cid: str
    category: str
    name: str            # trading name, e.g. "Harbour Logistics"
    legal_name: str
    country: str
    plan: str
    seats: int
    base: float
    seat_price: float
    payment_method: str
    customer_since: date
    domain: str
    contacts: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)
    target_days: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mrr(self) -> float:
        return money(self.base + self.seats * self.seat_price)

    @property
    def currency(self) -> str:
        return COUNTRIES[self.country][1]


class Generator:
    def __init__(self, seed: int, as_of: date):
        self.rng = random.Random(seed)
        self.as_of = as_of
        self.customers: list[Customer] = []
        self.inv_lines: list[dict] = []
        self.payments: list[dict] = []
        self.charges: list[dict] = []
        self.credit_notes: list[dict] = []
        self.usage: list[dict] = []
        self.inbox: list[dict] = []
        self.inbox_labels: dict[str, dict] = {}
        self.sf_accounts: list[dict] = []
        self.labels: dict[str, dict] = {}

    # ------------------------------------------------------------------ build
    def run(self) -> None:
        cats = [c for c, (_, n) in CATEGORIES.items() for _ in range(n)]
        self.rng.shuffle(cats)
        names = self._company_names(len(cats))
        for i, (cat, name) in enumerate(zip(cats, names)):
            self.customers.append(self._customer(i, cat, name))
        for c in self.customers:
            self._schedule_invoices(c)
        self._assign_invoice_ids()
        per_cat_idx: dict[str, int] = {}
        for c in self.customers:
            k = per_cat_idx.get(c.category, 0)
            per_cat_idx[c.category] = k + 1
            self._finance(c, k)
            self._usage(c, k)
            self._crm(c, k)
            self._replies(c, k)
            self._label(c)

    def _company_names(self, n: int) -> list[str]:
        pool = [f"{a} {b}" for a in NAME_A for b in NAME_B]
        self.rng.shuffle(pool)
        out, seen_a = [], {}
        for p in pool:  # avoid too many with the same first word
            a = p.split(" ")[0]
            if seen_a.get(a, 0) >= 4:
                continue
            seen_a[a] = seen_a.get(a, 0) + 1
            out.append(p)
            if len(out) == n:
                return out
        raise RuntimeError("not enough names")

    def _customer(self, i: int, cat: str, name: str) -> Customer:
        r = self.rng
        country = r.choices(list(COUNTRIES), weights=[v[4] for v in COUNTRIES.values()])[0]
        if cat == "renewal_in_progress":
            plan = r.choice(["Scale", "Scale", "Enterprise"])
        else:
            plan = r.choices(list(PLANS), weights=[30, 40, 22, 8])[0]
        base, seat_price, (lo, hi) = PLANS[plan]
        method = {
            "card_expired": "card",
            "chronic_late": r.choice(["card", "bank_transfer"]),
            "churn_risk": r.choice(["card", "bank_transfer"]),
            "healthy": "card" if r.random() < 0.6 else "bank_transfer",
        }.get(cat, "bank_transfer")
        suffix, tld = COUNTRIES[country][2], COUNTRIES[country][3]
        c = Customer(
            idx=i, cid=f"CUS-{1001 + i:05d}", category=cat, name=name,
            legal_name=f"{name} {suffix}", country=country, plan=plan,
            seats=r.randint(lo, hi), base=base, seat_price=seat_price,
            payment_method=method,
            customer_since=self.as_of - timedelta(days=r.randint(420, 1800)),
            domain=f"{slug(name)}.{tld}",
        )
        if cat in OVERDUE_DAYS:
            c.target_days = r.randint(*OVERDUE_DAYS[cat])
        return c

    def _schedule_invoices(self, c: Customer) -> None:
        """Monthly invoices, net 14. Everything from the target invoice onwards is unpaid."""
        if c.target_days is not None:
            issue0 = self.as_of - timedelta(days=c.target_days + 14)
            if issue0.day > 28:
                issue0 = issue0.replace(day=28)
            c.target_days = (self.as_of - (issue0 + timedelta(days=14))).days
        else:
            issue0 = self.as_of.replace(day=self.rng.randint(1, 28))
            if issue0 > self.as_of:
                issue0 = add_months(issue0, -1)
        for k in range(-11, 3):
            issue = add_months(issue0, k)
            if issue > self.as_of:
                break
            due = issue + timedelta(days=14)
            if c.target_days is not None:
                unpaid = issue >= issue0
            else:
                unpaid = due >= self.as_of
            c.invoices.append({"issue_date": issue, "due_date": due, "unpaid": unpaid,
                               "seats_billed": c.seats, "target": issue == issue0
                               and c.target_days is not None})

    def _assign_invoice_ids(self) -> None:
        allinv = [(inv["issue_date"], c.cid, inv) for c in self.customers for inv in c.invoices]
        allinv.sort(key=lambda t: (t[0], t[1]))
        for n, (_, _, inv) in enumerate(allinv):
            inv["id"] = f"INV-{104001 + n}"

    # ---------------------------------------------------------------- finance
    def _finance(self, c: Customer, k: int) -> None:
        r = self.rng
        target = next((inv for inv in c.invoices if inv["target"]), None)

        # Dispute: the target invoice over-bills seats.
        if c.category == "invoice_dispute" and target:
            target["seats_billed"] = c.seats + r.choice([3, 5, 8, 10])

        for inv in c.invoices:
            seat_amt = money(inv["seats_billed"] * c.seat_price)
            inv["amount"] = money(c.base + seat_amt)
            self.inv_lines += [
                {"id": f"{inv['id']}-1", "invoice_id": inv["id"],
                 "description": f"{c.plan} plan, monthly subscription",
                 "quantity": 1, "unit_price": c.base, "amount": c.base},
                {"id": f"{inv['id']}-2", "invoice_id": inv["id"],
                 "description": "Additional users", "quantity": inv["seats_billed"],
                 "unit_price": c.seat_price, "amount": seat_amt},
            ]

        late_prob = {"chronic_late": 0.6, "standard_overdue": 0.1}.get(c.category, 0.03)
        decoy_retry = c.category == "healthy" and c.payment_method == "card" and k % 6 == 0
        for j, inv in enumerate(c.invoices):
            if inv["unpaid"]:
                continue
            if c.payment_method == "card":
                paid = inv["due_date"]
                # Decoy: an expired card that the customer already fixed (must not be flagged)
                if decoy_retry and j == len([i for i in c.invoices if not i["unpaid"]]) - 1:
                    self._charge(c, inv, inv["due_date"], "failed", "expired_card")
                    paid = inv["due_date"] + timedelta(days=3)
                    c.notes.append("decoy: past expired-card failure already recovered")
                if c.category == "chronic_late" and r.random() < late_prob:
                    for d in (0, 3, 7):
                        self._charge(c, inv, inv["due_date"] + timedelta(days=d),
                                     "failed", "insufficient_funds")
                    paid = inv["due_date"] + timedelta(days=r.randint(12, 40))
                self._charge(c, inv, paid, "succeeded", None)
            else:
                if r.random() < late_prob:
                    paid = inv["due_date"] + timedelta(days=r.randint(12, 45))
                else:
                    paid = inv["due_date"] + timedelta(days=r.randint(-8, 3))
            paid = min(paid, self.as_of - timedelta(days=1))
            paid = max(paid, inv["issue_date"])
            inv["paid_at"] = paid
            self.payments.append({
                "id": f"PAY-{inv['id'][4:]}", "customer_id": c.cid, "invoice_id": inv["id"],
                "amount": inv["amount"], "currency": c.currency, "received_at": paid,
                "method": c.payment_method,
                "reference": inv["id"] if c.payment_method == "bank_transfer"
                else f"ch_{slug(inv['id'])}{c.idx:03d}",
                "status": "applied"})

        unpaid_due = [i for i in c.invoices if i["unpaid"] and i["due_date"] < self.as_of]
        if c.payment_method == "card":
            code = {"card_expired": "expired_card", "chronic_late": "insufficient_funds",
                    "churn_risk": "do_not_honor"}.get(c.category)
            for inv in unpaid_due:
                for d in (0, 3, 7):
                    when = inv["due_date"] + timedelta(days=d)
                    if when <= self.as_of and code:
                        self._charge(c, inv, when, "failed", code)

        if c.category == "paid_unreconciled" and target and k % 2 == 0:
            received = self.as_of - timedelta(days=r.randint(1, 3))
            ref = [f"{c.name.upper()[:18]} {target['id'][4:]}", "MONTHLY SUBSCRIPTION",
                   f"{target['id']}", f"INV{target['id'][4:]} {c.name.split()[0].upper()}"][k % 4]
            self.payments.append({
                "id": f"PAY-U{c.idx:03d}", "customer_id": None, "invoice_id": None,
                "amount": target["amount"], "currency": c.currency, "received_at": received,
                "method": "bank_transfer", "reference": ref, "status": "unapplied"})
            c.notes.append(f"unapplied payment PAY-U{c.idx:03d} (no customer link) matches "
                           f"{target['id']}")
        elif c.category == "paid_unreconciled":
            c.notes.append("customer says paid; no payment record yet (in transit)")

        if c.category == "invoice_dispute" and target and k < 2:
            over = money((target["seats_billed"] - c.seats) * c.seat_price)
            amount = over if k == 0 else money(over / 2)
            self.credit_notes.append({
                "id": f"CN-{c.idx:04d}", "customer_id": c.cid, "invoice_id": target["id"],
                "amount": amount,
                "reason": "Seat count correction" if k == 0 else "Partial goodwill credit",
                "issued_at": self.as_of - timedelta(days=r.randint(1, 5))})
            c.notes.append("credit note covers the full over-billing; CRM case still open"
                           if k == 0 else "credit note covers half the over-billing")

        # customer-level billing record
        if c.payment_method == "card":
            if c.category == "card_expired":
                first_due = unpaid_due[0]["due_date"]
                c.card_expiry = month_end(add_months(first_due, -1))
            else:
                c.card_expiry = month_end(add_months(self.as_of, r.randint(6, 36)))
        else:
            c.card_expiry = None

    def _charge(self, c: Customer, inv: dict, when: date, result: str, code: str | None):
        self.charges.append({
            "id": f"CH-{len(self.charges) + 1:05d}", "customer_id": c.cid,
            "invoice_id": inv["id"], "attempted_at": when, "result": result,
            "failure_code": code})

    def _usage(self, c: Customer, k: int) -> None:
        r = self.rng
        base = max(1, int(c.seats * r.uniform(0.7, 0.95)))
        for m in range(6, 0, -1):
            month = add_months(self.as_of.replace(day=1), -m)
            if c.category == "churn_risk" and m <= 4:
                factor = [0.85, 0.6, 0.35, 0.15][4 - m]
            elif c.category == "healthy" and k % 10 == 3 and m == 2:
                factor = 0.5  # decoy: one-month seasonal dip, then recovery
            else:
                factor = r.uniform(0.92, 1.08)
            self.usage.append({"customer_id": c.cid, "month": month,
                               "active_users": max(0, round(base * factor))})

    # -------------------------------------------------------------------- CRM
    def _crm(self, c: Customer, k: int) -> None:
        r = self.rng
        n_contacts = r.randint(1, 3)
        used = set()
        for j in range(n_contacts):
            while True:
                first, last = r.choice(FIRST), r.choice(LAST)
                if (first, last) not in used:
                    used.add((first, last))
                    break
            title = ["Finance Manager", "Accounts Payable", "Office Manager"][0 if j == 0 else
                                                                             r.randint(1, 2)]
            if j > 0:
                title = r.choice(["Operations Director", "Owner", "Practice Manager", title])
            email = f"{slug(first)}.{slug(last)}@{c.domain}"
            c.contacts.append({"ref": f"CON-{c.idx:03d}-{j}", "FirstName": first,
                               "LastName": last, "Title": title, "Email": email})
        c.billing_email = (f"accounts@{c.domain}" if r.random() < 0.5
                           else c.contacts[0]["Email"])

        # Name drift between billing and CRM
        variant = r.random()
        sf_name = c.name
        if variant < 0.12:
            shout = {"AU": "PTY. LTD.", "NZ": "LTD", "GB": "LIMITED", "US": "INC"}[c.country]
            sf_name = f"{c.name.upper()} {shout}"
        elif variant < 0.22:
            sf_name = c.name.replace("Harbour", "Harbor").replace("Centre", "Center")
            if sf_name == c.name:
                sf_name = f"{c.name} ({c.country})"
        elif variant < 0.32:
            sf_name = c.name.replace(" Co", " Company") if " Co" in c.name else f"The {c.name}"
        elif variant < 0.55:
            sf_name = c.legal_name
        account_number = c.cid
        an = r.random()
        if an < 0.15:
            account_number = None
        elif an < 0.25:
            account_number = c.cid.replace("-", "").replace("CUS0", "CUS")  # CUS01042 -> CUS1042
        c.sf_name, c.sf_account_number = sf_name, account_number

        rating = {"Enterprise": "Hot", "Scale": "Warm", "Growth": "Warm", "Starter": "Cold"}[c.plan]
        if c.category == "renewal_in_progress":
            rating = "Hot"
        acct = {
            "ref": f"ACC-{c.idx:03d}",
            "fields": {
                "Name": sf_name, "Site": "ardemo", "AccountNumber": account_number,
                "Type": "Customer - Direct", "Industry": r.choice(INDUSTRIES),
                "Rating": rating, "BillingCountry": COUNTRIES[c.country][0],
                "NumberOfEmployees": max(c.seats + r.randint(0, 20), 2),
                "Website": f"https://www.{c.domain}",
                "Description": f"{COMPANY} customer since {c.customer_since:%Y}.",
            },
            "contacts": [{k2: v for k2, v in ct.items()} for ct in c.contacts],
            "opportunities": [], "cases": [], "tasks": [],
        }
        if account_number is None:
            del acct["fields"]["AccountNumber"]
        annual = money(c.mrr * 12)
        owner = r.choice(["Sophie Turner", "Raj Malhotra", "Ellie Watson", "Tom Hayes"])

        # Opportunities
        if c.category == "renewal_in_progress":
            close = self.as_of + timedelta(days=r.randint(10, 60))
            acct["opportunities"].append({
                "Name": f"{c.name} - FY27 Renewal", "Type": "Existing Customer - Renewal",
                "StageName": r.choice(["Proposal/Price Quote", "Negotiation/Review"]),
                "CloseDate": close.isoformat(), "Amount": money(annual * 1.05),
                "Description": f"Account executive: {owner}. Multi-year renewal under discussion."})
            c.notes.append(f"open renewal closing {close}, AE {owner}")
        elif c.plan in ("Scale", "Enterprise"):
            acct["opportunities"].append({
                "Name": f"{c.name} - FY26 Renewal", "Type": "Existing Customer - Renewal",
                "StageName": "Closed Won",
                "CloseDate": (self.as_of - timedelta(days=r.randint(120, 300))).isoformat(),
                "Amount": annual, "Description": f"Account executive: {owner}."})
        if c.category in ("healthy", "standard_overdue") and r.random() < 0.15:
            acct["opportunities"].append({
                "Name": f"{c.name} - Add 10 users", "Type": "Existing Customer - Upgrade",
                "StageName": "Qualification",
                "CloseDate": (self.as_of + timedelta(days=r.randint(20, 90))).isoformat(),
                "Amount": money(10 * c.seat_price * 12),
                "Description": f"Account executive: {owner}."})

        # Cases
        target = next((inv for inv in c.invoices if inv["target"]), None)
        bill_contact = c.contacts[0]["ref"]
        if c.category == "invoice_dispute":
            opened = target["issue_date"] + timedelta(days=r.randint(2, 10))
            vague = k % 3 == 2
            acct["cases"].append({
                "Subject": "[Billing] Invoice query" if vague
                else f"[Billing] Disputed user count on {target['id']}",
                "Description": (
                    "Customer called about their latest invoice, says it looks too high. "
                    "Need to check." if vague else
                    f"Customer says they are contracted for {c.seats} users but "
                    f"{target['id']} bills {target['seats_billed']}. Asked for a corrected "
                    "invoice before paying."),
                "Status": r.choice(["New", "Working"]), "Priority": "High",
                "Origin": r.choice(["Email", "Phone"]), "Type": "Other",
                "contact_ref": bill_contact, "_opened": opened.isoformat()})
            if vague:
                c.notes.append("dispute case is vague; invoice lines show the over-billing")
        if c.category == "healthy" and k % 7 == 1:
            acct["cases"].append({
                "Subject": "[Billing] Duplicate charge query",
                "Description": "Customer thought they were charged twice. Confirmed single "
                               "charge, explained pending authorisation. Resolved.",
                "Status": "Closed", "Priority": "Medium", "Origin": "Email", "Type": "Other",
                "contact_ref": bill_contact})
            c.notes.append("decoy: closed billing case")
        if r.random() < 0.12:
            acct["cases"].append({
                "Subject": r.choice(["CSV export times out", "Can't add a new user",
                                     "Bank feed disconnected", "Report totals look off"]),
                "Description": "Product support request.", "Status": "Working",
                "Priority": "Medium", "Origin": "Web", "Type": "Other",
                "contact_ref": bill_contact})

        # Communication history (Tasks)
        acct["tasks"].append({
            "Subject": "Call: onboarding", "Type": "Call", "Status": "Completed",
            "ActivityDate": (c.customer_since + timedelta(days=5)).isoformat(),
            "Description": "Onboarding call, walked through setup.", "contact_ref": bill_contact})
        if r.random() < 0.5:
            acct["tasks"].append({
                "Subject": "Meeting: quarterly check-in", "Type": "Meeting",
                "Status": "Completed",
                "ActivityDate": (self.as_of - timedelta(days=r.randint(30, 200))).isoformat(),
                "Description": "Check-in, no issues raised.", "contact_ref": bill_contact})
        if c.category == "chronic_late":
            late = [i for i in c.invoices if i.get("paid_at") and
                    (i["paid_at"] - i["due_date"]).days > 10]
            for inv in late[-3:]:
                acct["tasks"].append({
                    "Subject": f"Email: overdue reminder {inv['id']}", "Type": "Email",
                    "Status": "Completed",
                    "ActivityDate": (inv["due_date"] + timedelta(days=7)).isoformat(),
                    "Description": f"Sent reminder for {inv['id']}.",
                    "contact_ref": bill_contact})
            broken = target["due_date"] + timedelta(days=10)
            acct["tasks"].append({
                "Subject": "Call: payment promise", "Type": "Call", "Status": "Completed",
                "ActivityDate": (target["due_date"] + timedelta(days=4)).isoformat(),
                "Description": f"Customer promised to pay {target['id']} by {broken}. "
                               "Follow up if not received.", "contact_ref": bill_contact})
            c.notes.append(f"{len(late)} late payments in history; broken promise {broken}")
        if c.category == "invoice_dispute":
            acct["tasks"].append({
                "Subject": f"Email: customer queried {target['id']}", "Type": "Email",
                "Status": "Completed",
                "ActivityDate": (target["issue_date"] + timedelta(days=3)).isoformat(),
                "Description": "Customer replied to invoice email questioning the amount.",
                "contact_ref": bill_contact})
        if c.category == "churn_risk" and k % 2 == 0:
            acct["tasks"].append({
                "Subject": "Call: renewal health check", "Type": "Call", "Status": "Completed",
                "ActivityDate": (self.as_of - timedelta(days=r.randint(20, 45))).isoformat(),
                "Description": "Customer mentioned budget cuts and fewer staff using the "
                               "product.", "contact_ref": bill_contact})
        self.sf_accounts.append(acct)

    # ---------------------------------------------------------------- replies
    def _replies(self, c: Customer, k: int) -> None:
        r = self.rng
        target = next((inv for inv in c.invoices if inv["target"]), None)
        plan: list[tuple[str, str]] = []  # (intent, template key)
        cat = c.category
        if cat == "invoice_dispute" and k < 6:
            plan.append(("dispute", ["d_amount", "d_hold", "d_po"][k % 3]))
        elif cat == "paid_unreconciled":
            plan.append(("paid", ["p_ref", "p_remit", "p_amount", "p_ap"][k % 4]))
        elif cat == "churn_risk":
            plan.append(("cancel", ["c_move", "c_end", "c_unused"][k % 3]) if k < 6
                        else ("unclear", "u_review"))
        elif cat == "chronic_late" and k < 5:
            plan.append(("promise_to_pay", ["pp_date", "pp_friday", "pp_run"][k % 3]))
        elif cat == "standard_overdue" and k < 6:
            plan.append(("promise_to_pay", ["pp_date", "pp_friday", "pp_run", "pp_date"][k])
                        if k < 4 else ("unclear", "u_ooo"))
        elif cat == "card_expired" and k < 3:
            plan.append(("promise_to_pay", "pp_card"))
        elif cat == "renewal_in_progress" and k < 3:
            plan.append(("promise_to_pay", "pp_renewal") if k < 2 else ("unclear", "u_who"))
        elif cat == "healthy" and k < 3:
            plan.append(("unclear", ["u_thanks", "u_resend", "u_who"][k]))

        for intent, key in plan:
            contact = c.contacts[0]
            roll = r.random()
            if roll < 0.12:
                sender = f"{slug(contact['FirstName'])}{slug(contact['LastName'])}" \
                         f"{r.randint(10, 99)}@gmail.com"
            elif roll < 0.35:
                sender = c.billing_email
            else:
                sender = contact["Email"]
            received = datetime.combine(self.as_of - timedelta(days=r.randint(0, 5)),
                                        time(r.randint(7, 18), r.choice([4, 17, 32, 48])))
            body, promise = self._render(key, c, target, received.date())
            sig_name = c.sf_name if r.random() < 0.5 else c.name
            body += f"\n\n{contact['FirstName']}\n{sig_name}"
            mid = f"MSG-{len(self.inbox) + 1:04d}"
            subject = (f"RE: Invoice {target['id']} overdue" if target and r.random() < 0.7
                       else f"RE: Your {COMPANY} account")
            self.inbox.append({"id": mid, "received_at": received.isoformat(),
                               "from_email": sender,
                               "from_name": f"{contact['FirstName']} {contact['LastName']}",
                               "to": AR_EMAIL, "subject": subject, "body": body})
            self.inbox_labels[mid] = {
                "intent": intent, "promise_date": promise.isoformat() if promise else None,
                "customer_id": c.cid, "invoice_id": target["id"] if target else None,
                "sender_is_personal": sender.endswith("@gmail.com")}

    def _render(self, key: str, c: Customer, inv: dict | None, today: date):
        r = self.rng
        inv_id = inv["id"] if inv else "the invoice"
        amt = f"{c.currency} {inv['amount']:,.2f}" if inv else ""
        promise = None
        if key == "pp_date":
            promise = today + timedelta(days=r.randint(3, 12))
            text = f"Apologies for the delay. We'll pay {inv_id} on {fmt_date(promise)}."
        elif key == "pp_friday":
            ahead = (4 - today.weekday()) % 7 or 7
            promise = today + timedelta(days=ahead)
            text = "Sorry, this slipped through. Will get it paid by Friday."
        elif key == "pp_run":
            day = 25 if today.day < 25 else 10
            promise = today.replace(day=day) if today.day < day else add_months(
                today.replace(day=day), 1)
            text = (f"Our payment run is on the {day}th of each month. "
                    f"{inv_id} will go out in the next one.")
        elif key == "pp_card":
            promise = today
            text = "Our card expired, sorry. I've just updated it in the portal, please retry " \
                   "the charge today."
        elif key == "pp_renewal":
            text = ("We're finalising the renewal with your sales team at the moment. We'll "
                    "settle the outstanding invoice once the new agreement is signed.")
        elif key == "d_amount":
            text = (f"The amount on {inv_id} is wrong. We're contracted for {c.seats} users "
                    f"but you've billed {inv['seats_billed']}. Please send a corrected invoice.")
        elif key == "d_hold":
            text = (f"We won't be paying {inv_id} until the extra user charges are sorted out. "
                    "We already raised this with your support team.")
        elif key == "d_po":
            text = "This invoice doesn't match our PO. The user count is higher than what we " \
                   "agreed. Can someone look into it?"
        elif key == "p_ref":
            paid = today - timedelta(days=r.randint(1, 4))
            text = (f"Hi, we paid {inv_id} by bank transfer on {fmt_date(paid)}. "
                    "Please check your records.")
        elif key == "p_remit":
            text = "This was paid last week, remittance advice attached. Can you please stop " \
                   "the reminders?"
        elif key == "p_amount":
            text = f"Payment of {amt} went out a few days ago. It should be with you shortly."
        elif key == "p_ap":
            text = f"Our AP system shows {inv_id} as settled. Could you confirm you've received it?"
        elif key == "c_move":
            text = "We've decided to move to another platform. How do we cancel our subscription?"
        elif key == "c_end":
            text = "Please cancel our account at the end of this month. We won't need it going " \
                   "forward. Happy to pay what's owing up to then."
        elif key == "c_unused":
            text = "We've hardly used it in months. Not renewing, please close the account and " \
                   "send a final invoice."
        elif key == "u_review":
            text = "We're reviewing all our software subscriptions for next year. Will get back " \
                   "to you."
        elif key == "u_ooo":
            back = today + timedelta(days=r.randint(5, 12))
            text = f"I'm out of the office until {fmt_date(back)} with limited access to email."
        elif key == "u_who":
            text = "Who should I talk to about our account? Our previous contact has left."
        elif key == "u_thanks":
            text = "Thanks, received."
        elif key == "u_resend":
            text = "Could you resend the latest invoice to our new accounts address?"
        else:
            raise KeyError(key)
        greeting = r.choice(["Hi,", "Hello,", "Hi team,", "Morning,"])
        if text.startswith("Hi,"):
            return text, promise
        return f"{greeting}\n\n{text}", promise

    # ----------------------------------------------------------------- labels
    def _label(self, c: Customer) -> None:
        overdue = [i for i in c.invoices if i["unpaid"] and i["due_date"] < self.as_of]
        self.labels[c.cid] = {
            "sf_ref": f"ACC-{c.idx:03d}",
            "category": c.category,
            "expected_action": CATEGORIES[c.category][0],
            "overdue_invoice_ids": [i["id"] for i in overdue],
            "days_overdue": c.target_days,
            "amount_overdue": money(sum(i["amount"] for i in overdue)),
            "billing_name": c.legal_name,
            "crm_name": c.sf_name,
            "crm_account_number": c.sf_account_number,
            "notes": c.notes,
        }

    # ----------------------------------------------------------------- output
    def outputs(self) -> dict[str, object]:
        customers = [{
            "id": c.cid, "legal_name": c.legal_name, "billing_email": c.billing_email,
            "country": c.country, "currency": c.currency, "plan": c.plan, "mrr": c.mrr,
            "payment_method": c.payment_method, "card_expiry": c.card_expiry,
            "customer_since": c.customer_since} for c in self.customers]
        invoices = [{
            "id": i["id"], "customer_id": c.cid, "issue_date": i["issue_date"],
            "due_date": i["due_date"], "amount": i["amount"], "currency": c.currency,
            "status": "open" if i["unpaid"] else "paid"}
            for c in self.customers for i in c.invoices]
        invoices.sort(key=lambda x: x["id"])
        billing = {"as_of": self.as_of, "customers": customers, "invoices": invoices,
                   "invoice_lines": sorted(self.inv_lines, key=lambda x: x["id"]),
                   "payments": self.payments, "charge_attempts": self.charges,
                   "credit_notes": self.credit_notes, "usage_monthly": self.usage}
        return {
            "billing.json": billing,
            "salesforce.json": {"as_of": self.as_of, "marker_site": "ardemo",
                                "accounts": self.sf_accounts},
            "inbox.json": self.inbox,
            "labels.json": {"as_of": self.as_of, "seed": SEED, "customers": self.labels},
            "inbox_labels.json": self.inbox_labels,
        }


def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--as-of", type=date.fromisoformat, default=AS_OF)
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    g = Generator(args.seed, args.as_of)
    g.run()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, obj in g.outputs().items():
        (args.out / name).write_text(json.dumps(obj, indent=2, default=_default,
                                                ensure_ascii=False) + "\n")
    print(f"Wrote {len(g.customers)} customers, {len(g.inbox)} inbox messages to {args.out}")


if __name__ == "__main__":
    main()
