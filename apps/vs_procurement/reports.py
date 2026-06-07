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


# --------------------------------------------------------------------------- #
# AP cash-requirements forecast                                               #
# --------------------------------------------------------------------------- #

#: Forward-looking buckets for the cash forecast, by days until due.
FORECAST_BUCKETS = ("overdue", "0-7", "8-30", "31-60", "61-90", "90+")


def _forecast_bucket(days_until_due: int) -> str:
    if days_until_due < 0:
        return "overdue"
    if days_until_due <= 7:
        return "0-7"
    if days_until_due <= 30:
        return "8-30"
    if days_until_due <= 60:
        return "31-60"
    if days_until_due <= 90:
        return "61-90"
    return "90+"


@dataclass
class CashRequirementRow:
    """One vendor's open AP, split by *when* the cash will be needed (kobo)."""

    vendor_id: int
    code: str
    name: str
    buckets: dict = field(default_factory=lambda: {b: 0 for b in FORECAST_BUCKETS})
    total: int = 0


@dataclass
class CashRequirementsForecast:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in FORECAST_BUCKETS})
    total_due: int = 0


def ap_cash_requirements(entity, *, as_of=None) -> CashRequirementsForecast:
    """Forecast upcoming cash outflows by grouping open bills on *days until due*.

    The forward-looking twin of :func:`ap_aging`: every POSTED, not-fully-paid bill's
    ``balance_due`` is bucketed by ``due_date - as_of`` into overdue / 0-7 / 8-30 / 31-60
    / 61-90 / 90+ days, per vendor, so treasury can see how much cash each window needs.
    A bill with no ``due_date`` falls back to ``invoice_date`` (typically landing in
    ``overdue``). All amounts are integer kobo.
    """
    from .models import VendorInvoice

    as_of = as_of or timezone.now().date()
    report = CashRequirementsForecast(entity_id=entity.id, as_of=as_of)
    rows: dict[int, CashRequirementRow] = {}

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
        days_until_due = (ref_date - as_of).days
        bucket = _forecast_bucket(days_until_due)
        r = rows.get(inv.vendor_id)
        if r is None:
            r = CashRequirementRow(
                vendor_id=inv.vendor_id, code=inv.vendor.code, name=inv.vendor.name,
                buckets={b: 0 for b in FORECAST_BUCKETS},
            )
            rows[inv.vendor_id] = r
        r.buckets[bucket] += due
        r.total += due

    for r in rows.values():
        for b in FORECAST_BUCKETS:
            report.bucket_totals[b] += r.buckets[b]
        report.total_due += r.total

    report.rows = sorted(rows.values(), key=lambda x: x.code)
    return report


# --------------------------------------------------------------------------- #
# GR/IR aging                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class GRIRAgingRow:
    """One goods receipt's still-open GR/IR position, aged by received date (kobo)."""

    grn_id: int
    reference: str
    vendor_code: str
    vendor_name: str
    received_date: object
    days: int
    bucket: str
    received_value: int
    invoiced_value: int
    open_value: int   # received − invoiced (received-not-invoiced when positive)


@dataclass
class GRIRAgingReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    total_open: int = 0
    control_balance: int = 0   # GL net of the GR/IR clearing account
    difference: int = 0        # total_open − |control_balance| (price variance / non-PO noise)


def grir_aging(entity, *, as_of=None) -> GRIRAgingReport:
    """Age the open GR/IR clearing balance by goods-receipt date.

    Where :func:`grir_balance` is a single point-in-time figure, this drills it into how
    *long* each received-not-invoiced position has been sitting: per POSTED GRN, the
    received value (credited to GR/IR) less the value of POSTED vendor-invoice lines that
    reference its lines (which debited GR/IR clearing it). The remaining ``open_value`` is
    aged off the GRN's ``received_date``. The GL ``control_balance`` is carried alongside;
    a non-zero ``difference`` flags price variances or non-PO postings the GRN walk can't
    see. All amounts are integer kobo.
    """
    from django.db.models import Sum

    from .models import GoodsReceivedNote, VendorInvoiceLine

    as_of = as_of or timezone.now().date()
    report = GRIRAgingReport(entity_id=entity.id, as_of=as_of)

    posted_grns = (
        GoodsReceivedNote.objects
        .filter(entity=entity, status="POSTED")
        .select_related("vendor")
        .order_by("received_date", "id")
    )
    rows = []
    for grn in posted_grns:
        invoiced = (
            VendorInvoiceLine.objects
            .filter(grn_line__grn=grn, vendor_invoice__status="POSTED")
            .aggregate(v=Sum("net_amount"))["v"] or 0
        )
        open_value = grn.total_value - invoiced
        if open_value == 0:
            continue
        days = (as_of - grn.received_date).days
        bucket = _bucket_for(days)
        rows.append(GRIRAgingRow(
            grn_id=grn.id, reference=grn.document_number or str(grn.pk),
            vendor_code=grn.vendor.code, vendor_name=grn.vendor.name,
            received_date=grn.received_date, days=days, bucket=bucket,
            received_value=grn.total_value, invoiced_value=invoiced, open_value=open_value,
        ))
        report.bucket_totals[bucket] += open_value
        report.total_open += open_value

    report.rows = rows
    control = grir_balance(entity)
    report.control_balance = control
    report.difference = report.total_open - abs(control)
    return report


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
