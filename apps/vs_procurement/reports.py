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
    payment_terms: str = ""       # vendor's standard net terms (e.g. "NET_30"), for the table subtitle
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
                payment_terms=vendor.payment_terms,
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


@dataclass
class APVendorOpenBill:
    """One open POSTED bill for a vendor, aged for the AP drawer (kobo)."""

    invoice_id: int
    document_number: str
    invoice_date: object
    due_date: object
    days_overdue: int
    bucket: str
    balance_due: int
    payment_status: str


@dataclass
class APVendorDetail:
    """A single vendor's AP position for the AP-aging drawer (buckets + open bills)."""

    vendor_id: int
    code: str
    name: str
    as_of: object
    buckets: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    outstanding: int = 0
    unallocated_credit: int = 0
    net: int = 0
    invoices: list = field(default_factory=list)


def ap_vendor_open_bills(entity, vendor, *, as_of=None) -> APVendorDetail:
    """Age one vendor's open bills for the AP drawer — buckets + the invoice list.

    Scoped to a single ``vendor`` (entity-checked by the caller), this mirrors
    :func:`ap_aging`'s per-vendor arithmetic but returns the underlying open invoices too:
    each POSTED, not-fully-paid bill's ``balance_due`` aged off its ``due_date`` (falling
    back to ``invoice_date``). All amounts are integer kobo.
    """
    from .models import VendorInvoice, VendorPayment

    as_of = as_of or timezone.now().date()
    detail = APVendorDetail(
        vendor_id=vendor.id, code=vendor.code, name=vendor.name, as_of=as_of,
        buckets={b: 0 for b in AGING_BUCKETS},
    )

    open_invoices = (
        VendorInvoice.objects
        .filter(entity=entity, vendor=vendor, status="POSTED")
        .exclude(payment_status="PAID")
        .order_by("due_date", "invoice_date", "id")
    )
    for inv in open_invoices:
        due = inv.balance_due
        if due <= 0:
            continue
        ref_date = inv.due_date or inv.invoice_date
        days_overdue = (as_of - ref_date).days
        bucket = _bucket_for(days_overdue)
        detail.buckets[bucket] += due
        detail.outstanding += due
        detail.invoices.append(APVendorOpenBill(
            invoice_id=inv.id, document_number=inv.document_number or str(inv.pk),
            invoice_date=inv.invoice_date, due_date=inv.due_date,
            days_overdue=days_overdue, bucket=bucket,
            balance_due=due, payment_status=inv.payment_status,
        ))

    # Net the vendor's unallocated (prepayment) credit, exactly as ap_aging does.
    for pay in VendorPayment.objects.filter(entity=entity, vendor=vendor, status="POSTED"):
        credit = pay.unallocated_amount
        if credit > 0:
            detail.unallocated_credit += credit
    detail.net = detail.outstanding - detail.unallocated_credit
    return detail


@dataclass
class GRIRGrnDetail:
    """One GRN's GR/IR reconciliation + the documents around it, for the GR/IR drawer."""

    grn_id: int
    reference: str
    vendor_code: str
    vendor_name: str
    received_date: object
    days: int
    bucket: str
    po_number: str
    received_value: int
    invoiced_value: int
    open_value: int
    invoices: list = field(default_factory=list)   # [{id, document_number, invoice_date, net}]


