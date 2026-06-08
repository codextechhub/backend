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


# --------------------------------------------------------------------------- #
# Procurement analytics — spend, vendor performance, PR→payment cycle time     #
# --------------------------------------------------------------------------- #
#
# Management reporting over the P2P chain. Spend analysis reads realised cost
# (POSTED vendor invoices); vendor performance blends ordering, delivery timeliness
# and payment speed per vendor; cycle time measures how long each hop of the
# requisition → PO → receipt → invoice → payment chain takes on average. All amounts
# are integer kobo; durations are whole days.


def _avg_days(values) -> float | None:
    """Mean of a list of day-counts, rounded to one decimal; ``None`` when empty."""
    return round(sum(values) / len(values), 1) if values else None


def _format_naira(amount: int) -> str:
    from vs_finance.money import format_naira

    return format_naira(amount)


@dataclass
class SpendRow:
    """Realised spend for one grouping key (vendor or category), in kobo."""

    key: str
    label: str
    net: int = 0
    tax: int = 0
    gross: int = 0
    invoice_count: int = 0

    @property
    def gross_naira(self) -> str:
        return _format_naira(self.gross)


@dataclass
class SpendAnalysis:
    """Spend over a window, broken down by vendor and by vendor category (kobo)."""

    entity_id: int
    start_date: object
    end_date: object
    by_vendor: list = field(default_factory=list)
    by_category: list = field(default_factory=list)
    total_net: int = 0
    total_tax: int = 0
    total_gross: int = 0
    invoice_count: int = 0


