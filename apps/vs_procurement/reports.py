"""Read-side reporting over the AP sub-ledger and the GR/IR control.

The AP mirror of :mod:`vs_finance.reports`: an aging of what the entity owes its
vendors, the cardinal **sub-ledger == control** reconciliation, and the GR/IR clearing
balance (goods received but not yet invoiced, or invoiced but not received). The lower
half adds realised-spend, vendor-performance, and procure-to-pay cycle analytics.

All amounts are integer kobo. Every public query starts from ``entity`` (or traverses an
entity-scoped parent), which is the read-side tenant boundary for these reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from vs_finance.reports import _account_gl_net
from vs_finance.receivables import compute_line_net


# --------------------------------------------------------------------------- #
# AP aging and sub-ledger/control reconciliation                              #
# --------------------------------------------------------------------------- #

#: Aging bucket labels, in order. "current" = not yet overdue.
AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def _bucket_for(days_overdue: int) -> str:
    """Map an overdue-day count to inclusive AP/GRIR aging boundaries.

    Due today and future-dated items are ``current``; day 30 remains ``1-30``, day 60
    remains ``31-60``, and day 90 remains ``61-90``. Only day 91 onward is ``90+``.
    """
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
    unallocated_credit: int = 0   # paid in advance, sitting in vendor advances (1240)
    net: int = 0                  # outstanding - advances paid


@dataclass
class APAgingReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    total_outstanding: int = 0
    total_unallocated_credit: int = 0
    total_net: int = 0
    #: Entity-level (branch-less) bills excluded from a narrowed view; None when not narrowed.
    unassigned_excluded_count: int | None = None


def _unassigned_count(qs, branch_scope, *, prefix="") -> int | None:
    """How many rows of this population belong to the entity as a whole, not a branch.

    A document raised before the branch column existed carries a null branch, so it reads
    as entity-level and is legitimately outside a branch-bound caller's report.  Silently
    dropping it would let that caller read their total as the whole story, so the reports
    carry this count beside the total.

    The **count** is deliberate: it says "your view is a subset, by this many documents"
    without disclosing what head office or another branch actually spent, which a money
    figure would.  ``None`` means the caller is not narrowed at all, and every caller
    then keeps the response they had before this existed - an unbound viewer and a tenant
    with no branches included.  Old rows are never given an invented branch.
    """
    if branch_scope is None or not branch_scope.is_narrowed:
        return None
    return qs.filter(**{f"{prefix}branch__isnull": True}).count()


def _ap_snapshot(entity, *, as_of=None, vendor=None, branch_scope=None):
    """Return effective invoices, settlement applied by the cutoff, and vendor advances.

    With no explicit cutoff this preserves the current-state contract. With ``as_of``,
    journal dates define accounting effectiveness and a later payment reversal does not
    rewrite the earlier snapshot; its posted reversal only removes the payment on/after
    the reversal date.

    The third return value is money paid to a vendor that has not settled a bill: it sits
    in the vendor-advance asset (1240), **not** in AP, because the payment journal debits
    AP only for what it settles. It nets down the vendor's overall position on the aging
    screen but has no place in the AP control reconciliation.

    Settlement is dated by the allocation row's own ``effective_date`` - the date of the
    journal that debited AP for it - and not by the mere existence of the row. A row is
    written when someone allocates, which can be long after both documents, and one run
    can settle bills of different ages under a single journal dated at the newest of
    them. Reading the row without its date put settlements on the timeline before the
    ledger moved, and the reconciliation then failed for the days in between.

    ``branch_scope`` (``views.base._BranchScope``) narrows both sides of the snapshot to
    the sub-scope the caller can actually open: bills through ``VendorInvoice.branch`` and
    advances through ``VendorPayment.branch``.  Narrowing both is what keeps the net
    honest - a branch's bills reduced by another branch's prepayment would be a figure
    that reconciles with nothing.  Allocations need no scope filter of their own: they are
    already bounded by the two id sets above.  Omitted, the snapshot stays entity-wide.
    """
    from django.db.models import Q, Sum
    from .models import VendorInvoice, VendorPayment, VendorPaymentAllocation

    invoices = VendorInvoice.objects.filter(entity=entity)
    payments = VendorPayment.objects.filter(entity=entity)
    if branch_scope is not None:
        invoices = invoices.filter(branch_scope.q())
        payments = payments.filter(branch_scope.q())
    if vendor is not None:
        invoices = invoices.filter(vendor=vendor)
        payments = payments.filter(vendor=vendor)
    if as_of is None:
        invoices = invoices.filter(status="POSTED")
        payments = payments.filter(status="POSTED")
    else:
        invoices = invoices.filter(
            journal__status__in=("POSTED", "REVERSED"), journal__date__lte=as_of,
        ).filter(
            Q(journal__reversed_by__isnull=True)
            | Q(journal__reversed_by__date__gt=as_of)
        )
        payments = payments.filter(
            journal__status__in=("POSTED", "REVERSED"), journal__date__lte=as_of,
        ).filter(
            Q(journal__reversed_by__isnull=True)
            | Q(journal__reversed_by__date__gt=as_of)
        )

    invoices = list(invoices.select_related("vendor").order_by("invoice_date", "id"))
    invoice_ids = [invoice.id for invoice in invoices]
    payment_ids = list(payments.values_list("id", flat=True))

    def _settled(queryset, group_field):
        """``{id: kobo}`` settled, honouring ``as_of`` through the row's effective date.

        Rows with no ``effective_date`` are excluded from a dated read rather than
        assumed: a null means the date is genuinely unknown (a row written before the
        column existed and out of reach of the backfill), and guessing would put a
        settlement on the timeline at a date nobody can defend.
        """
        if as_of is not None:
            queryset = queryset.filter(effective_date__isnull=False, effective_date__lte=as_of)
        return {
            row[group_field]: int(row["amount"] or 0)
            for row in queryset.values(group_field).annotate(amount=Sum("amount"))
        }

    paid_by_invoice = _settled(
        VendorPaymentAllocation.objects
        .filter(vendor_invoice_id__in=invoice_ids, payment_id__in=payment_ids),
        "vendor_invoice_id",
    )
    allocated_by_payment = _settled(
        VendorPaymentAllocation.objects
        .filter(payment_id__in=payment_ids, vendor_invoice_id__in=invoice_ids),
        "payment_id",
    )
    advances_by_vendor = {}
    for payment in payments.select_related("vendor").order_by("payment_date", "id"):
        advance = int(payment.gross_amount) - allocated_by_payment.get(payment.id, 0)
        if advance > 0:
            advances_by_vendor[payment.vendor_id] = (
                advances_by_vendor.get(payment.vendor_id, 0) + advance
            )
    return invoices, paid_by_invoice, advances_by_vendor


def _snapshot_payment_status(total: int, paid: int) -> str:
    if paid <= 0:
        return "UNPAID"
    return "PAID" if paid >= total else "PARTIAL"


def _account_gl_net_as_of(account, as_of) -> int:
    """Posted journal-line movement through ``as_of``, signed to normal balance."""
    from django.db.models import Sum
    from vs_finance.constants import NormalBalance
    from vs_finance.models import JournalLine

    totals = JournalLine.objects.filter(
        account=account, entry__status__in=("POSTED", "REVERSED"),
        entry__date__lte=as_of,
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    net = int(totals["debit"] or 0) - int(totals["credit"] or 0)
    return net if account.normal_balance == NormalBalance.DEBIT else -net


def ap_aging(entity, *, as_of=None, branch_scope=None) -> APAgingReport:
    """Age each vendor's open bills into current/1-30/31-60/61-90/90+ buckets.

    A bill ages off its ``due_date`` (falling back to ``invoice_date``). Only POSTED,
    not-fully-paid bills contribute, by their ``balance_due``. Each vendor's unapplied
    payment is reported and netted for the vendor's overall position. That money lives in
    the separate 1240 vendor-advance asset, so :func:`reconcile_ap` compares
    ``total_outstanding``, not ``total_net``, with the AP control account. Bucket totals
    remain gross open invoices; advances reduce only the vendor/report net. When supplied,
    ``as_of`` is both the aging clock and accounting-effectiveness cutoff.

    ``branch_scope`` narrows the report to the bills the caller can actually open, so a
    branch-bound viewer's aging reconciles with their own vendor-invoice list.  Note that
    ``total_outstanding`` then no longer equals the entity's AP control balance - that
    identity is an entity-level control, which is why :func:`reconcile_ap` deliberately
    never passes a scope through.
    """
    from .models import VendorInvoice

    cutoff = as_of
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

    # Entity scoping is applied before any vendor grouping; callers cannot mix tenant
    # balances merely by passing a vendor id from another ledger entity.
    invoices, paid_by_invoice, advances_by_vendor = _ap_snapshot(
        entity, as_of=cutoff, branch_scope=branch_scope,
    )
    report.unassigned_excluded_count = _unassigned_count(
        VendorInvoice.objects.filter(entity=entity, status="POSTED"), branch_scope,
    )
    for inv in invoices:
        due = int(inv.total) - paid_by_invoice.get(inv.id, 0)
        if due <= 0:
            continue
        ref_date = inv.due_date or inv.invoice_date
        days_overdue = (as_of - ref_date).days
        bucket = _bucket_for(days_overdue)
        r = row_for(inv.vendor)
        r.buckets[bucket] += due
        r.outstanding += due

    # A posted but unapplied payment is a vendor advance. It is not aged into an invoice
    # bucket because no bill/due date owns that money yet.
    if advances_by_vendor:
        from .models import Vendor

        for vendor in Vendor.objects.filter(
            entity=entity, id__in=advances_by_vendor,
        ).order_by("code", "id"):
            row_for(vendor).unallocated_credit += advances_by_vendor[vendor.id]

    for r in rows.values():
        r.net = r.outstanding - r.unallocated_credit
        for b in AGING_BUCKETS:
            report.bucket_totals[b] += r.buckets[b]
        report.total_outstanding += r.outstanding
        report.total_unallocated_credit += r.unallocated_credit
        report.total_net += r.net

    report.rows = sorted(rows.values(), key=lambda x: (x.code, x.vendor_id))
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
    ``_account_gl_net`` expresses each credit-normal AP account as a positive liability,
    matching the sub-ledger's ``outstanding`` sign convention.
    """
    from .models import Vendor

    aging = ap_aging(entity, as_of=as_of)
    # Money paid ahead of a bill is booked to the 1240 vendor-advance asset, not to the
    # AP control, so it belongs on the aging screen's vendor *net* position but not in
    # this control-account reconciliation. It used to be netted here because the payment
    # journal debited AP for the full gross, which is exactly the bug that put a debit
    # balance on a liability. Mirrors :func:`vs_finance.reports.reconcile_ar`.
    subledger_total = aging.total_outstanding

    # De-duplicate shared AP controls: several vendors may point at the same account,
    # but its GL balance must enter the reconciliation exactly once.
    control_accounts = {
        v.payable_account
        for v in Vendor.objects.filter(entity=entity).select_related("payable_account")
        if v.payable_account_id is not None
    }
    control_total = sum(
        _account_gl_net_as_of(acc, as_of) if as_of is not None else _account_gl_net(acc)
        for acc in control_accounts
    )

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
    """Map a due-date delta to inclusive forward cash windows.

    Negative values are overdue; zero means due today and starts the ``0-7`` bucket.
    Boundary days 7, 30, 60, and 90 stay in their named lower window.
    """
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
    unallocated_credit: int = 0
    net_total: int = 0


