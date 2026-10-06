"""Payables corrections: vendor credit notes, bill voids and goods returns.

A posted bill or goods receipt is never edited. What was booked stays on the record,
and a mistake is corrected by a further document whose journal undoes the part that
was wrong. These are the AP mirror of :mod:`vs_finance.voids` and
:mod:`vs_finance.credit_notes`:

* **Vendor credit note** (``Dr AP / Dr vendor advances, Cr GR/IR, PPV, expense and
  input tax``) credits one posted bill, in full, by an amount spread across its lines,
  or line by line. It settles what the bill still owes, and anything beyond that (the
  bill was already paid) becomes vendor credit in the vendor-advance asset of the
  bill's branch, which :func:`allocate_vendor_credit_note` later applies to another
  bill of that branch. It is approved through ``vs_workflow`` like the bill itself.
* **Bill void** reverses a bill nothing stands on: no cash settled against it and no
  credit note raised on it. It gives the PO its invoiced quantities back. A bill with
  money paid against it is corrected with a credit note instead.
* **Goods return** takes goods back out of a posted receipt, by line, at the receipt's
  own price (``Dr GR/IR, Cr inventory/expense``), with the stock leaving at the cost it
  came in at and the PO's received quantity reduced. Only goods not yet billed can go;
  a billed quantity is credited on the bill first.

Every document carries the branch of what it corrects, so a settlement or a credit
lands on the branch that owed the bill or received the goods. Each service keeps the
AP sub-ledger and the AP control moving together in one transaction, which is what
keeps the AP reconciliation at the period close passing after a correction.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Sum

from vs_finance.audit import record, record_rejection
from vs_finance.constants import (
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from vs_finance.exceptions import FinanceError, PostingError
from vs_finance.money import format_naira
from vs_finance.posting import post_journal, resolve_period, reverse_journal
from vs_finance.receivables import compute_line_net

from .constants import (
    GRIR_CLEARING_CODE,
    PURCHASE_PRICE_VARIANCE_CODE,
    VENDOR_ADVANCE_CODE,
    MatchStatus,
    ProcApprovalState,
    StockMovementType,
)
from .exceptions import (
    GoodsReturnError,
    SettlementBranchError,
    VendorCreditNoteError,
    VendorInvoiceVoidError,
)
from .purchasing import resolve_account
from vs_finance.wording import state_word


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #

def _half_up(value: Decimal) -> int:
    """Round a Decimal kobo figure half-up to a whole kobo."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _label(document) -> str:
    """The number a person knows ``document`` by."""
    return document.document_number or str(document.pk)


def _run_with_rejection(document, action, worker, **kwargs):
    """Run ``worker`` and record a durable rejection audit row when it refuses."""
    try:
        return worker(document, **kwargs)
    except FinanceError as exc:
        record_rejection(
            entity=document.entity, action=action, exc=exc,
            actor_user=kwargs.get("actor_user"), target=document,
        )
        raise


def rematch_open_bills(po_line_ids, *, exclude_invoice_id=None) -> None:
    """Re-run the three-way match on draft bills that bill any of ``po_line_ids``.

    A correction moves a PO line's received or invoiced quantity, and a draft bill's
    stored match outcome was computed against the old figure: a bill blocked as
    over-billed may now fit, or one that fitted may now be short. Bills never matched
    (``NOT_MATCHED``) are left for their own match run.
    """
    from .models import VendorInvoice
    from .payables import match_vendor_invoice

    if not po_line_ids:
        return
    drafts = VendorInvoice.objects.filter(
        status=DocumentStatus.DRAFT, lines__po_line_id__in=list(po_line_ids),
    ).exclude(match_status=MatchStatus.NOT_MATCHED).distinct()
    if exclude_invoice_id is not None:
        drafts = drafts.exclude(pk=exclude_invoice_id)
    for bill in drafts:
        match_vendor_invoice(bill, save=True)


def _quantity_factor(entity) -> Decimal:
    """The entity's received/ordered quantity tolerance as a multiplier."""
    from .settings import resolve_procurement_settings

    policy = resolve_procurement_settings(entity)
    return Decimal(10000 + policy.quantity_tolerance_bps) / Decimal(10000)


# --------------------------------------------------------------------------- #
# Vendor credit note: drafting                                                #
# --------------------------------------------------------------------------- #

def proportional_tax(invoice_line, net: int) -> int:
    """The bill line's own input tax on ``net`` kobo of its net.

    Proportional rather than recomputed from the tax code's rate, so a full credit
    reverses exactly the tax the bill booked even if the rate has changed since.
    """
    if not invoice_line.tax_amount or not invoice_line.net_amount:
        return 0
    if int(net) == int(invoice_line.net_amount):
        return int(invoice_line.tax_amount)
    return _half_up(
        Decimal(int(invoice_line.tax_amount)) * Decimal(int(net))
        / Decimal(int(invoice_line.net_amount))
    )


def credited_by_line(invoice, *, exclude_note_id=None) -> dict:
    """``{bill line id: (quantity, net, tax)}`` already credited by posted notes."""
    from .models import VendorCreditNoteLine

    rows = VendorCreditNoteLine.objects.filter(
        invoice_line__vendor_invoice=invoice, credit_note__status=DocumentStatus.POSTED,
    )
    if exclude_note_id is not None:
        rows = rows.exclude(credit_note_id=exclude_note_id)
    return {
        row["invoice_line_id"]: (
            Decimal(row["q"] or 0), int(row["n"] or 0), int(row["t"] or 0),
        )
        for row in rows.values("invoice_line_id").annotate(
            q=Sum("quantity"), n=Sum("net_amount"), t=Sum("tax_amount"),
        )
    }