def spend_analysis(entity, *, start_date=None, end_date=None) -> SpendAnalysis:
    """Analyse realised spend for ``entity`` from POSTED vendor invoices.

    Spend is the gross of POSTED :class:`VendorInvoice` s whose ``invoice_date`` falls
    in ``[start_date, end_date]`` (either bound optional). Rows are returned both by
    vendor and by vendor category (uncategorised vendors roll into an "Uncategorised"
    bucket), each sorted by descending gross spend.
    """
    from vs_finance.constants import DocumentStatus

    from .models import VendorInvoice

    qs = (
        VendorInvoice.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor", "vendor__category")
    )
    if start_date is not None:
        qs = qs.filter(invoice_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(invoice_date__lte=end_date)

    vendors: dict = {}
    categories: dict = {}
    report = SpendAnalysis(entity_id=entity.id, start_date=start_date, end_date=end_date)

    for inv in qs:
        report.total_net += inv.subtotal
        report.total_tax += inv.tax_total
        report.total_gross += inv.total
        report.invoice_count += 1

        v = inv.vendor
        vrow = vendors.get(v.id)
        if vrow is None:
            vrow = vendors[v.id] = SpendRow(key=v.code, label=v.name)
        vrow.net += inv.subtotal
        vrow.tax += inv.tax_total
        vrow.gross += inv.total
        vrow.invoice_count += 1

        cat = v.category
        ckey = cat.code if cat else "UNCATEGORISED"
        clabel = cat.name if cat else "Uncategorised"
        crow = categories.get(ckey)
        if crow is None:
            crow = categories[ckey] = SpendRow(key=ckey, label=clabel)
        crow.net += inv.subtotal
        crow.tax += inv.tax_total
        crow.gross += inv.total
        crow.invoice_count += 1

    report.by_vendor = sorted(vendors.values(), key=lambda r: r.gross, reverse=True)
    report.by_category = sorted(categories.values(), key=lambda r: r.gross, reverse=True)
    return report


@dataclass
class VendorPerformanceRow:
    """One vendor's ordering, delivery and payment behaviour over a window."""

    vendor_id: int
    code: str
    name: str
    po_count: int = 0
    total_ordered: int = 0
    receipt_count: int = 0
    on_time_receipts: int = 0
    late_receipts: int = 0
    invoice_count: int = 0
    total_billed: int = 0
    payment_count: int = 0
    total_paid: int = 0
    avg_payment_days: float | None = None

    @property
    def on_time_rate(self) -> float | None:
        rated = self.on_time_receipts + self.late_receipts
        return round(self.on_time_receipts / rated, 4) if rated else None


@dataclass
class VendorPerformanceReport:
    entity_id: int
    start_date: object
    end_date: object
    rows: list = field(default_factory=list)


def vendor_performance(entity, *, start_date=None, end_date=None) -> VendorPerformanceReport:
    """Blend ordering, delivery timeliness and payment speed per vendor.

    For each vendor with activity in ``[start_date, end_date]``:

    * **Ordering** — count and value of POs (``order_date`` in window, excluding
      CANCELLED / REVERSED).
    * **Delivery** — POSTED goods receipts (``received_date`` in window) classified
      on-time vs late against their PO's ``expected_date`` (receipts whose PO has no
      expected date are not rated).
    * **Billing & payment** — POSTED vendor invoices and the average days from
      ``invoice_date`` to the settling payment's ``payment_date`` (over allocations).
    """
    from vs_finance.constants import DocumentStatus

    from .models import (
        GoodsReceivedNote,
        PurchaseOrder,
        VendorInvoice,
        VendorPaymentAllocation,
    )

    rows: dict = {}

    def row_for(vendor) -> VendorPerformanceRow:
        r = rows.get(vendor.id)
        if r is None:
            r = rows[vendor.id] = VendorPerformanceRow(
                vendor_id=vendor.id, code=vendor.code, name=vendor.name,
            )
        return r

    excluded = {DocumentStatus.CANCELLED, DocumentStatus.REVERSED}

    po_qs = PurchaseOrder.objects.filter(entity=entity).select_related("vendor").exclude(status__in=excluded)
    if start_date is not None:
        po_qs = po_qs.filter(order_date__gte=start_date)
    if end_date is not None:
        po_qs = po_qs.filter(order_date__lte=end_date)
    for po in po_qs:
        r = row_for(po.vendor)
        r.po_count += 1
        r.total_ordered += po.total

    grn_qs = (
        GoodsReceivedNote.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor", "purchase_order")
    )
    if start_date is not None:
        grn_qs = grn_qs.filter(received_date__gte=start_date)
    if end_date is not None:
        grn_qs = grn_qs.filter(received_date__lte=end_date)
    for grn in grn_qs:
        r = row_for(grn.vendor)
        r.receipt_count += 1
        po = grn.purchase_order
        if po is not None and po.expected_date is not None:
            if grn.received_date <= po.expected_date:
                r.on_time_receipts += 1
            else:
                r.late_receipts += 1

    inv_qs = (
        VendorInvoice.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor")
    )
    if start_date is not None:
        inv_qs = inv_qs.filter(invoice_date__gte=start_date)
    if end_date is not None:
        inv_qs = inv_qs.filter(invoice_date__lte=end_date)
    pay_days: dict = {}
    for inv in inv_qs:
        r = row_for(inv.vendor)
        r.invoice_count += 1
        r.total_billed += inv.total

    # Average days-to-pay: invoice_date → settling payment's payment_date, per allocation.
    alloc_qs = (
        VendorPaymentAllocation.objects
        .filter(payment__entity=entity, payment__status=DocumentStatus.POSTED)
        .select_related("payment", "payment__vendor", "vendor_invoice")
    )
    paid_seen: dict = {}
    for alloc in alloc_qs:
        pay = alloc.payment
        inv = alloc.vendor_invoice
        if start_date is not None and inv.invoice_date < start_date:
            continue
        if end_date is not None and inv.invoice_date > end_date:
            continue
        r = row_for(pay.vendor)
        days = (pay.payment_date - inv.invoice_date).days
        pay_days.setdefault(pay.vendor_id, []).append(days)
        r.total_paid += alloc.amount
        # Count each payment once per vendor for payment_count.
        seen = paid_seen.setdefault(pay.vendor_id, set())
        if pay.id not in seen:
            seen.add(pay.id)
            r.payment_count += 1

    for vid, days in pay_days.items():
        rows[vid].avg_payment_days = _avg_days(days)

    ordered_rows = sorted(rows.values(), key=lambda r: r.total_billed, reverse=True)
    return VendorPerformanceReport(
        entity_id=entity.id, start_date=start_date, end_date=end_date, rows=ordered_rows,
    )


@dataclass
class CycleStage:
    """Average duration of one hop in the P2P chain (whole days)."""

    name: str
    label: str
    sample_count: int = 0
    avg_days: float | None = None


@dataclass
class ProcurementCycleTime:
    """Average per-stage and end-to-end durations of the procure-to-pay chain."""

    entity_id: int
    start_date: object
    end_date: object
    stages: list = field(default_factory=list)
    end_to_end_avg_days: float | None = None
    end_to_end_count: int = 0


def procurement_cycle_time(entity, *, start_date=None, end_date=None) -> ProcurementCycleTime:
    """Measure how long each hop of the procure-to-pay chain takes, on average.

    Walks every settling payment back through its bill → PO → requisition and averages
    the elapsed days of each hop:

    * **req → PO**     requisition ``request_date`` → PO ``order_date``
    * **PO → receipt** PO ``order_date`` → first POSTED goods receipt ``received_date``
    * **receipt → invoice** receipt ``received_date`` → bill ``invoice_date``
    * **invoice → payment** bill ``invoice_date`` → payment ``payment_date``

    The chain is anchored on the **payment** (``payment_date`` in the window); each hop
    is only counted when both of its endpoints exist, so a stage's sample size may be
    smaller than the others. ``end_to_end`` is requisition → payment for chains where
    every link is present.
    """
    from vs_finance.constants import DocumentStatus

    from .models import GoodsReceivedNote, VendorPaymentAllocation

    req_to_po: list = []
    po_to_receipt: list = []
    receipt_to_invoice: list = []
    invoice_to_payment: list = []
    end_to_end: list = []

    # Cache the earliest POSTED receipt per PO so we don't re-query in the loop.
    first_receipt: dict = {}
    for grn in (
        GoodsReceivedNote.objects
        .filter(entity=entity, status=DocumentStatus.POSTED, purchase_order__isnull=False)
        .order_by("received_date")
    ):
        first_receipt.setdefault(grn.purchase_order_id, grn.received_date)

    alloc_qs = (
        VendorPaymentAllocation.objects
        .filter(payment__entity=entity, payment__status=DocumentStatus.POSTED)
        .select_related(
            "payment", "vendor_invoice", "vendor_invoice__purchase_order",
            "vendor_invoice__purchase_order__requisition",
        )
    )
    seen_invoices: set = set()
    for alloc in alloc_qs:
        pay = alloc.payment
        if start_date is not None and pay.payment_date < start_date:
            continue
        if end_date is not None and pay.payment_date > end_date:
            continue
        inv = alloc.vendor_invoice
        if inv.id in seen_invoices:
            continue  # measure each bill's chain once (first settling payment)
        seen_invoices.add(inv.id)

        invoice_to_payment.append((pay.payment_date - inv.invoice_date).days)

        po = inv.purchase_order
        receipt_date = first_receipt.get(po.id) if po else None
        if receipt_date is not None:
            receipt_to_invoice.append((inv.invoice_date - receipt_date).days)
        if po is not None:
            if receipt_date is not None:
                po_to_receipt.append((receipt_date - po.order_date).days)
            req = po.requisition
            if req is not None:
                req_to_po.append((po.order_date - req.request_date).days)
                if receipt_date is not None:
                    end_to_end.append((pay.payment_date - req.request_date).days)

    stages = [
        CycleStage("req_to_po", "Requisition → PO",
                   len(req_to_po), _avg_days(req_to_po)),
        CycleStage("po_to_receipt", "PO → Goods receipt",
                   len(po_to_receipt), _avg_days(po_to_receipt)),
        CycleStage("receipt_to_invoice", "Goods receipt → Invoice",
                   len(receipt_to_invoice), _avg_days(receipt_to_invoice)),
        CycleStage("invoice_to_payment", "Invoice → Payment",
                   len(invoice_to_payment), _avg_days(invoice_to_payment)),
    ]
    return ProcurementCycleTime(
        entity_id=entity.id, start_date=start_date, end_date=end_date,
        stages=stages,
        end_to_end_avg_days=_avg_days(end_to_end),
        end_to_end_count=len(end_to_end),
    )
