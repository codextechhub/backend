"""Read-side reporting over the AP sub-ledger and the GR/IR control.

The AP mirror of :mod:`vs_finance.reports`: an aging of what the entity owes its
vendors, the cardinal **sub-ledger == control** reconciliation, and the GR/IR clearing
balance (goods received but not yet invoiced, or invoiced but not received).

All amounts are integer kobo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from vs_finance.reports import _account_gl_net

#: Aging bucket labels, in order. "current" = not yet overdue.
AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def _bucket_for(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


@dataclass
class AgingRow:
    """One vendor's outstanding AP, split into aging buckets (kobo)."""

    vendor_id: int
    code: str
    name: str
    buckets: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    outstanding: int = 0          # gross of unapplied debit
    unallocated_credit: int = 0   # open payment (prepayment) not yet applied
    net: int = 0                  # outstanding - unallocated_credit


@dataclass
class APAgingReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    total_outstanding: int = 0
    total_unallocated_credit: int = 0
    total_net: int = 0


def ap_aging(entity, *, as_of=None) -> APAgingReport:
    """Age each vendor's open bills into current/1-30/31-60/61-90/90+ buckets.

    A bill ages off its ``due_date`` (falling back to ``invoice_date``). Only POSTED,
    not-fully-paid bills contribute, by their ``balance_due``. Each vendor's unallocated
    payment (a prepayment/debit) is reported and netted, so ``total_net`` equals the AP
    control account's GL balance (see :func:`reconcile_ap`).
    """
    from .models import VendorInvoice, VendorPayment

    as_of = as_of or timezone.now().date()
    report = APAgingReport(entity_id=entity.id, as_of=as_of)
    rows: dict[int, AgingRow] = {}

    def row_for(vendor):
        r = rows.get(vendor.id)
        if r is None:
            r = AgingRow(
                vendor_id=vendor.id, code=vendor.code, name=vendor.name,
                buckets={b: 0 for b in AGING_BUCKETS},
            )
            rows[vendor.id] = r
        return r

    posted_invoices = (
        VendorInvoice.objects
        .filter(entity=entity, status="POSTED")
        .exclude(payment_status="PAID")
        .select_related("vendor")
    )
    for inv in posted_invoices:
        due = inv.balance_due
        if due <= 0:
            continue
        ref_date = inv.due_date or inv.invoice_date
        days_overdue = (as_of - ref_date).days
        bucket = _bucket_for(days_overdue)
        r = row_for(inv.vendor)
        r.buckets[bucket] += due
        r.outstanding += due

    posted_payments = (
        VendorPayment.objects.filter(entity=entity, status="POSTED").select_related("vendor")
    )
    for pay in posted_payments:
        credit = pay.unallocated_amount
        if credit <= 0:
            continue
        r = row_for(pay.vendor)
        r.unallocated_credit += credit

    for r in rows.values():
        r.net = r.outstanding - r.unallocated_credit
        for b in AGING_BUCKETS:
            report.bucket_totals[b] += r.buckets[b]
        report.total_outstanding += r.outstanding
        report.total_unallocated_credit += r.unallocated_credit
        report.total_net += r.net

    report.rows = sorted(rows.values(), key=lambda x: x.code)
    return report


@dataclass
class APReconciliation:
    entity_id: int
    subledger_total: int     # from the AP aging (vendor balances)
    control_total: int       # from the AP control account(s) in the GL
    difference: int

    @property
    def is_reconciled(self) -> bool:
        return self.difference == 0


def reconcile_ap(entity, *, as_of=None) -> APReconciliation:
    """Assert the AP **sub-ledger** (vendor balances) equals the AP **control** GL.

    The cardinal AP control: the sum of what the entity owes every vendor must equal
    the balance of the payable control account(s) in the ledger. Any drift means a
    posting bypassed the sub-ledger (or vice-versa) and must be investigated.
    """
    from .models import Vendor

    aging = ap_aging(entity, as_of=as_of)
    subledger_total = aging.total_net

    control_accounts = {
        v.payable_account
        for v in Vendor.objects.filter(entity=entity).select_related("payable_account")
        if v.payable_account_id is not None
    }
    control_total = sum(_account_gl_net(acc) for acc in control_accounts)

    return APReconciliation(
        entity_id=entity.id,
        subledger_total=subledger_total,
        control_total=control_total,
        difference=subledger_total - control_total,
    )


def grir_balance(entity) -> int:
    """Net balance of the GR/IR clearing account for ``entity`` (kobo, signed credit).

    The GR/IR control nets to **zero** when every received good has been invoiced (and
    vice-versa). A non-zero balance is the value of goods received-not-invoiced (credit)
    or invoiced-not-received (debit) — the headline number a GR/IR aging drills into.
    """
    from vs_finance.models import Account

    from .constants import GRIR_CLEARING_CODE

    account = (
        Account.objects
        .filter(entity=entity, code=GRIR_CLEARING_CODE)
        .first()
    )
    if account is None:
        return 0
    return _account_gl_net(account)