def _remaining_by_line(invoice, *, exclude_note_id=None) -> list:
    """``[(bill line, quantity, net, tax)]`` each bill line still has to credit."""
    credited = credited_by_line(invoice, exclude_note_id=exclude_note_id)
    rows = []
    for line in invoice.lines.order_by("line_no", "pk"):
        cq, cn, ct = credited.get(line.pk, (Decimal(0), 0, 0))
        rows.append((
            line, Decimal(line.quantity) - cq,
            int(line.net_amount) - cn, int(line.tax_amount) - ct,
        ))
    return rows


def _line_name(line) -> str:
    return f"'{line.description}'" if line.description else f"line {line.line_no or line.pk}"


def _plan_full(remaining) -> list:
    """Credit everything each bill line has left, quantities included."""
    return [
        (line, max(qty, Decimal(0)), net, tax)
        for line, qty, net, tax in remaining if net > 0 or tax > 0
    ]


def _plan_amount(remaining, amount: int) -> list:
    """Spread a gross ``amount`` across the bill's lines in proportion to what is left.

    A value-only credit (no quantity), which is what a price allowance or a discount
    agreed after billing is. Each line's share is split into net and tax in the line's
    own net-to-tax proportion, and every kobo lands somewhere: the shares are floored
    and the residue is handed out one kobo at a time to lines with room.
    """
    open_rows = [(line, net, tax) for line, _q, net, tax in remaining if net + tax > 0]
    available = sum(net + tax for _line, net, tax in open_rows)
    if amount <= 0:
        raise VendorCreditNoteError("A credit amount must be greater than zero.")
    if amount > available:
        raise VendorCreditNoteError(
            f"This bill has {format_naira(available)} left to credit, so a credit of "
            f"{format_naira(amount)} is more than it can take.",
        )
    shares = [amount * (net + tax) // available for _line, net, tax in open_rows]
    residue = amount - sum(shares)
    index = 0
    while residue > 0:
        _line, net, tax = open_rows[index % len(open_rows)]
        if shares[index % len(open_rows)] < net + tax:
            shares[index % len(open_rows)] += 1
            residue -= 1
        index += 1
    plan = []
    for (line, net, tax), share in zip(open_rows, shares):
        if share <= 0:
            continue
        tax_part = min(tax, _half_up(Decimal(share) * Decimal(tax) / Decimal(net + tax)))
        net_part = share - tax_part
        if net_part > net:
            net_part, tax_part = net, share - net
        plan.append((line, Decimal(0), net_part, tax_part))
    return plan


def _plan_lines(remaining, requested) -> list:
    """Credit named bill lines, each by a quantity, a net amount, or both.

    A quantity alone is valued at the bill line's own unit price. The tax follows
    the net in the bill line's proportion (:func:`proportional_tax`).
    """
    by_id = {line.pk: (line, qty, net, tax) for line, qty, net, tax in remaining}
    seen = set()
    plan = []
    for item in requested:
        line_id, quantity, net = item["invoice_line"], item.get("quantity"), item.get("net_amount")
        if line_id not in by_id:
            raise VendorCreditNoteError("Every credited line must be a line of the bill being credited.")
        if line_id in seen:
            raise VendorCreditNoteError("Name each bill line once in a credit note.")
        seen.add(line_id)
        line, left_qty, left_net, left_tax = by_id[line_id]
        quantity = Decimal(quantity or 0)
        if quantity < 0:
            raise VendorCreditNoteError("A credited quantity cannot be negative.")
        if net is None:
            if quantity <= 0:
                raise VendorCreditNoteError(
                    f"Give a quantity or a net amount to credit on {_line_name(line)}.",
                )
            net = compute_line_net(quantity, line.unit_price)
        net = int(net)
        if net <= 0:
            raise VendorCreditNoteError(f"The credit on {_line_name(line)} must be greater than zero.")
        if quantity > left_qty:
            raise VendorCreditNoteError(
                f"Only {left_qty.normalize()} of {_line_name(line)} is left to credit.",
            )
        if net > left_net:
            raise VendorCreditNoteError(
                f"Only {format_naira(left_net)} of net is left to credit on {_line_name(line)}.",
            )
        plan.append((line, quantity, net, min(left_tax, proportional_tax(line, net))))
    return plan


def write_credit_note_lines(note, *, full=False, amount=None, lines=None) -> None:
    """Replace a draft note's lines from exactly one of three instructions.

    ``full`` credits everything the bill has left, quantities included, which is what
    a bill keyed in error or a duplicate needs. ``amount`` spreads a gross figure
    across the lines as a price allowance. ``lines`` names bill lines one by one,
    each with a ``quantity`` (goods credited, which lowers the PO's invoiced quantity)
    and/or a ``net_amount``. What is left to credit excludes other posted notes; the
    posting service checks it again under the bill's lock.
    """
    from .models import VendorCreditNoteLine

    chosen = [bool(full), amount is not None, lines is not None]
    if sum(chosen) != 1:
        raise VendorCreditNoteError(
            "Say what to credit in one way: the whole bill, an amount, or its lines.",
        )
    remaining = _remaining_by_line(note.vendor_invoice, exclude_note_id=note.pk)
    if full:
        plan = _plan_full(remaining)
    elif amount is not None:
        plan = _plan_amount(remaining, int(amount))
    else:
        plan = _plan_lines(remaining, lines)
    if not plan:
        raise VendorCreditNoteError("This bill has nothing left to credit.")

    note.lines.all().delete()
    for number, (line, quantity, net, tax) in enumerate(plan, start=1):
        VendorCreditNoteLine.objects.create(
            credit_note=note, invoice_line=line, line_no=number,
            description=line.description, quantity=quantity,
            net_amount=net, tax_amount=tax,
        )
    note.recompute_totals(save=True)


# --------------------------------------------------------------------------- #
# Vendor credit note: posting                                                 #
# --------------------------------------------------------------------------- #

def post_vendor_credit_note(note, *, actor_user=None):
    """Post an approved draft credit note, settling its bill and booking the rest.

    Records a durable rejection audit row on any refusal, then re-raises.
    """
    return _run_with_rejection(
        note, FinanceAuditAction.VENDOR_CREDIT_NOTE_POSTED,
        _post_vendor_credit_note_atomic, actor_user=actor_user,
    )


def _check_credit_caps(note, invoice, lines) -> None:
    """Refuse a note that would credit a bill line past what the line booked.

    Run under the bill's lock, against every other posted note, because two notes
    drafted side by side each fit on their own and together could credit more than
    was ever billed.
    """
    credited = credited_by_line(invoice, exclude_note_id=note.pk)
    asked: dict[int, list] = defaultdict(lambda: [Decimal(0), 0, 0])
    for line in lines:
        slot = asked[line.invoice_line_id]
        slot[0] += Decimal(line.quantity)
        slot[1] += int(line.net_amount)
        slot[2] += int(line.tax_amount)
    for line in lines:
        bill_line = line.invoice_line
        if bill_line.vendor_invoice_id != invoice.pk:
            raise VendorCreditNoteError("Every credited line must be a line of the bill being credited.")
        cq, cn, ct = credited.get(bill_line.pk, (Decimal(0), 0, 0))
        q, n, t = asked[bill_line.pk]
        if (cq + q > Decimal(bill_line.quantity) or cn + n > int(bill_line.net_amount)
                or ct + t > int(bill_line.tax_amount)):
            raise VendorCreditNoteError(
                f"This note credits {_line_name(bill_line)} of bill {_label(invoice)} "
                f"for more than the bill has left on it once other posted credit notes "
                f"are counted. Rewrite the note from what is left.",
            )


@transaction.atomic
def _post_vendor_credit_note_atomic(note, *, actor_user=None):
    """Raise the note's journal, settle its bill and move PO quantities, as one unit.

    Lock order is note, bill, then PO lines by primary key: the bill before its PO
    lines, which is the order bill posting takes them in.

    The journal undoes each credited line the way the bill booked it. A line of a
    PO- or receipt-backed bill credited by quantity clears GR/IR at the receipt basis
    of those units, and the difference between that basis and the net credited goes
    to purchase price variance; one credited by value only is a price allowance and
    goes to purchase price variance whole, because the goods and their GR/IR entry are
    untouched. A non-PO line credits its own expense account. The input tax follows.
    The debit is split at source, as a vendor payment's is: AP for what the bill still
    owes, and the vendor-advance asset for the rest.
    """
    from vs_finance.chronology import ensure_on_or_after
    from vs_finance.models import JournalEntry, JournalLine

    from .models import (
        PurchaseOrderLine, VendorCreditNote, VendorCreditNoteAllocation, VendorInvoice,
    )

    note = VendorCreditNote.objects.select_for_update().get(pk=note.pk)
    invoice = VendorInvoice.objects.select_for_update().get(pk=note.vendor_invoice_id)
    lines = list(note.lines.select_related(
        "invoice_line__po_line", "invoice_line__grn_line__grn",
        "invoice_line__expense_account", "invoice_line__tax_code__paid_account",
    ).order_by("line_no", "pk"))
    po_line_ids = sorted({
        line.invoice_line.po_line_id for line in lines
        if line.invoice_line.po_line_id and line.quantity
    })
    locked_po_lines = {
        row.pk: row for row in PurchaseOrderLine.objects.select_for_update()
        .filter(pk__in=po_line_ids).order_by("pk")
    }

    if note.status != DocumentStatus.DRAFT:
        raise VendorCreditNoteError(
            f"Vendor credit note {_label(note)} is {state_word(note)}; only a draft can be posted.",
        )
    if note.approval_state != ProcApprovalState.APPROVED:
        raise VendorCreditNoteError(
            f"Vendor credit note {_label(note)} must be approved before posting.",
        )
    if invoice.status != DocumentStatus.POSTED:
        raise VendorCreditNoteError(
            f"Bill {_label(invoice)} is {state_word(invoice)}; only a posted bill can be credited.",
        )
    if (note.entity_id, note.vendor_id, note.branch_id) != (
            invoice.entity_id, invoice.vendor_id, invoice.branch_id):
        raise VendorCreditNoteError(
            "A credit note must carry its bill's vendor and branch.",
        )
    if not lines:
        raise VendorCreditNoteError("A vendor credit note must have at least one line to post.")
    note.recompute_totals(save=True)
    if note.total <= 0:
        raise VendorCreditNoteError("A vendor credit note must have a positive total to post.")
    ensure_on_or_after(
        subject=f"Vendor credit note {_label(note)}", subject_date=note.note_date,
        source=f"bill {_label(invoice)}", source_date=invoice.invoice_date,
        remedy=f"Date the credit note {invoice.invoice_date} or later.",
    )
    _check_credit_caps(note, invoice, lines)

    vendor = invoice.vendor
    ap_account = vendor.payable_account
    if ap_account is None:
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")

    grir_total = 0
    ppv_by_cost_center: dict[int | None, int] = defaultdict(int)
    expense_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    expense_objs: dict[int, object] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in lines:
        bill_line = line.invoice_line
        receipt_backed = (
            bill_line.grn_line_id is not None
            and bill_line.grn_line.grn.status == DocumentStatus.POSTED
        )
        if bill_line.po_line_id is not None or receipt_backed:
            if line.quantity > 0:
                basis_price = (
                    bill_line.grn_line.unit_price if receipt_backed
                    else bill_line.po_line.unit_price
                )
                basis = compute_line_net(line.quantity, basis_price)
                grir_total += basis
                ppv_by_cost_center[bill_line.cost_center_id] += int(line.net_amount) - basis
            else:
                ppv_by_cost_center[bill_line.cost_center_id] += int(line.net_amount)
        else:
            key = (bill_line.expense_account_id, bill_line.cost_center_id)
            expense_by_key[key] += int(line.net_amount)
            expense_objs[bill_line.expense_account_id] = bill_line.expense_account
        if line.tax_amount:
            tax_account = bill_line.tax_code.paid_account if bill_line.tax_code_id else None
            if tax_account is None:
                raise PostingError(
                    f"Tax code '{bill_line.tax_code.code}' has no paid (input/recoverable) "
                    f"account set." if bill_line.tax_code_id
                    else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_account.id] += int(line.tax_amount)
            tax_objs[tax_account.id] = tax_account

    applied = min(int(note.total), max(0, invoice.balance_due))
    advance = int(note.total) - applied

    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch, date=note.note_date,
        period=resolve_period(note.entity, note.note_date),
        source=JournalSource.PURCHASE, currency=note.currency,
        narration=note.reason or f"Vendor credit note {_label(note)}",
        reference=note.vendor_reference, created_by=actor_user,
    )
    journal_lines = []
    if applied:
        journal_lines.append((ap_account, applied, 0, None, f"AP: {vendor.code}"))
    if advance:
        journal_lines.append((
            resolve_account(note.entity, VENDOR_ADVANCE_CODE, label="vendor advances"),
            advance, 0, None, f"Vendor credit: {vendor.code}",
        ))
    if grir_total:
        journal_lines.append((
            resolve_account(note.entity, GRIR_CLEARING_CODE, label="GR/IR clearing"),
            0, grir_total, None, "GR/IR clearing",
        ))
    for (account_id, cost_center_id), amount in expense_by_key.items():
        if amount:
            journal_lines.append((expense_objs[account_id], 0, amount, cost_center_id, "Purchase credited"))
    ppv = None
    for cost_center_id, amount in ppv_by_cost_center.items():
        if not amount:
            continue
        if ppv is None:
            ppv = resolve_account(
                note.entity, PURCHASE_PRICE_VARIANCE_CODE, label="purchase price variance",
            )
        journal_lines.append((
            ppv, -amount if amount < 0 else 0, amount if amount > 0 else 0,
            cost_center_id, "Purchase price variance",
        ))
    for account_id, amount in tax_by_account.items():
        journal_lines.append((tax_objs[account_id], 0, amount, None, "Input tax reversed"))
    for number, (account, debit, credit, cost_center_id, text) in enumerate(journal_lines, start=1):
        JournalLine.objects.create(
            entry=entry, account=account, debit=debit, credit=credit,
            cost_center_id=cost_center_id, description=text, line_no=number,
        )
    post_journal(entry, actor_user=actor_user)

    if applied:
        VendorCreditNoteAllocation.objects.create(
            note=note, vendor_invoice=invoice, amount=applied, effective_date=note.note_date,
        )
        invoice.amount_credited += applied
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])

    for line in lines:
        po_line_id = line.invoice_line.po_line_id
        if not (po_line_id and line.quantity):
            continue
        if Decimal(locked_po_lines[po_line_id].invoiced_qty) < Decimal(line.quantity):
            raise VendorCreditNoteError(
                f"The purchase order shows less billed on {_line_name(line.invoice_line)} "
                f"than this note credits.",
            )
        PurchaseOrderLine.objects.filter(pk=po_line_id).update(
            invoiced_qty=F("invoiced_qty") - line.quantity,
        )

    note.journal = entry
    note.status = DocumentStatus.POSTED
    note.allocated_amount = applied
    note.save(update_fields=["journal", "status", "allocated_amount", "updated_at"])
    record(
        entity=note.entity, action=FinanceAuditAction.VENDOR_CREDIT_NOTE_POSTED,
        actor_user=actor_user, target=note,
        message=(
            f"Posted credit note from {vendor.code} against bill {_label(invoice)} "
            f"({format_naira(note.total)}; {format_naira(applied)} settled the bill, "
            f"{format_naira(advance)} held as vendor credit)."
        ),
        journal_id=entry.pk, total=note.total, tax=note.tax_total,
        vendor_invoice_id=invoice.pk, applied=applied, vendor_credit=advance,
    )
    rematch_open_bills(po_line_ids)
    return note