def grir_grn_detail(entity, grn_id, *, as_of=None) -> GRIRGrnDetail | None:
    """The GR/IR position and linked documents for one GRN (drawer detail).

    Returns the GRN's received value, the value of POSTED vendor-invoice lines that
    reference its GRN lines (which cleared it), the remaining ``open_value`` aged off the
    received date, its source PO number, and the distinct matched invoices. Entity-scoped;
    ``None`` when the GRN is not in ``entity``. All amounts are integer kobo.
    """
    from .models import GoodsReceivedNote, VendorInvoiceLine

    as_of = as_of or timezone.now().date()
    grn = (
        GoodsReceivedNote.objects
        .filter(entity=entity, pk=grn_id)
        .select_related("vendor", "purchase_order")
        .first()
    )
    if grn is None:
        return None

    invoiced = 0
    invoices: dict[int, dict] = {}
    inv_lines = (
        VendorInvoiceLine.objects
        .filter(grn_line__grn=grn, vendor_invoice__status="POSTED")
        .select_related("vendor_invoice")
    )
    for line in inv_lines:
        invoiced += line.net_amount
        vi = line.vendor_invoice
        # Accumulate the GR/IR-clearing net per distinct matched invoice.
        row = invoices.get(vi.id)
        if row is None:
            row = invoices[vi.id] = {
                "id": vi.id, "document_number": vi.document_number or str(vi.pk),
                "invoice_date": str(vi.invoice_date), "net": 0,
            }
        row["net"] += line.net_amount

    days = (as_of - grn.received_date).days
    return GRIRGrnDetail(
        grn_id=grn.id, reference=grn.document_number or str(grn.pk),
        vendor_code=grn.vendor.code, vendor_name=grn.vendor.name,
        received_date=grn.received_date, days=days, bucket=_bucket_for(days),
        po_number=(grn.purchase_order.document_number if grn.purchase_order else ""),
        received_value=grn.total_value, invoiced_value=invoiced,
        open_value=grn.total_value - invoiced,
        invoices=list(invoices.values()),
    )


#: GR/IR line status labels — mirror the prototype's per-PO-line status chips.
GRIR_LINE_CLEARED = "Cleared"
GRIR_LINE_RECV_GT_INV = "Received > Invoiced"
GRIR_LINE_INV_GT_RECV = "Invoiced > Received"


def _grir_line_status(received_qty, invoiced_qty, balance) -> str:
    """Derive a PO line's GR/IR status from its received vs invoiced quantities.

    Quantity is the headline the table shows, so it leads the derivation; the monetary
    ``balance`` (received value − invoiced value) only breaks the tie when the two
    quantities are equal (a pure price variance still reads as an imbalance, not cleared).
    """
    if received_qty > invoiced_qty:
        return GRIR_LINE_RECV_GT_INV
    if invoiced_qty > received_qty:
        return GRIR_LINE_INV_GT_RECV
    # Equal quantities: cleared only when the value nets to zero too.
    if balance == 0:
        return GRIR_LINE_CLEARED
    return GRIR_LINE_RECV_GT_INV if balance > 0 else GRIR_LINE_INV_GT_RECV


@dataclass
class GRIRPoLineRow:
    """One PO line's GR/IR position at the line grain (quantities + kobo values)."""

    po_line_id: int
    po_line_ref: str        # "<PO document_number>-<line_no>"
    item: str               # PO line description
    vendor_code: str
    vendor_name: str
    ordered_qty: str        # decimal serialised as string (exact, no float drift)
    received_qty: str
    invoiced_qty: str
    received_value: int     # Σ accepted GRN value (kobo)
    invoiced_value: int     # Σ invoiced net (kobo)
    grir_balance: int       # received_value − invoiced_value (kobo)
    status: str


@dataclass
class GRIRPoLinesReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)