@dataclass
class CashRequirementsForecast:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in FORECAST_BUCKETS})
    total_due: int = 0
    total_unallocated_credit: int = 0
    net_cash_requirement: int = 0
    #: Entity-level (branch-less) bills excluded from a narrowed view; None when not narrowed.
    unassigned_excluded_count: int | None = None


def ap_cash_requirements(entity, *, as_of=None, branch_scope=None) -> CashRequirementsForecast:
    """Forecast upcoming cash outflows by grouping open bills on *days until due*.

    The forward-looking twin of :func:`ap_aging`: every POSTED, not-fully-paid bill's
    ``balance_due`` is bucketed by ``due_date - as_of`` into overdue / 0-7 / 8-30 / 31-60
    / 61-90 / 90+ days, per vendor, so treasury can see how much cash each window needs.
    A bill with no ``due_date`` falls back to ``invoice_date`` (typically landing in
    ``overdue``). Money already paid in advance (sitting in the 1240 vendor-advance
    asset) is shown separately and reduces the net requirement, because that cash has
    left already and the bill it will settle needs none. All amounts are integer kobo;
    ``as_of`` is both forecast clock and accounting-effectiveness cutoff.

    ``branch_scope`` narrows the forecast to the bills the caller can actually open, so a
    branch-bound viewer forecasts their own site's cash rather than the whole tenant's.
    """
    from .models import VendorInvoice

    cutoff = as_of
    as_of = as_of or timezone.now().date()
    report = CashRequirementsForecast(entity_id=entity.id, as_of=as_of)
    rows: dict[int, CashRequirementRow] = {}

    invoices, paid_by_invoice, advances_by_vendor = _ap_snapshot(
        entity, as_of=cutoff, branch_scope=branch_scope,
    )
    report.unassigned_excluded_count = _unassigned_count(
        VendorInvoice.objects.filter(entity=entity, status="POSTED"), branch_scope,
    )
    for inv in invoices:
        due = int(inv.total) - paid_by_invoice.get(inv.id, 0)
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

    if advances_by_vendor:
        from .models import Vendor

        for vendor in Vendor.objects.filter(
            entity=entity, id__in=advances_by_vendor,
        ).order_by("code", "id"):
            row = rows.get(vendor.id)
            if row is None:
                row = rows[vendor.id] = CashRequirementRow(
                    vendor_id=vendor.id, code=vendor.code, name=vendor.name,
                    buckets={b: 0 for b in FORECAST_BUCKETS},
                )
            row.unallocated_credit += advances_by_vendor[vendor.id]

    for r in rows.values():
        for b in FORECAST_BUCKETS:
            report.bucket_totals[b] += r.buckets[b]
        report.total_due += r.total
        report.total_unallocated_credit += r.unallocated_credit
        r.net_total = r.total - r.unallocated_credit

    report.net_cash_requirement = report.total_due - report.total_unallocated_credit
    report.rows = sorted(rows.values(), key=lambda x: (x.code, x.vendor_id))
    return report