# --------------------------------------------------------------------------- #
# Vendor credit note: applying the credit to a later bill                     #
# --------------------------------------------------------------------------- #

def _require_note_branch_bills(note, bills) -> None:
    """Refuse a bill named for ``note``'s credit unless it is of the note's branch.

    The reclassification that settles it is booked to the note's branch, so Ikeja's
    credit applied to a Lekki bill would clear Lekki's liability out of Ikeja's books.
    Compared by :func:`vs_rbac.scoping.same_transaction_branch`, as a vendor
    payment's advance is (:func:`vs_procurement.payables._require_own_branch_bills`):
    at a school with one branch a bill not yet given a branch is that branch's, and
    at a school with several an unbranched note or bill matches only another
    unbranched one, never a branch's.
    """
    from vs_rbac.scoping import same_transaction_branch

    for bill in bills:
        if same_transaction_branch(note.entity.tenant_id, note.branch_id, bill.branch_id):
            continue
        number = bill.document_number or "the selected bill"
        held = (f"belongs to {bill.branch.name}" if bill.branch_id
                else "has not been given a branch")
        if note.branch_id is None:
            raise SettlementBranchError(
                f"This vendor credit has not been given a branch and bill {number} "
                f"{held}, so it cannot settle it.",
            )
        name = note.branch.name
        article = "an" if name[:1].upper() in "AEIOU" else "a"
        raise SettlementBranchError(
            f"This vendor credit belongs to {name} and bill {number} {held}. "
            f"Apply it to {article} {name} bill.",
        )