def grir_po_lines(entity, *, as_of=None) -> GRIRPoLinesReport:
    """Line-level GR/IR: per PO line, ordered vs received vs invoiced (qty + value).

    Where :func:`grir_aging` ages the balance per *goods receipt*, this drills it to the
    **PO line** the prototype's GR/IR table lists. For each line on a live PO (CANCELLED /
    REVERSED orders excluded), ``received_qty``/``received_value`` sum the POSTED
    ``GoodsReceivedNoteLine``s pointing at it, and ``invoiced_qty``/``invoiced_value`` sum
    the POSTED ``VendorInvoiceLine``s pointing at it (the direct ``po_line`` FK — the same
    link that advances ``PurchaseOrderLine.invoiced_qty`` in the three-way match). Only
    lines with any receipt or invoice activity are returned. All amounts are integer kobo.
    """
    from collections import defaultdict
    from decimal import Decimal

    from django.db.models import Sum

    from .models import GoodsReceivedNoteLine, PurchaseOrderLine, VendorInvoiceLine

    as_of = as_of or timezone.now().date()
    report = GRIRPoLinesReport(entity_id=entity.id, as_of=as_of)

    # Live PO lines only — a cancelled/reversed order is not an open GR/IR obligation.
    po_lines = (
        PurchaseOrderLine.objects
        .filter(purchase_order__entity=entity)
        .exclude(purchase_order__status__in=("CANCELLED", "REVERSED"))
        .select_related("purchase_order", "purchase_order__vendor")
        .order_by("purchase_order__order_date", "purchase_order_id", "line_no", "id")
    )

    # Two bulk aggregates keyed by po_line — no per-line query (avoids N+1).
    # Received side: accepted qty + booked value from POSTED goods-receipt lines.
    recv = defaultdict(lambda: (Decimal(0), 0))
    grn_agg = (
        GoodsReceivedNoteLine.objects
        .filter(po_line__purchase_order__entity=entity, grn__status="POSTED")
        .values("po_line")
        .annotate(qty=Sum("accepted_qty"), value=Sum("value_amount"))
    )
    for r in grn_agg:
        recv[r["po_line"]] = (Decimal(r["qty"] or 0), int(r["value"] or 0))

    # Invoiced side: billed qty + net from POSTED vendor-invoice lines on that PO line.
    inv = defaultdict(lambda: (Decimal(0), 0))
    inv_agg = (
        VendorInvoiceLine.objects
        .filter(po_line__purchase_order__entity=entity, vendor_invoice__status="POSTED")
        .values("po_line")
        .annotate(qty=Sum("quantity"), net=Sum("net_amount"))
    )
    for r in inv_agg:
        inv[r["po_line"]] = (Decimal(r["qty"] or 0), int(r["net"] or 0))

    rows = []
    for line in po_lines:
        received_qty, received_value = recv.get(line.id, (Decimal(0), 0))
        invoiced_qty, invoiced_value = inv.get(line.id, (Decimal(0), 0))
        # Skip lines with no receipt and no invoice — nothing to reconcile yet.
        if received_qty == 0 and invoiced_qty == 0:
            continue
        balance = received_value - invoiced_value
        po = line.purchase_order
        ref = f"{po.document_number or po.pk}-{line.line_no}"
        rows.append(GRIRPoLineRow(
            po_line_id=line.id, po_line_ref=ref, item=line.description,
            vendor_code=po.vendor.code, vendor_name=po.vendor.name,
            ordered_qty=str(line.quantity),
            received_qty=str(received_qty), invoiced_qty=str(invoiced_qty),
            received_value=received_value, invoiced_value=invoiced_value,
            grir_balance=balance,
            status=_grir_line_status(received_qty, invoiced_qty, balance),
        ))

    report.rows = rows
    return report


@dataclass
class GRIRPoLineDetail:
    """One PO line's GR/IR reconciliation + its linked POSTED GRNs and invoices."""

    po_line_id: int
    po_line_ref: str
    item: str
    vendor_code: str
    vendor_name: str
    po_number: str
    ordered_qty: str
    received_qty: str
    invoiced_qty: str
    received_value: int
    invoiced_value: int
    grir_balance: int
    status: str
    unit_price: int
    grns: list = field(default_factory=list)      # [{id, reference, received_date, accepted_qty, value}]
    invoices: list = field(default_factory=list)  # [{id, document_number, invoice_date, quantity, net}]