# --------------------------------------------------------------------------- #
# GR/IR aging                                                                 #
# --------------------------------------------------------------------------- #


def _grir_invoice_line_basis(line) -> int:
    """Return the net value this posted invoice line actually debits to GR/IR.

    Keep reporting on the same historical basis as vendor-invoice posting: a linked
    posted receipt snapshot wins, then the PO price, while a truly direct bill never
    clears GR/IR. Callers must eager-load ``grn_line__grn`` and ``po_line``.
    """
    if (
        line.grn_line_id is not None
        and line.grn_line.grn.status == "POSTED"
    ):
        unit_price = line.grn_line.unit_price
    elif line.po_line_id is not None:
        unit_price = line.po_line.unit_price
    else:
        return 0
    return compute_line_net(line.quantity, unit_price)


def _grir_attribution(entity, *, as_of=None, branch_scope=None):
    """Attribute posted invoice clearing to receipt lines, including PO-only FIFO.

    Explicit ``grn_line`` links win and consume that receipt's capacity first. Remaining
    PO-only clearing is split by receipt date/GRN/line order without inventing receipt
    rows for excess invoice-first value.

    ``branch_scope`` narrows both sides, each through its own route to the branch column
    (``grn__`` for receipt lines, ``vendor_invoice__`` for invoice lines).  Both sides must
    move together: narrowing receipts alone would leave a branch's receipts looking
    uncleared because the bills that cleared them were filtered away.  A downstream
    document inherits its source's branch (``views.base._inherited_branch_id``), so a
    receipt and the bill clearing it are in the same sub-scope by construction.
    """
    from collections import defaultdict
    from django.db.models import Q
    from .models import GoodsReceivedNoteLine, VendorInvoiceLine

    receipt_qs = GoodsReceivedNoteLine.objects.filter(
        grn__entity=entity,
    ).select_related("grn", "grn__vendor", "po_line").order_by(
        "grn__received_date", "grn_id", "id",
    )
    invoice_qs = VendorInvoiceLine.objects.filter(
        vendor_invoice__entity=entity,
    ).select_related(
        "vendor_invoice", "grn_line__grn", "po_line",
    ).order_by("vendor_invoice__invoice_date", "vendor_invoice_id", "id")
    if branch_scope is not None:
        receipt_qs = receipt_qs.filter(branch_scope.q("grn__"))
        invoice_qs = invoice_qs.filter(branch_scope.q("vendor_invoice__"))
    if as_of is None:
        receipt_qs = receipt_qs.filter(grn__status="POSTED")
        invoice_qs = invoice_qs.filter(vendor_invoice__status="POSTED")
    else:
        receipt_qs = receipt_qs.filter(
            grn__journal__status__in=("POSTED", "REVERSED"),
            grn__journal__date__lte=as_of,
        ).filter(
            Q(grn__journal__reversed_by__isnull=True)
            | Q(grn__journal__reversed_by__date__gt=as_of)
        )
        invoice_qs = invoice_qs.filter(
            vendor_invoice__journal__status__in=("POSTED", "REVERSED"),
            vendor_invoice__journal__date__lte=as_of,
        ).filter(
            Q(vendor_invoice__journal__reversed_by__isnull=True)
            | Q(vendor_invoice__journal__reversed_by__date__gt=as_of)
        )

    receipts = list(receipt_qs)
    invoice_lines = list(invoice_qs)
    receipt_by_id = {line.id: line for line in receipts}
    remaining = {line.id: int(line.value_amount) for line in receipts}
    receipts_by_po = defaultdict(list)
    for line in receipts:
        if line.po_line_id is not None:
            receipts_by_po[line.po_line_id].append(line)

    by_grn = defaultdict(int)
    evidence_by_grn = defaultdict(dict)
    unattributed_by_po = defaultdict(int)

    def add_evidence(grn_id, invoice, amount):
        by_grn[grn_id] += amount
        row = evidence_by_grn[grn_id].get(invoice.id)
        if row is None:
            row = evidence_by_grn[grn_id][invoice.id] = {
                "id": invoice.id,
                "document_number": invoice.document_number or str(invoice.pk),
                "invoice_date": str(invoice.invoice_date),
                "net": 0,
            }
        row["net"] += amount

    # Explicit receipt links are authoritative and reserve capacity before FIFO.
    for line in invoice_lines:
        if line.grn_line_id is None:
            continue
        receipt = receipt_by_id.get(line.grn_line_id)
        if receipt is None:
            continue
        basis = _grir_invoice_line_basis(line)
        add_evidence(receipt.grn_id, line.vendor_invoice, basis)
        remaining[receipt.id] = max(0, remaining[receipt.id] - basis)

    # PO-only lines consume the still-open receipt capacity in deterministic FIFO order.
    for line in invoice_lines:
        if line.grn_line_id is not None or line.po_line_id is None:
            continue
        left = _grir_invoice_line_basis(line)
        for receipt in receipts_by_po.get(line.po_line_id, ()):
            if left <= 0:
                break
            amount = min(left, remaining[receipt.id])
            if amount <= 0:
                continue
            add_evidence(receipt.grn_id, line.vendor_invoice, amount)
            remaining[receipt.id] -= amount
            left -= amount
        if left > 0:
            unattributed_by_po[line.po_line_id] += left

    return {
        "by_grn": dict(by_grn),
        "evidence_by_grn": {
            grn_id: sorted(rows.values(), key=lambda row: (row["invoice_date"], row["id"]))
            for grn_id, rows in evidence_by_grn.items()
        },
        "unattributed_by_po": dict(unattributed_by_po),
        "receipt_lines": receipts,
        "invoice_lines": invoice_lines,
    }


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
    # Signed normal-balance net of the GR/IR clearing account, and total_open − that
    # (unlinked/manual/legacy noise). Both are None for a branch-narrowed caller: the
    # GL carries no branch, so there is no branch-level control figure to compare against.
    control_balance: int | None = 0
    difference: int | None = 0
    #: Entity-level (branch-less) receipts excluded from a narrowed view; None when not narrowed.
    unassigned_excluded_count: int | None = None