@transaction.atomic
def allocate_vendor_credit_note(note, *, allocations=None, actor_user=None, bill_scope=None):
    """Apply what a posted note left as vendor credit to later bills of its branch.

    The credit sits in the vendor-advance asset (1240). Applying it reclassifies it
    into AP (``Dr AP, Cr vendor advances``) and settles the bill with no cash moving,
    exactly as a vendor payment's advance is applied. ``allocations`` is an optional
    explicit ``[(bill, amount)]`` plan; without it the vendor's open bills of the
    note's branch are settled oldest-due first, within ``bill_scope`` when a caller
    passes one. The journal is dated at the later of the note and the newest bill it
    settles, so AP is never debited before the liability exists.

    Returns the allocation rows written.
    """
    from vs_finance.chronology import effective_allocation_date
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.receivables import lock_settlement_targets

    from .models import (
        VendorCreditAllocationJournal, VendorCreditNote, VendorCreditNoteAllocation,
        VendorInvoice,
    )

    note = VendorCreditNote.objects.select_for_update().get(pk=note.pk)
    if note.status != DocumentStatus.POSTED:
        raise VendorCreditNoteError("Only a posted vendor credit note can be applied.")
    vendor = note.vendor
    if vendor.payable_account_id is None:
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")
    remaining = note.advance_remaining
    if remaining <= 0:
        return []

    strict = allocations is not None
    if strict:
        _require_note_branch_bills(note, [bill for bill, _ in allocations])
        plan = [(bill, int(amount)) for bill, amount in allocations]
    else:
        from vs_rbac.scoping import transaction_branch_match_q

        candidates = VendorInvoice.objects.filter(
            transaction_branch_match_q(note.entity.tenant_id, note.branch_id),
            entity_id=note.entity_id, vendor_id=note.vendor_id,
            status=DocumentStatus.POSTED,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
        if bill_scope is not None:
            candidates = candidates.filter(bill_scope)
        plan = [
            (bill, bill.balance_due)
            for bill in candidates.order_by("due_date", "invoice_date", "id")
        ]
    plan = lock_settlement_targets(plan)

    settlements = []
    seen = set()
    for bill, requested in plan:
        if bill.pk in seen:
            raise VendorCreditNoteError("Name each bill once when applying a vendor credit.")
        seen.add(bill.pk)
        if bill.entity_id != note.entity_id or bill.vendor_id != note.vendor_id:
            raise VendorCreditNoteError("A vendor credit can only settle that vendor's bills.")
        if bill.status != DocumentStatus.POSTED:
            raise VendorCreditNoteError(f"Bill {_label(bill)} is not posted.")
        if strict:
            if requested <= 0:
                raise VendorCreditNoteError("Amounts applied must be greater than zero.")
            if requested > bill.balance_due:
                raise VendorCreditNoteError(
                    f"Bill {_label(bill)} has only {format_naira(bill.balance_due)} left to settle.",
                )
        amount = min(int(requested), bill.balance_due, remaining)
        if strict and amount < requested:
            raise VendorCreditNoteError(
                f"This vendor credit has only {format_naira(note.advance_remaining)} left to apply.",
            )
        if amount <= 0:
            continue
        settlements.append((bill, amount))
        remaining -= amount
        if remaining <= 0 and not strict:
            break
    applied = sum(amount for _bill, amount in settlements)
    if applied <= 0:
        return []

    effective = effective_allocation_date(
        note.note_date, [bill.invoice_date for bill, _amount in settlements],
    )
    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch, date=effective,
        period=resolve_period(note.entity, effective),
        source=JournalSource.PURCHASE, currency=note.currency,
        narration=f"Apply vendor credit: {vendor.code}",
        reference=note.vendor_reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=vendor.payable_account, debit=applied, credit=0,
        description=f"AP: {vendor.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry,
        account=resolve_account(note.entity, VENDOR_ADVANCE_CODE, label="vendor advances"),
        debit=0, credit=applied, description=f"Vendor credit applied: {vendor.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)
    VendorCreditAllocationJournal.objects.create(note=note, journal=entry, amount=applied)

    rows = []
    for bill, amount in settlements:
        rows.append(VendorCreditNoteAllocation.objects.create(
            note=note, vendor_invoice=bill, amount=amount, effective_date=effective,
        ))
        bill.amount_credited += amount
        bill.refresh_payment_status(save=False)
        bill.save(update_fields=["amount_credited", "payment_status", "updated_at"])

    note.allocated_amount += applied
    note.save(update_fields=["allocated_amount", "updated_at"])
    record(
        entity=note.entity, action=FinanceAuditAction.VENDOR_CREDIT_NOTE_ALLOCATED,
        actor_user=actor_user, target=note,
        message=f"Applied {format_naira(applied)} of vendor credit across {len(rows)} bill(s).",
        journal_id=entry.pk, allocated=note.allocated_amount,
        unallocated=note.advance_remaining, effective_date=str(effective),
    )
    return rows


# --------------------------------------------------------------------------- #
# Vendor credit note: void                                                    #
# --------------------------------------------------------------------------- #

def void_vendor_credit_note(note, *, actor_user=None, date=None):
    """Void a posted credit note, putting every bill it settled back as it was."""
    return _run_with_rejection(
        note, FinanceAuditAction.VENDOR_CREDIT_NOTE_VOIDED,
        _void_vendor_credit_note_atomic, actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_vendor_credit_note_atomic(note, *, actor_user=None, date=None):
    """Reverse the note's journals newest first and restore bills and PO quantities.

    The note owns its own journal and one reclassification per later application of
    its credit. All of them are reversed, as a vendor payment's reversal does, and
    every bill it settled has the credit taken back off. Refused when the units it
    credited have since been returned to the vendor: giving the bill back those units
    would bill goods the school no longer holds.
    """
    from .models import PurchaseOrderLine, VendorCreditNote, VendorCreditNoteAllocation, VendorInvoice

    note = VendorCreditNote.objects.select_for_update().get(pk=note.pk)
    if note.status != DocumentStatus.POSTED or note.journal_id is None:
        raise VendorCreditNoteError(
            f"Only a posted vendor credit note can be voided; {_label(note)} is {state_word(note)}.",
        )
    allocations = list(
        VendorCreditNoteAllocation.objects.select_for_update().filter(note=note)
        .order_by("vendor_invoice_id", "pk")
    )
    bills = {
        bill.pk: bill for bill in VendorInvoice.objects.select_for_update()
        .filter(pk__in=sorted({row.vendor_invoice_id for row in allocations})).order_by("pk")
    }
    if sum(int(row.amount) for row in allocations) != int(note.allocated_amount):
        raise VendorCreditNoteError(
            f"Vendor credit note {_label(note)} has applications that do not add up to "
            f"what it records as applied, so voiding it would leave AP out of step.",
        )
    quantity_lines = [
        line for line in note.lines.select_related("invoice_line").order_by("pk")
        if line.quantity and line.invoice_line.po_line_id
    ]
    po_lines = {
        row.pk: row for row in PurchaseOrderLine.objects.select_for_update()
        .filter(pk__in=sorted({line.invoice_line.po_line_id for line in quantity_lines}))
        .order_by("pk")
    }
    restored: dict[int, Decimal] = defaultdict(Decimal)
    for line in quantity_lines:
        restored[line.invoice_line.po_line_id] += Decimal(line.quantity)
    factor = _quantity_factor(note.entity)
    for po_line_id, quantity in restored.items():
        po_line = po_lines[po_line_id]
        if Decimal(po_line.invoiced_qty) + quantity > Decimal(po_line.received_qty) * factor:
            raise VendorCreditNoteError(
                f"Units this note credited on '{po_line.description}' have since been "
                f"returned to the vendor, so the bill cannot have them back. Raise a new "
                f"bill for anything still owed instead.",
            )

    links = list(
        note.allocation_journals.select_for_update(of=("self",)).select_related("journal")
        .order_by("journal__date", "journal_id")
    )
    for link in reversed(links):
        reverse_journal(link.journal, actor_user=actor_user, date=date, document_owner=note)
    reversal = reverse_journal(note.journal, actor_user=actor_user, date=date, document_owner=note)

    for row in allocations:
        bill = bills[row.vendor_invoice_id]
        bill.amount_credited = max(0, bill.amount_credited - int(row.amount))
        bill.refresh_payment_status(save=False)
        bill.save(update_fields=["amount_credited", "payment_status", "updated_at"])
    for po_line_id, quantity in restored.items():
        PurchaseOrderLine.objects.filter(pk=po_line_id).update(
            invoiced_qty=F("invoiced_qty") + quantity,
        )

    note.allocated_amount = 0
    note.status = DocumentStatus.REVERSED
    note.save(update_fields=["allocated_amount", "status", "updated_at"])
    record(
        entity=note.entity, action=FinanceAuditAction.VENDOR_CREDIT_NOTE_VOIDED,
        actor_user=actor_user, target=note,
        message=f"Voided vendor credit note {_label(note)}.",
        journal_id=note.journal_id, reversal_id=reversal.pk,
        allocation_reversals=[link.journal_id for link in links],
    )
    rematch_open_bills(set(restored))
    return note


# --------------------------------------------------------------------------- #
# Bill void                                                                   #
# --------------------------------------------------------------------------- #

def void_vendor_invoice(invoice, *, actor_user=None, date=None):
    """Void a posted bill nothing stands on, reversing its journal."""
    return _run_with_rejection(
        invoice, FinanceAuditAction.VENDOR_INVOICE_VOIDED,
        _void_vendor_invoice_atomic, actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_vendor_invoice_atomic(invoice, *, actor_user=None, date=None):
    """Reverse the bill's journal and give the PO back its invoiced quantities.

    Only while nothing has been paid against the bill and no credit note has been
    raised on it. Either of those is a document that settled part of the bill, and
    voiding underneath it would leave that document pointing at a bill that no longer
    exists; a bill with money paid against it is corrected with a credit note. The
    bill reads REVERSED afterwards, the ledger's word for void, and its match outcome
    is cleared so it stops appearing among the bills in dispute.
    """
    from .models import PurchaseOrderLine, VendorInvoice

    invoice = VendorInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != DocumentStatus.POSTED or invoice.journal_id is None:
        raise VendorInvoiceVoidError(
            f"Only a posted vendor invoice can be voided; {_label(invoice)} is "
            f"{state_word(invoice)}.",
        )
    if invoice.amount_paid > 0:
        raise VendorInvoiceVoidError(
            f"{format_naira(invoice.amount_paid)} has been paid against bill "
            f"{_label(invoice)}, so it cannot be voided. A bill with money paid "
            f"against it is corrected with a vendor credit note.",
        )
    if invoice.amount_credited > 0 or invoice.credit_notes.filter(
            status=DocumentStatus.POSTED).exists():
        raise VendorInvoiceVoidError(
            f"A posted credit note stands against bill {_label(invoice)}, so it cannot "
            f"be voided. Void the credit note first, or credit the rest of the bill.",
        )

    bill_lines = list(invoice.lines.exclude(po_line_id=None).order_by("pk"))
    locked = {
        row.pk: row for row in PurchaseOrderLine.objects.select_for_update()
        .filter(pk__in=sorted({line.po_line_id for line in bill_lines})).order_by("pk")
    }
    billed: dict[int, Decimal] = defaultdict(Decimal)
    for line in bill_lines:
        billed[line.po_line_id] += Decimal(line.quantity)

    for po_line_id, quantity in billed.items():
        if Decimal(locked[po_line_id].invoiced_qty) < quantity:
            raise VendorInvoiceVoidError(
                f"The purchase order shows less billed on '{locked[po_line_id].description}' "
                f"than bill {_label(invoice)} carries, so its quantities cannot be given back.",
            )

    reversal = reverse_journal(
        invoice.journal, actor_user=actor_user, date=date, document_owner=invoice,
    )
    for po_line_id, quantity in billed.items():
        PurchaseOrderLine.objects.filter(pk=po_line_id).update(
            invoiced_qty=F("invoiced_qty") - quantity,
        )

    invoice.status = DocumentStatus.REVERSED
    invoice.match_status = MatchStatus.NOT_MATCHED
    invoice.save(update_fields=["status", "match_status", "updated_at"])
    record(
        entity=invoice.entity, action=FinanceAuditAction.VENDOR_INVOICE_VOIDED,
        actor_user=actor_user, target=invoice,
        message=f"Voided bill {_label(invoice)} from {invoice.vendor.code} ({format_naira(invoice.total)}).",
        journal_id=invoice.journal_id, reversal_id=reversal.pk,
        before={"status": DocumentStatus.POSTED}, after={"status": DocumentStatus.REVERSED},
    )
    rematch_open_bills(set(billed), exclude_invoice_id=invoice.pk)
    return invoice


# --------------------------------------------------------------------------- #
# Goods return (reversing a posted receipt)                                   #
# --------------------------------------------------------------------------- #

def return_goods(grn, *, return_date, reason, lines=None, actor_user=None):
    """Send goods on a posted receipt back to the vendor, and book it.

    ``lines`` is ``[(grn_line_id, quantity)]``; without it, everything on the receipt
    that can still go back goes, which is what reversing a receipt entered in error
    means. Records a durable rejection audit row on any refusal, then re-raises.
    Returns the posted :class:`~vs_procurement.models.GoodsReturn`.
    """
    return _run_with_rejection(
        grn, FinanceAuditAction.GOODS_RETURNED, _return_goods_atomic,
        return_date=return_date, reason=reason, lines=lines, actor_user=actor_user,
    )


def _billed_without_po(grn_line_ids) -> dict:
    """``{grn line id: quantity}`` billed on posted non-PO bills, net of credits."""
    from .models import VendorCreditNoteLine, VendorInvoiceLine

    billed = defaultdict(Decimal)
    for row in VendorInvoiceLine.objects.filter(
        grn_line_id__in=grn_line_ids, po_line__isnull=True,
        vendor_invoice__status=DocumentStatus.POSTED,
    ).values("grn_line_id").annotate(q=Sum("quantity")):
        billed[row["grn_line_id"]] += Decimal(row["q"] or 0)
    for row in VendorCreditNoteLine.objects.filter(
        invoice_line__grn_line_id__in=grn_line_ids, invoice_line__po_line__isnull=True,
        invoice_line__vendor_invoice__status=DocumentStatus.POSTED,
        credit_note__status=DocumentStatus.POSTED,
    ).values("invoice_line__grn_line_id").annotate(q=Sum("quantity")):
        billed[row["invoice_line__grn_line_id"]] -= Decimal(row["q"] or 0)
    return billed


@transaction.atomic
def _return_goods_atomic(grn, *, return_date, reason, lines=None, actor_user=None):
    """Book one return: journal, stock, PO and receipt counters, in one transaction.

    Lock order is receipt, its lines, stock items, then PO lines, which is the order
    receipt posting takes them in.

    What may go back from a line is what was accepted, less what went back already,
    less what has been billed and not credited. For a PO-backed line billing is
    counted on the PO line (received less invoiced), because a bill names the PO
    line and not the receipt; for a line with no PO it is counted from the bills
    that name the receipt line.

    Stock leaves the store it arrived in, at the receipt's own price, and is refused
    when that store no longer holds the units or their value (they were issued).
    """
    from vs_finance.chronology import ensure_on_or_after
    from vs_finance.models import JournalEntry, JournalLine

    from .models import (
        GoodsReceivedNote, GoodsReceivedNoteLine, GoodsReturn, GoodsReturnLine,
        PurchaseOrderLine, StockMovement,
    )
    from .stock import _record_movement, location_for_branch, lock_balance, lock_stock_items

    grn = GoodsReceivedNote.objects.select_for_update().get(pk=grn.pk)
    if grn.status != DocumentStatus.POSTED or grn.journal_id is None:
        raise GoodsReturnError(
            f"Only a posted goods receipt can be reversed; {_label(grn)} is {state_word(grn)}.",
        )
    reason = str(reason or "").strip()
    if not reason:
        raise GoodsReturnError("Say why the goods are going back to the vendor.")
    ensure_on_or_after(
        subject=f"Return of goods on {_label(grn)}", subject_date=return_date,
        source=f"goods receipt {_label(grn)}", source_date=grn.received_date,
        remedy=f"Date the return {grn.received_date} or later.",
    )

    grn_lines = {
        line.pk: line for line in GoodsReceivedNoteLine.objects.select_for_update(of=("self",))
        .filter(grn=grn).select_related("expense_account", "stock_item").order_by("pk")
    }
    stock_ids = sorted({line.stock_item_id for line in grn_lines.values() if line.stock_item_id})
    stock_items = lock_stock_items(stock_ids)
    po_lines = {
        row.pk: row for row in PurchaseOrderLine.objects.select_for_update()
        .filter(pk__in=sorted({line.po_line_id for line in grn_lines.values() if line.po_line_id}))
        .order_by("pk")
    }
    billed_without_po = _billed_without_po(
        [pk for pk, line in grn_lines.items() if not line.po_line_id],
    )
    po_room = {
        pk: Decimal(row.received_qty) - Decimal(row.invoiced_qty) for pk, row in po_lines.items()
    }

    def returnable(line) -> Decimal:
        left = Decimal(line.accepted_qty) - Decimal(line.returned_qty)
        if line.po_line_id:
            return max(Decimal(0), min(left, po_room[line.po_line_id]))
        return max(Decimal(0), left - billed_without_po.get(line.pk, Decimal(0)))

    plan = []
    if lines is None:
        for line in grn_lines.values():
            quantity = returnable(line)
            if quantity > 0:
                plan.append((line, quantity))
                if line.po_line_id:
                    po_room[line.po_line_id] -= quantity
        if not plan:
            raise GoodsReturnError(
                f"Nothing on goods receipt {_label(grn)} can go back: it has all been "
                f"returned or billed. Credit the bill for billed goods first, then return them.",
            )
    else:
        seen = set()
        for line_id, quantity in lines:
            line = grn_lines.get(line_id)
            if line is None:
                raise GoodsReturnError("Every returned line must be a line of this goods receipt.")
            if line_id in seen:
                raise GoodsReturnError("Name each receipt line once in a return.")
            seen.add(line_id)
            quantity = Decimal(quantity)
            if quantity <= 0:
                raise GoodsReturnError("A returned quantity must be greater than zero.")
            room = returnable(line)
            if quantity > room:
                name = line.description or f"line {line.line_no or line.pk}"
                raise GoodsReturnError(
                    f"Only {room.normalize()} of '{name}' can go back: the rest has "
                    f"already been returned or billed. Credit the bill for billed goods "
                    f"first, then return them.",
                )
            plan.append((line, quantity))
            if line.po_line_id:
                po_room[line.po_line_id] -= quantity

    receipt_location = {
        movement.stock_item_id: movement.location
        for movement in StockMovement.objects.filter(
            grn=grn, movement_type=StockMovementType.RECEIPT, location__isnull=False,
        ).select_related("location").order_by("pk")
    }
    credit_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    credit_objs: dict[int, object] = {}
    balances: dict[tuple[int, int], object] = {}
    leaving: dict[tuple[int, int], list] = defaultdict(lambda: [Decimal(0), 0])
    stock_moves = []
    total_value = 0
    valued = []
    for line, quantity in plan:
        value = compute_line_net(quantity, line.unit_price)
        valued.append((line, quantity, value))
        total_value += value
        if line.stock_item_id:
            item = stock_items[line.stock_item_id]
            location = (
                receipt_location.get(item.pk) or location_for_branch(grn.entity, grn.branch)
            )
            slot = (item.pk, location.pk)
            if slot not in balances:
                balances[slot] = lock_balance(item, location)
            leaving[slot][0] += quantity
            leaving[slot][1] += value
            stock_moves.append((item, balances[slot], quantity, value))
            account = item.inventory_account
            key = (account.id, None)
        else:
            account = line.expense_account
            key = (account.id, line.cost_center_id)
        credit_by_key[key] += value
        credit_objs[account.id] = account
    if total_value <= 0:
        raise GoodsReturnError("A goods return must take back a positive value.")
    for slot, (quantity, value) in leaving.items():
        balance = balances[slot]
        if Decimal(balance.on_hand_qty) < quantity or int(balance.stock_value) < value:
            item = stock_items[slot[0]]
            raise GoodsReturnError(
                f"{balance.location.code} holds {Decimal(balance.on_hand_qty).normalize()} "
                f"of '{item.code}' worth {format_naira(int(balance.stock_value))}, so "
                f"{quantity.normalize()} at {format_naira(value)} cannot go back from it: "
                f"the rest has been issued.",
            )

    entry = JournalEntry.objects.create(
        entity=grn.entity, branch=grn.branch, date=return_date,
        period=resolve_period(grn.entity, return_date), source=JournalSource.PURCHASE,
        narration=f"Return to vendor: {_label(grn)}", reference=grn.reference,
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_account(grn.entity, GRIR_CLEARING_CODE, label="GR/IR clearing"),
        debit=total_value, credit=0, description=f"GR/IR: {grn.vendor.code}", line_no=1,
    )
    for number, ((account_id, cost_center_id), amount) in enumerate(credit_by_key.items(), start=2):
        JournalLine.objects.create(
            entry=entry, account=credit_objs[account_id], debit=0, credit=amount,
            cost_center_id=cost_center_id, description="Goods returned", line_no=number,
        )
    post_journal(entry, actor_user=actor_user)

    goods_return = GoodsReturn.objects.create(
        entity=grn.entity, branch=grn.branch, vendor=grn.vendor, grn=grn,
        return_date=return_date, reason=reason, total_value=total_value,
        journal=entry, status=DocumentStatus.POSTED, created_by=actor_user,
    )
    returned_by_po_line: dict[int, Decimal] = defaultdict(Decimal)
    for number, (line, quantity, value) in enumerate(valued, start=1):
        GoodsReturnLine.objects.create(
            goods_return=goods_return, grn_line=line, quantity=quantity,
            value_amount=value, line_no=number,
        )
        GoodsReceivedNoteLine.objects.filter(pk=line.pk).update(
            returned_qty=F("returned_qty") + quantity,
        )
        if line.po_line_id:
            returned_by_po_line[line.po_line_id] += quantity
    for po_line_id, quantity in returned_by_po_line.items():
        PurchaseOrderLine.objects.filter(pk=po_line_id).update(
            received_qty=F("received_qty") - quantity,
        )
    for item, balance, quantity, value in stock_moves:
        _record_movement(
            item, balance, movement_type=StockMovementType.RETURN,
            quantity=-quantity, value_amount=-value, movement_date=return_date,
            grn=grn, journal=entry, actor_user=actor_user,
            reference=goods_return.document_number,
            narration=f"Returned to {grn.vendor.code}: {_label(grn)}",
        )

    record(
        entity=grn.entity, action=FinanceAuditAction.GOODS_RETURNED,
        actor_user=actor_user, target=goods_return,
        message=(
            f"Returned goods on {_label(grn)} to {grn.vendor.code} "
            f"({format_naira(total_value)} out of GR/IR): {reason}"
        ),
        journal_id=entry.pk, value=total_value, grn_id=grn.pk, reason=reason,
    )
    rematch_open_bills(set(returned_by_po_line))
    return goods_return