def grir_po_line_detail(entity, po_line_id, *, as_of=None) -> GRIRPoLineDetail | None:
    """The GR/IR reconciliation and linked documents for a single PO line (drawer).

    Entity-scoped: a PO line on another entity's order returns ``None`` (the view 404s,
    never leaks). Lists each POSTED goods-receipt line and POSTED vendor-invoice line that
    references this PO line, alongside the received/invoiced/balance reconciliation. All
    amounts are integer kobo.
    """
    from decimal import Decimal

    from .models import GoodsReceivedNoteLine, PurchaseOrderLine, VendorInvoiceLine

    line = (
        PurchaseOrderLine.objects
        # Scope through the parent PO's entity so a foreign line id cannot be read.
        .filter(purchase_order__entity=entity, pk=po_line_id)
        .select_related("purchase_order", "purchase_order__vendor")
        .first()
    )
    if line is None:
        return None

    received_qty, received_value = Decimal(0), 0
    grns = []
    grn_lines = (
        GoodsReceivedNoteLine.objects
        .filter(po_line=line, grn__status="POSTED")
        .select_related("grn")
        .order_by("grn__received_date", "grn_id", "id")
    )
    for gl in grn_lines:
        received_qty += Decimal(gl.accepted_qty)
        received_value += int(gl.value_amount)
        grns.append({
            "id": gl.grn_id,
            "reference": gl.grn.document_number or str(gl.grn_id),
            "received_date": str(gl.grn.received_date),
            "accepted_qty": str(gl.accepted_qty),
            "value": int(gl.value_amount),
        })

    invoiced_qty, invoiced_value = Decimal(0), 0
    invoices = []
    inv_lines = (
        VendorInvoiceLine.objects
        .filter(po_line=line, vendor_invoice__status="POSTED")
        .select_related("vendor_invoice")
        .order_by("vendor_invoice__invoice_date", "vendor_invoice_id", "id")
    )
    for il in inv_lines:
        invoiced_qty += Decimal(il.quantity)
        invoiced_value += int(il.net_amount)
        vi = il.vendor_invoice
        invoices.append({
            "id": vi.id,
            "document_number": vi.document_number or str(vi.id),
            "invoice_date": str(vi.invoice_date),
            "quantity": str(il.quantity),
            "net": int(il.net_amount),
        })

    balance = received_value - invoiced_value
    po = line.purchase_order
    return GRIRPoLineDetail(
        po_line_id=line.id, po_line_ref=f"{po.document_number or po.pk}-{line.line_no}",
        item=line.description, vendor_code=po.vendor.code, vendor_name=po.vendor.name,
        po_number=po.document_number or str(po.pk),
        ordered_qty=str(line.quantity),
        received_qty=str(received_qty), invoiced_qty=str(invoiced_qty),
        received_value=received_value, invoiced_value=invoiced_value,
        grir_balance=balance,
        status=_grir_line_status(received_qty, invoiced_qty, balance),
        unit_price=int(line.unit_price),
        grns=grns, invoices=invoices,
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
class SpendPeriod:
    """One calendar month's realised gross spend (kobo)."""

    period: str          # "YYYY-MM" — sorts chronologically as a plain string
    label: str           # "Mon YYYY" (e.g. "Jan 2026")
    gross: int = 0
    invoice_count: int = 0


@dataclass
class SpendAnalysis:
    """Spend over a window, broken down by vendor and by vendor category (kobo)."""

    entity_id: int
    start_date: object
    end_date: object
    by_vendor: list = field(default_factory=list)
    by_category: list = field(default_factory=list)
    by_period: list = field(default_factory=list)   # monthly trend, chronological
    total_net: int = 0
    total_tax: int = 0
    total_gross: int = 0
    invoice_count: int = 0


def spend_analysis(entity, *, start_date=None, end_date=None, vendor=None, category=None) -> SpendAnalysis:
    """Analyse realised spend for ``entity`` from POSTED vendor invoices.

    Spend is the gross of POSTED :class:`VendorInvoice` s whose ``invoice_date`` falls
    in ``[start_date, end_date]`` (either bound optional). Rows are returned both by
    vendor and by vendor category (uncategorised vendors roll into an "Uncategorised"
    bucket), each sorted by descending gross spend. Pass ``vendor`` to scope the whole
    computation to a single supplier; pass ``category`` (a category code, or the literal
    ``"UNCATEGORISED"``) to scope it to one purchasing category — the per-category drawer
    reuses this so its by_vendor / by_period reflect only that category.
    """
    from vs_finance.constants import DocumentStatus

    from .models import VendorInvoice

    qs = (
        VendorInvoice.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor", "vendor__category")
    )
    if vendor is not None:
        qs = qs.filter(vendor=vendor)
    if category is not None:
        # "UNCATEGORISED" is the synthetic key for vendors with no category (mirrors the
        # by_category grouping below); a real code matches the vendor's category code.
        if category == "UNCATEGORISED":
            qs = qs.filter(vendor__category__isnull=True)
        else:
            qs = qs.filter(vendor__category__code=category)
    if start_date is not None:
        qs = qs.filter(invoice_date__gte=start_date)
    if end_date is not None:
        qs = qs.filter(invoice_date__lte=end_date)

    vendors: dict = {}
    categories: dict = {}
    periods: dict = {}
    report = SpendAnalysis(entity_id=entity.id, start_date=start_date, end_date=end_date)

    for inv in qs:
        report.total_net += inv.subtotal
        report.total_tax += inv.tax_total
        report.total_gross += inv.total
        report.invoice_count += 1

        # Monthly trend: bucket each bill on its invoice_date month in this same pass
        # (no second query). The "YYYY-MM" key sorts chronologically as a plain string.
        pkey = f"{inv.invoice_date.year:04d}-{inv.invoice_date.month:02d}"
        prow = periods.get(pkey)
        if prow is None:
            prow = periods[pkey] = SpendPeriod(
                period=pkey, label=inv.invoice_date.strftime("%b %Y"),
            )
        prow.gross += inv.total
        prow.invoice_count += 1

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
    # Ascending by month key = chronological (a plain "YYYY-MM" string sort).
    report.by_period = [periods[k] for k in sorted(periods)]
    return report


@dataclass
class VendorPerformanceRow:
    """One vendor's ordering, delivery and payment behaviour over a window."""

    vendor_id: int
    code: str
    name: str
    category: str = ""   # vendor's category name (table subtitle), "" when uncategorised
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
    latest_assessment: object = None   # most-recent VendorAssessment, or None

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


def vendor_performance(entity, *, start_date=None, end_date=None, vendor=None) -> VendorPerformanceReport:
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
    if vendor is not None:
        po_qs = po_qs.filter(vendor=vendor)
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
    if vendor is not None:
        grn_qs = grn_qs.filter(vendor=vendor)
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
    if vendor is not None:
        inv_qs = inv_qs.filter(vendor=vendor)
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
    if vendor is not None:
        alloc_qs = alloc_qs.filter(payment__vendor=vendor)
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

    # Attach each vendor's category name — one query for every row's vendor (no N+1),
    # used as the performance table's per-vendor subtitle.
    if rows:
        from .models import Vendor

        for v in Vendor.objects.filter(id__in=list(rows.keys())).select_related("category"):
            rows[v.id].category = v.category.name if v.category_id else ""

    # Attach each vendor's most-recent point-in-time assessment (one query for all
    # vendors in the report; the first row seen per vendor is the newest by ordering).
    if rows:
        from .models import VendorAssessment

        for assessment in (
            VendorAssessment.objects
            .filter(entity=entity, vendor_id__in=list(rows.keys()))
            .select_related("vendor")
            .order_by("vendor_id", "-assessment_date", "-id")
        ):
            row = rows[assessment.vendor_id]
            if row.latest_assessment is None:
                row.latest_assessment = assessment

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