def grir_aging(entity, *, as_of=None, branch_scope=None) -> GRIRAgingReport:
    """Age the open GR/IR clearing balance by goods-receipt date.

    Where :func:`grir_balance` is a single point-in-time figure, this drills it into how
    *long* each received-not-invoiced position has been sitting: per POSTED GRN, the
    received value (credited to GR/IR) less the value of POSTED vendor-invoice lines that
    reference its lines (which debited GR/IR clearing it). The remaining ``open_value`` is
    aged off the GRN's ``received_date``. The GL ``control_balance`` is carried alongside;
    a non-zero ``difference`` flags unlinked, manual, or legacy reconciliation noise the
    GRN walk cannot attribute. Normal purchase-price variance is posted separately and
    therefore does not remain in GR/IR. ``open_value`` is signed: positive means
    received-not-invoiced; negative means the clearing basis exceeded the receipt.
    Bucket totals preserve that sign. All amounts are integer kobo.

    ``branch_scope`` narrows the receipt walk to the GRNs the caller can actually open.
    The GL side is **not** narrowed and is reported as ``None`` instead: the ledger has no
    branch column, so comparing one branch's open receipts against the entity's clearing
    account would manufacture a ``difference`` on every branch-bound read and drown the
    real reconciliation alarm this field exists to raise.  The entity-level control stays
    available, unchanged, on :func:`grir_balance`.
    """
    cutoff = as_of
    as_of = as_of or timezone.now().date()
    report = GRIRAgingReport(entity_id=entity.id, as_of=as_of)
    attribution = _grir_attribution(entity, as_of=cutoff, branch_scope=branch_scope)
    posted_grns = {}
    for line in attribution["receipt_lines"]:
        posted_grns[line.grn_id] = line.grn
    invoiced_by_grn = attribution["by_grn"]

    rows = []
    for grn in sorted(
        posted_grns.values(), key=lambda row: (row.received_date, row.id),
    ):
        invoiced = invoiced_by_grn.get(grn.id, 0)
        # GRN credit less matched invoice debit: positive is an uncleared receipt;
        # negative is an over-clear position and remains visible.
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
    from .models import GoodsReceivedNote

    report.unassigned_excluded_count = _unassigned_count(
        GoodsReceivedNote.objects.filter(entity=entity, status="POSTED"), branch_scope,
    )
    if branch_scope is not None and branch_scope.is_narrowed:
        # No branch-level GL control exists to reconcile against; say so rather than
        # subtract an entity-wide balance from a branch-wide total.
        report.control_balance = None
        report.difference = None
        return report
    control = grir_balance(entity, as_of=cutoff)
    report.control_balance = control
    # Both sides use the GR/IR account's signed normal-balance convention: receipt-heavy
    # positions are positive and invoice-heavy/over-clear positions are negative.
    report.difference = report.total_open - control
    return report


# --------------------------------------------------------------------------- #
# AP and GR/IR drill-downs                                                     #
# --------------------------------------------------------------------------- #


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


def ap_vendor_open_bills(entity, vendor, *, as_of=None, branch_scope=None) -> APVendorDetail:
    """Age one vendor's open bills for the AP drawer - buckets + the invoice list.

    Scoped to a single ``vendor`` (entity-checked by the caller), this mirrors
    :func:`ap_aging`'s per-vendor arithmetic but returns the underlying open invoices too:
    each POSTED, not-fully-paid bill's ``balance_due`` aged off its ``due_date`` (falling
    back to ``invoice_date``). All amounts are integer kobo.

    ``branch_scope`` narrows it exactly as :func:`ap_aging` is narrowed, which is what
    stops the drawer from disagreeing with the row that opened it.  A vendor is entity
    master data shared by every branch, so the vendor itself is never branch-checked;
    only their bills are.
    """
    cutoff = as_of
    as_of = as_of or timezone.now().date()
    detail = APVendorDetail(
        vendor_id=vendor.id, code=vendor.code, name=vendor.name, as_of=as_of,
        buckets={b: 0 for b in AGING_BUCKETS},
    )

    invoices, paid_by_invoice, advances_by_vendor = _ap_snapshot(
        entity, as_of=cutoff, vendor=vendor, branch_scope=branch_scope,
    )
    for inv in sorted(
        invoices, key=lambda row: (row.due_date or row.invoice_date, row.invoice_date, row.id),
    ):
        paid = paid_by_invoice.get(inv.id, 0)
        due = int(inv.total) - paid
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
            balance_due=due, payment_status=_snapshot_payment_status(inv.total, paid),
        ))

    detail.unallocated_credit = advances_by_vendor.get(vendor.id, 0)
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
    invoices: list = field(default_factory=list)   # [{id, document_number, invoice_date, net}] (GR/IR basis)


def grir_grn_detail(entity, grn_id, *, as_of=None, branch_scope=None) -> GRIRGrnDetail | None:
    """The GR/IR position and linked documents for one GRN (drawer detail).

    Returns the GRN's received value, the value of POSTED vendor-invoice lines that
    reference its GRN lines (which cleared it), the remaining ``open_value`` aged off the
    received date, its source PO number, and the distinct matched invoices. Entity-scoped;
    ``None`` when the GRN is not in ``entity``. The entity-qualified lookup is the
    tenant boundary even when a caller guesses a valid foreign ``grn_id``. All amounts
    are integer kobo.

    ``branch_scope`` applies the same narrowing to the lookup, so a receipt in another
    branch returns ``None`` and the view reports it exactly like a receipt that does not
    exist.  Without it this drawer would let a branch-bound caller read a neighbouring
    site's receipt by guessing an id - the id-discovery hole
    ``views.base._document_or_404`` closed on the operational endpoints.
    """
    from django.db.models import Q
    from .models import GoodsReceivedNote

    cutoff = as_of
    as_of = as_of or timezone.now().date()
    grn_qs = GoodsReceivedNote.objects.filter(entity=entity, pk=grn_id)
    if branch_scope is not None:
        grn_qs = grn_qs.filter(branch_scope.q())
    grn = grn_qs.select_related("vendor", "purchase_order").first()
    if grn is None:
        return None
    if cutoff is None:
        if grn.status != "POSTED":
            return None
    else:
        effective = (
            GoodsReceivedNote.objects
            .filter(
                pk=grn.pk, journal__status__in=("POSTED", "REVERSED"),
                journal__date__lte=cutoff,
            )
            .filter(
                Q(journal__reversed_by__isnull=True)
                | Q(journal__reversed_by__date__gt=cutoff)
            )
            .exists()
        )
        if not effective:
            return None

    attribution = _grir_attribution(entity, as_of=cutoff, branch_scope=branch_scope)
    invoiced = attribution["by_grn"].get(grn.id, 0)
    invoices = attribution["evidence_by_grn"].get(grn.id, [])

    days = (as_of - grn.received_date).days
    return GRIRGrnDetail(
        grn_id=grn.id, reference=grn.document_number or str(grn.pk),
        vendor_code=grn.vendor.code, vendor_name=grn.vendor.name,
        received_date=grn.received_date, days=days, bucket=_bucket_for(days),
        po_number=(grn.purchase_order.document_number if grn.purchase_order else ""),
        received_value=grn.total_value, invoiced_value=invoiced,
        open_value=grn.total_value - invoiced,
        invoices=invoices,
    )


#: GR/IR line status labels - mirror the prototype's per-PO-line status chips.
GRIR_LINE_CLEARED = "Cleared"
GRIR_LINE_RECV_GT_INV = "Received > Invoiced"
GRIR_LINE_INV_GT_RECV = "Invoiced > Received"


def _grir_line_status(received_qty, invoiced_qty, balance) -> str:
    """Derive a PO line's GR/IR status from its received vs invoiced quantities.

    Quantity is the headline the table shows, so it leads the derivation; the monetary
    ``balance`` (received value − GR/IR clearing basis) only breaks the tie when the two
    quantities are equal. Normal purchase-price variance posts to PPV and does not affect
    this balance.
    ``balance`` is signed ``received_value - invoiced_value``: positive selects the
    received-heavy label; negative selects invoiced-heavy.
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
    invoiced_value: int     # Σ invoice value clearing GR/IR (kobo)
    grir_balance: int       # received_value − invoiced_value (kobo)
    status: str


@dataclass
class GRIRPoLinesReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)


def grir_po_lines(entity, *, as_of=None, branch_scope=None) -> GRIRPoLinesReport:
    """Line-level GR/IR: per PO line, ordered vs received vs invoiced (qty + value).

    Where :func:`grir_aging` ages the balance per *goods receipt*, this drills it to the
    **PO line** the prototype's GR/IR table lists. For each line on a live PO (CANCELLED /
    REVERSED orders excluded), ``received_qty``/``received_value`` sum the POSTED
    ``GoodsReceivedNoteLine``s pointing at it, while ``invoiced_qty`` and the GR/IR
    clearing-basis ``invoiced_value`` sum POSTED ``VendorInvoiceLine``s pointing at it
    (the direct ``po_line`` FK - the same link that advances invoiced quantity). Only
    lines with any receipt or invoice activity are returned. ``as_of`` cuts both sides
    off by their posted journal dates. All amounts are integer kobo.

    ``branch_scope`` narrows every side through its own route to the order's branch
    (``purchase_order__`` for the lines, ``po_line__purchase_order__`` for both
    aggregates), so a branch-bound caller reconciles only their own orders.  The PO owns
    the branch here rather than the receipt or the bill, because the PO line is the row
    this report returns.
    """
    from collections import defaultdict
    from decimal import Decimal

    from django.db.models import Q, Sum

    from .models import GoodsReceivedNoteLine, PurchaseOrderLine, VendorInvoiceLine

    cutoff = as_of
    as_of = as_of or timezone.now().date()
    report = GRIRPoLinesReport(entity_id=entity.id, as_of=as_of)

    # Live PO lines only - a cancelled/reversed order is not an open GR/IR obligation.
    po_lines = (
        PurchaseOrderLine.objects
        .filter(purchase_order__entity=entity)
        .exclude(purchase_order__status__in=("CANCELLED", "REVERSED"))
        .select_related("purchase_order", "purchase_order__vendor")
        .order_by("purchase_order__order_date", "purchase_order_id", "line_no", "id")
    )
    if branch_scope is not None:
        po_lines = po_lines.filter(branch_scope.q("purchase_order__"))

    # Two bulk aggregates keyed by po_line - no per-line query (avoids N+1).
    # Received side: accepted qty + booked value from POSTED goods-receipt lines.
    recv = defaultdict(lambda: (Decimal(0), 0))
    grn_agg = (
        GoodsReceivedNoteLine.objects
        .filter(po_line__purchase_order__entity=entity)
    )
    if branch_scope is not None:
        grn_agg = grn_agg.filter(branch_scope.q("po_line__purchase_order__"))
    if cutoff is None:
        grn_agg = grn_agg.filter(grn__status="POSTED")
    else:
        grn_agg = grn_agg.filter(
            grn__journal__status__in=("POSTED", "REVERSED"),
            grn__journal__date__lte=cutoff,
        ).filter(
            Q(grn__journal__reversed_by__isnull=True)
            | Q(grn__journal__reversed_by__date__gt=cutoff)
        )
    grn_agg = grn_agg.values("po_line").annotate(
        qty=Sum("accepted_qty"), value=Sum("value_amount"),
    )
    for r in grn_agg:
        recv[r["po_line"]] = (Decimal(r["qty"] or 0), int(r["value"] or 0))

    # Invoiced side: billed qty + GR/IR clearing basis, loaded once for all PO lines.
    inv = defaultdict(lambda: (Decimal(0), 0))
    inv_lines = (
        VendorInvoiceLine.objects
        .filter(po_line__purchase_order__entity=entity)
        .select_related("po_line", "grn_line__grn")
    )
    if branch_scope is not None:
        inv_lines = inv_lines.filter(branch_scope.q("po_line__purchase_order__"))
    if cutoff is None:
        inv_lines = inv_lines.filter(vendor_invoice__status="POSTED")
    else:
        inv_lines = inv_lines.filter(
            vendor_invoice__journal__status__in=("POSTED", "REVERSED"),
            vendor_invoice__journal__date__lte=cutoff,
        ).filter(
            Q(vendor_invoice__journal__reversed_by__isnull=True)
            | Q(vendor_invoice__journal__reversed_by__date__gt=cutoff)
        )
    for invoice_line in inv_lines:
        quantity, value = inv[invoice_line.po_line_id]
        inv[invoice_line.po_line_id] = (
            quantity + Decimal(invoice_line.quantity),
            value + _grir_invoice_line_basis(invoice_line),
        )

    rows = []
    for line in po_lines:
        received_qty, received_value = recv.get(line.id, (Decimal(0), 0))
        invoiced_qty, invoiced_value = inv.get(line.id, (Decimal(0), 0))
        # Skip lines with no receipt and no invoice - nothing to reconcile yet.
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
    invoices: list = field(default_factory=list)  # [{..., quantity, net}] where net is GR/IR basis


def grir_po_line_detail(entity, po_line_id, *, as_of=None, branch_scope=None) -> GRIRPoLineDetail | None:
    """The GR/IR reconciliation and linked documents for a single PO line (drawer).

    Entity-scoped: a PO line on another entity's order returns ``None`` (the view 404s,
    never leaks). Lists each POSTED goods-receipt line and POSTED vendor-invoice line that
    references this PO line, alongside the received/invoiced/balance reconciliation. All
    amounts are integer kobo.

    ``branch_scope`` narrows the same resolution, so a line on another branch's order is
    reported exactly like a line that does not exist.  Applying it once here is enough:
    the receipts and bills below are reached through this line, and a document inherits
    its source's branch, so they cannot belong to a different sub-scope.
    """
    from decimal import Decimal
    from django.db.models import Q

    from .models import GoodsReceivedNoteLine, PurchaseOrderLine, VendorInvoiceLine

    line_qs = (
        PurchaseOrderLine.objects
        # Scope through the parent PO's entity so a foreign line id cannot be read.
        .filter(purchase_order__entity=entity, pk=po_line_id)
    )
    if branch_scope is not None:
        line_qs = line_qs.filter(branch_scope.q("purchase_order__"))
    line = line_qs.select_related("purchase_order", "purchase_order__vendor").first()
    if line is None:
        return None

    received_qty, received_value = Decimal(0), 0
    grns = []
    grn_lines = (
        GoodsReceivedNoteLine.objects
        .filter(po_line=line)
        .select_related("grn")
        .order_by("grn__received_date", "grn_id", "id")
    )
    if as_of is None:
        grn_lines = grn_lines.filter(grn__status="POSTED")
    else:
        grn_lines = grn_lines.filter(
            grn__journal__status__in=("POSTED", "REVERSED"),
            grn__journal__date__lte=as_of,
        ).filter(
            Q(grn__journal__reversed_by__isnull=True)
            | Q(grn__journal__reversed_by__date__gt=as_of)
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
        .filter(po_line=line)
        .select_related("vendor_invoice", "po_line", "grn_line__grn")
        .order_by("vendor_invoice__invoice_date", "vendor_invoice_id", "id")
    )
    if as_of is None:
        inv_lines = inv_lines.filter(vendor_invoice__status="POSTED")
    else:
        inv_lines = inv_lines.filter(
            vendor_invoice__journal__status__in=("POSTED", "REVERSED"),
            vendor_invoice__journal__date__lte=as_of,
        ).filter(
            Q(vendor_invoice__journal__reversed_by__isnull=True)
            | Q(vendor_invoice__journal__reversed_by__date__gt=as_of)
        )
    for il in inv_lines:
        invoiced_qty += Decimal(il.quantity)
        clearing_basis = _grir_invoice_line_basis(il)
        invoiced_value += clearing_basis
        vi = il.vendor_invoice
        invoices.append({
            "id": vi.id,
            "document_number": vi.document_number or str(vi.id),
            "invoice_date": str(vi.invoice_date),
            "quantity": str(il.quantity),
            "net": clearing_basis,
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


def grir_balance(entity, *, as_of=None) -> int:
    """Net balance of the GR/IR clearing account for ``entity`` (kobo, normal-balance signed).

    The GR/IR control nets to **zero** when every received good has been invoiced (and
    vice-versa). Because GR/IR is normally credit, a positive result is received-not-
    invoiced and a negative result is a net debit/invoice-first position - the headline
    number a GR/IR aging drills into.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey
    from vs_finance.exceptions import MissingAccountError

    try:
        account = resolve_mapped_account(entity, AccountMappingKey.GRIR_CLEARING)
    except MissingAccountError:
        return 0
    return (
        _account_gl_net_as_of(account, as_of)
        if as_of is not None else _account_gl_net(account)
    )


# --------------------------------------------------------------------------- #
# Procurement analytics - spend, vendor performance, PR→payment cycle time     #
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

    period: str          # "YYYY-MM" - sorts chronologically as a plain string
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
    #: Entity-level (branch-less) bills excluded from a narrowed view; None when not narrowed.
    unassigned_excluded_count: int | None = None


def spend_analysis(entity, *, start_date=None, end_date=None, vendor=None, category=None,
                   branch_scope=None) -> SpendAnalysis:
    """Analyse realised spend for ``entity`` from POSTED vendor invoices.

    Spend is the gross of POSTED :class:`VendorInvoice` s whose ``invoice_date`` falls
    in ``[start_date, end_date]`` (either bound optional). Rows are returned both by
    vendor and by vendor category (uncategorised vendors roll into an "Uncategorised"
    bucket), each sorted by descending gross spend. Pass ``vendor`` to scope the whole
    computation to a single supplier; pass ``category`` (a category code, or the literal
    ``"UNCATEGORISED"``) to scope it to one purchasing category - the per-category drawer
    reuses this so its by_vendor / by_period reflect only that category. Both supplied
    date bounds are inclusive, and every amount remains gross/net/tax integer kobo.

    ``branch_scope`` (``views.base._BranchScope``) narrows the population to the bills the
    caller can actually open, so a branch-bound viewer's spend equals the sum of their own
    vendor-invoice list.  Omitted, the analysis stays entity-wide exactly as before.
    """
    from vs_finance.constants import DocumentStatus

    from .models import VendorInvoice

    # Built without the branch term first, so the same window/category/vendor population
    # can also answer how many of its bills sit at entity level. Deriving both from one
    # queryset is what stops the excluded count from describing a different population
    # than the totals beside it.
    population = (
        VendorInvoice.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor", "vendor__category")
    )
    if vendor is not None:
        population = population.filter(vendor=vendor)
    if category is not None:
        # "UNCATEGORISED" is the synthetic key for vendors with no category (mirrors the
        # by_category grouping below); a real code matches the vendor's category code.
        if category == "UNCATEGORISED":
            population = population.filter(vendor__category__isnull=True)
        else:
            population = population.filter(vendor__category__code=category)
    if start_date is not None:
        population = population.filter(invoice_date__gte=start_date)
    if end_date is not None:
        population = population.filter(invoice_date__lte=end_date)

    qs = population
    if branch_scope is not None:
        qs = qs.filter(branch_scope.q())

    vendors: dict = {}
    categories: dict = {}
    periods: dict = {}
    report = SpendAnalysis(entity_id=entity.id, start_date=start_date, end_date=end_date)
    report.unassigned_excluded_count = _unassigned_count(population, branch_scope)

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

    report.by_vendor = sorted(vendors.values(), key=lambda r: (-r.gross, r.key))
    report.by_category = sorted(categories.values(), key=lambda r: (-r.gross, r.key))
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
        """On-time share of receipts that had an expected date, not all receipts."""
        rated = self.on_time_receipts + self.late_receipts
        return round(self.on_time_receipts / rated, 4) if rated else None


@dataclass
class VendorPerformanceReport:
    entity_id: int
    start_date: object
    end_date: object
    rows: list = field(default_factory=list)
    #: Entity-level (branch-less) bills excluded from a narrowed view; None when not narrowed.
    unassigned_excluded_count: int | None = None


def vendor_performance(entity, *, start_date=None, end_date=None, vendor=None,
                       branch_scope=None) -> VendorPerformanceReport:
    """Blend ordering, delivery timeliness and payment speed per vendor.

    For each vendor with activity in ``[start_date, end_date]``:

    * **Ordering** - count and value of POs (``order_date`` in window, excluding
      CANCELLED / REVERSED).
    * **Delivery** - POSTED goods receipts (``received_date`` in window) classified
      on-time vs late against their PO's ``expected_date`` (receipts whose PO has no
      expected date are not rated).
    * **Billing & payment** - POSTED vendor invoices and the average days from
      ``invoice_date`` to each allocating payment's ``payment_date``. The denominator is
      allocation rows, while ``payment_count`` de-duplicates payment documents per vendor.

    Date bounds are inclusive and are applied to the date owned by each metric (PO order,
    GRN receipt, or invoice date); the attached assessment is the latest overall snapshot,
    not constrained to the activity window.

    ``branch_scope`` narrows every evidence population to the documents the caller can
    open, each through its own route to the branch column: orders, receipts and bills
    carry it directly, while a payment allocation reaches it through ``payment__``.
    Allocations follow the **payment's** branch, matching the vendor-payment list the
    caller sees.  ``latest_assessment`` is deliberately left alone: an assessment is a
    scorecard of the vendor, which is entity master data every branch shares, and it has
    no branch column of its own to narrow by.
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
    if branch_scope is not None:
        po_qs = po_qs.filter(branch_scope.q())
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
    if branch_scope is not None:
        grn_qs = grn_qs.filter(branch_scope.q())
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

    inv_population = (
        VendorInvoice.objects
        .filter(entity=entity, status=DocumentStatus.POSTED)
        .select_related("vendor")
    )
    if vendor is not None:
        inv_population = inv_population.filter(vendor=vendor)
    if start_date is not None:
        inv_population = inv_population.filter(invoice_date__gte=start_date)
    if end_date is not None:
        inv_population = inv_population.filter(invoice_date__lte=end_date)
    # Billing is the report's headline (total_billed is also the row sort key), so the
    # bill population is the one whose entity-level remainder is worth reporting.
    report_unassigned = _unassigned_count(inv_population, branch_scope)
    inv_qs = inv_population
    if branch_scope is not None:
        inv_qs = inv_qs.filter(branch_scope.q())
    pay_days: dict = {}
    for inv in inv_qs:
        r = row_for(inv.vendor)
        r.invoice_count += 1
        r.total_billed += inv.total

    # Average days-to-pay: invoice_date → allocating payment date. Each allocation is a
    # sample, so a bill paid in instalments contributes once per payment allocation.
    alloc_qs = (
        VendorPaymentAllocation.objects
        .filter(payment__entity=entity, payment__status=DocumentStatus.POSTED)
        .select_related("payment", "payment__vendor", "vendor_invoice")
    )
    if branch_scope is not None:
        alloc_qs = alloc_qs.filter(branch_scope.q("payment__"))
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

    # Attach each vendor's category name - one query for every row's vendor (no N+1),
    # used as the performance table's per-vendor subtitle.
    if rows:
        from .models import Vendor

        for v in Vendor.objects.filter(id__in=list(rows.keys())).select_related("category"):
            rows[v.id].category = v.category.name if v.category_id else ""

    # Attach each vendor's most-recent point-in-time assessment (one query for all
    # vendors). Descending assessment_date/id makes the first row per vendor authoritative
    # when multiple assessments share a day.
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

    ordered_rows = sorted(
        rows.values(), key=lambda r: (-r.total_billed, r.code, r.vendor_id),
    )
    return VendorPerformanceReport(
        entity_id=entity.id, start_date=start_date, end_date=end_date, rows=ordered_rows,
        unassigned_excluded_count=report_unassigned,
    )


@dataclass
class CycleStage:
    """Average duration of one hop in the P2P chain (whole days)."""

    name: str
    label: str
    sample_count: int = 0
    avg_days: float | None = None
    excluded_count: int = 0


@dataclass
class ProcurementCycleTime:
    """Average per-stage and end-to-end durations of the procure-to-pay chain."""

    entity_id: int
    start_date: object
    end_date: object
    stages: list = field(default_factory=list)
    end_to_end_avg_days: float | None = None
    end_to_end_count: int = 0
    end_to_end_excluded_count: int = 0
    #: Entity-level (branch-less) settling payments excluded from a narrowed view.
    unassigned_excluded_count: int | None = None


def procurement_cycle_time(entity, *, start_date=None, end_date=None,
                           branch_scope=None) -> ProcurementCycleTime:
    """Measure how long each hop of the procure-to-pay chain takes, on average.

    Walks every settling payment back through its bill → PO → requisition and averages
    the elapsed days of each hop:

    * **req → PO**     requisition ``request_date`` → PO ``order_date``
    * **PO → receipt** PO ``order_date`` → first POSTED goods receipt ``received_date``
    * **receipt → invoice** receipt ``received_date`` → bill ``invoice_date``
    * **invoice → payment** bill ``invoice_date`` → payment ``payment_date``

    The chain is anchored on the date cumulative posted allocations fully settle the
    invoice (that settlement date must fall in the inclusive window). Partially settled
    invoices are omitted. Each hop is counted only when both endpoints exist; impossible
    negative hops are excluded and counted separately without suppressing other valid
    stages. ``end_to_end`` is requisition → full settlement for complete chains. PO
    timing uses the earliest POSTED receipt.

    ``branch_scope`` narrows the chains to the caller's sub-scope.  The chain is anchored
    on the settling payment, so the payment's branch decides (``payment__``), and the
    receipt cache is narrowed to match; a document inherits its source's branch, so a
    chain never straddles two branches.  A payment that settled bills from two branches
    resolves to no branch at all (``views.base._inherited_branch_id``) and so belongs to
    neither branch's cycle time, which is the honest answer rather than counting it twice.
    """
    from vs_finance.constants import DocumentStatus

    from .models import GoodsReceivedNote, VendorPayment, VendorPaymentAllocation

    req_to_po: list = []
    po_to_receipt: list = []
    receipt_to_invoice: list = []
    invoice_to_payment: list = []
    end_to_end: list = []
    excluded = {
        "req_to_po": 0,
        "po_to_receipt": 0,
        "receipt_to_invoice": 0,
        "invoice_to_payment": 0,
        "end_to_end": 0,
    }

    def add_duration(stage, values, value):
        """Keep valid evidence while making impossible negative hops observable."""
        if value < 0:
            excluded[stage] += 1
        else:
            values.append(value)

    # Cache the earliest POSTED receipt per PO so we don't re-query in the loop.
    # Chronological ordering plus setdefault deliberately keeps the first receipt only.
    first_receipt: dict = {}
    grn_qs = (
        GoodsReceivedNote.objects
        .filter(entity=entity, status=DocumentStatus.POSTED, purchase_order__isnull=False)
        .order_by("received_date", "id")
    )
    if branch_scope is not None:
        grn_qs = grn_qs.filter(branch_scope.q())
    for grn in grn_qs:
        first_receipt.setdefault(grn.purchase_order_id, grn.received_date)

    alloc_qs = (
        VendorPaymentAllocation.objects
        .filter(
            payment__entity=entity,
            payment__status=DocumentStatus.POSTED,
            vendor_invoice__status=DocumentStatus.POSTED,
        )
        .select_related(
            "payment", "vendor_invoice", "vendor_invoice__purchase_order",
            "vendor_invoice__purchase_order__requisition",
        )
        .order_by("vendor_invoice_id", "payment__payment_date", "payment_id", "id")
    )
    if branch_scope is not None:
        alloc_qs = alloc_qs.filter(branch_scope.q("payment__"))
    settled: dict = {}
    for alloc in alloc_qs:
        inv = alloc.vendor_invoice
        state = settled.setdefault(inv.id, {"amount": 0, "allocation": None})
        if state["allocation"] is not None:
            continue
        state["amount"] += int(alloc.amount)
        if state["amount"] >= int(inv.total):
            state["allocation"] = alloc

    for state in settled.values():
        alloc = state["allocation"]
        if alloc is None:
            # Partially paid invoices have no completed procurement-to-payment cycle.
            continue
        pay = alloc.payment
        if start_date is not None and pay.payment_date < start_date:
            continue
        if end_date is not None and pay.payment_date > end_date:
            continue
        inv = alloc.vendor_invoice

        add_duration(
            "invoice_to_payment", invoice_to_payment,
            (pay.payment_date - inv.invoice_date).days,
        )

        po = inv.purchase_order
        receipt_date = first_receipt.get(po.id) if po else None
        if receipt_date is not None:
            add_duration(
                "receipt_to_invoice", receipt_to_invoice,
                (inv.invoice_date - receipt_date).days,
            )
        if po is not None:
            if receipt_date is not None:
                add_duration(
                    "po_to_receipt", po_to_receipt,
                    (receipt_date - po.order_date).days,
                )
            req = po.requisition
            if req is not None:
                add_duration(
                    "req_to_po", req_to_po,
                    (po.order_date - req.request_date).days,
                )
                if receipt_date is not None:
                    # End-to-end is stricter than the independent stage samples: it
                    # represents one valid chronological chain, not merely a positive
                    # subtraction between the first and last dates.
                    chain_dates = (
                        req.request_date, po.order_date, receipt_date,
                        inv.invoice_date, pay.payment_date,
                    )
                    if all(
                        earlier <= later
                        for earlier, later in zip(chain_dates, chain_dates[1:])
                    ):
                        end_to_end.append(
                            (pay.payment_date - req.request_date).days,
                        )
                    else:
                        excluded["end_to_end"] += 1

    stages = [
        CycleStage("req_to_po", "Requisition → PO",
                   len(req_to_po), _avg_days(req_to_po), excluded["req_to_po"]),
        CycleStage("po_to_receipt", "PO → Goods receipt",
                   len(po_to_receipt), _avg_days(po_to_receipt), excluded["po_to_receipt"]),
        CycleStage("receipt_to_invoice", "Goods receipt → Invoice",
                   len(receipt_to_invoice), _avg_days(receipt_to_invoice),
                   excluded["receipt_to_invoice"]),
        CycleStage("invoice_to_payment", "Invoice → Payment",
                   len(invoice_to_payment), _avg_days(invoice_to_payment),
                   excluded["invoice_to_payment"]),
    ]
    return ProcurementCycleTime(
        entity_id=entity.id, start_date=start_date, end_date=end_date,
        stages=stages,
        end_to_end_avg_days=_avg_days(end_to_end),
        end_to_end_count=len(end_to_end),
        end_to_end_excluded_count=excluded["end_to_end"],
        unassigned_excluded_count=_unassigned_count(
            # The chain is anchored on the settling payment, so that is the population
            # whose entity-level remainder a narrowed caller is not seeing.
            VendorPayment.objects.filter(entity=entity, status=DocumentStatus.POSTED),
            branch_scope,
        ),
    )
