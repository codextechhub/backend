"""Installment plans and concessions — the "how they pay" / "give-back" of AR.

Two unrelated-but-adjacent receivable services live here:

* **Payment plans** — split a receivable into dated installments. A *scheduling overlay
  only*: nothing here touches the General Ledger. The invoice already sits in AR; the
  plan records the expected dates/amounts so progress, reminders and dunning have
  something to measure against. Settlement is reflected by distributing the linked
  invoice's settled amount across installments oldest-first.

* **Concessions** — discounts, waivers and scholarships. These *do* post: ``Dr discounts
  & allowances (contra-revenue), Cr AR control``, clearing that much of the invoice via
  :attr:`Invoice.amount_credited` — a targeted, single-line credit on policy grounds.

Domain-neutral throughout: only generic customers, invoices and accounts. A school
tenant's *scholarship/bursary* is simply a concession with ``kind=SCHOLARSHIP``. All
amounts are integer kobo.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    ConcessionKind,
    DISCOUNTS_ALLOWED_CODE,
    DocumentStatus,
    FinanceAuditAction,
    InstallmentStatus,
    JournalSource,
    PaymentPlanFrequency,
    PaymentPlanStatus,
)
from .exceptions import FinanceError, PostingError
from .posting import post_journal, resolve_period


# --------------------------------------------------------------------------- #
# Installment scheduling (no GL effect)                                        #
# --------------------------------------------------------------------------- #

#: Day deltas for the fixed-day frequencies; month-based ones are handled separately.
_DAY_DELTAS = {
    PaymentPlanFrequency.WEEKLY: 7,
    PaymentPlanFrequency.FORTNIGHTLY: 14,
}
_MONTH_DELTAS = {
    PaymentPlanFrequency.MONTHLY: 1,
    PaymentPlanFrequency.QUARTERLY: 3,
}


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """Add ``months`` to ``d``, clamping the day to the target month's last day."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Last day of the target month (handles 31→30/28 etc.).
    if month == 12:
        last_day = 31
    else:
        last_day = (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).day
    return datetime.date(year, month, min(d.day, last_day))


def _due_date(start: datetime.date, index: int, frequency: str) -> datetime.date:
    """Due date of the ``index``-th installment (0-based) for ``frequency`` from ``start``."""
    if index == 0:
        return start
    if frequency in _DAY_DELTAS:
        return start + datetime.timedelta(days=_DAY_DELTAS[frequency] * index)
    if frequency in _MONTH_DELTAS:
        return _add_months(start, _MONTH_DELTAS[frequency] * index)
    raise PostingError(f"Unsupported payment-plan frequency '{frequency}'.")


def split_amount(total_kobo: int, count: int) -> list[int]:
    """Split ``total_kobo`` into ``count`` installments, remainder on the last one.

    Integer-exact (kobo never fractionalised): each of the first ``count-1`` installments
    gets ``floor(total/count)``; the final one absorbs the rounding remainder so the
    parts always sum back to ``total_kobo``.
    """
    if count <= 0:
        raise PostingError("A payment plan needs at least one installment.")
    base = int((Decimal(int(total_kobo)) / Decimal(count)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    ))
    parts = [base] * (count - 1)
    parts.append(int(total_kobo) - base * (count - 1))
    return parts


def build_installments(plan, *, amounts=None):
    """(Re)generate the installment rows for a DRAFT ``plan`` from its schedule fields.

    Replaces any existing installments. ``amounts`` may pass an explicit list of kobo
    amounts (must sum to ``plan.total_amount`` and match ``installment_count``);
    otherwise the total is split evenly with the remainder on the final installment.
    Returns the created installment rows.
    """
    from .models import PaymentPlanInstallment

    if plan.plan_status != PaymentPlanStatus.DRAFT:
        raise PostingError("Only a draft payment plan's schedule can be (re)built.")
    if plan.total_amount <= 0:
        raise PostingError("A payment plan must spread a positive total.")

    count = int(plan.installment_count)
    if amounts is None:
        amounts = split_amount(plan.total_amount, count)
    else:
        amounts = [int(a) for a in amounts]
        if len(amounts) != count:
            raise PostingError(
                f"Expected {count} installment amounts, got {len(amounts)}.",
            )
        if sum(amounts) != plan.total_amount:
            raise PostingError(
                f"Installment amounts sum to {sum(amounts)} kobo, "
                f"but the plan total is {plan.total_amount} kobo.",
            )
        if any(a <= 0 for a in amounts):
            raise PostingError("Every installment amount must be positive.")

    plan.installments.all().delete()
    rows = [
        PaymentPlanInstallment(
            plan=plan, seq_no=i + 1,
            due_date=_due_date(plan.start_date, i, plan.frequency),
            amount=amounts[i],
        )
        for i in range(count)
    ]
    PaymentPlanInstallment.objects.bulk_create(rows)
    return list(plan.installments.all())


def activate_payment_plan(plan, *, actor_user=None):
    """Commit a DRAFT plan: validate the schedule, mark ACTIVE, sync any settlement."""
    try:
        return _activate_payment_plan_atomic(plan, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_ACTIVATED,
            exc=exc, actor_user=actor_user, target=plan,
        )
        raise


@transaction.atomic
def _activate_payment_plan_atomic(plan, *, actor_user=None):
    if plan.plan_status != PaymentPlanStatus.DRAFT:
        raise PostingError(
            f"Payment plan {plan.document_number or plan.pk} is "
            f"'{plan.plan_status}'; only a draft plan can be activated.",
        )
    if not plan.installments.exists():
        raise PostingError("Build the installment schedule before activating the plan.")
    if plan.scheduled_total != plan.total_amount:
        raise PostingError(
            f"Installments sum to {plan.scheduled_total} kobo but the plan total is "
            f"{plan.total_amount} kobo; rebuild the schedule.",
        )

    plan.plan_status = PaymentPlanStatus.ACTIVE
    plan.save(update_fields=["plan_status", "updated_at"])
    record(
        entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_ACTIVATED,
        actor_user=actor_user, target=plan,
        message=f"Activated {plan.installment_count}-installment plan "
                f"for {plan.customer.code} ({plan.total_amount} kobo).",
        total=plan.total_amount, installments=plan.installment_count,
    )
    # Reflect any settlement already on the linked invoice.
    refresh_plan_progress(plan, actor_user=actor_user)
    return plan


def cancel_payment_plan(plan, *, actor_user=None):
    """Cancel a plan that is no longer being followed. Idempotent on terminal states."""
    if plan.plan_status in (PaymentPlanStatus.COMPLETED, PaymentPlanStatus.CANCELLED):
        return plan
    plan.plan_status = PaymentPlanStatus.CANCELLED
    plan.save(update_fields=["plan_status", "updated_at"])
    record(
        entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_CANCELLED,
        actor_user=actor_user, target=plan,
        message=f"Cancelled payment plan {plan.document_number} for {plan.customer.code}.",
    )
    return plan


@transaction.atomic
def refresh_plan_progress(plan, *, settled_amount=None, actor_user=None):
    """Distribute settlement across the plan's installments oldest-first.

    ``settled_amount`` (kobo) overrides the source figure; otherwise, for a plan linked
    to an invoice, the invoice's :attr:`Invoice.settled_amount` (cash + non-cash
    credits) is used. Each installment is filled in sequence, its ``amount_settled`` and
    derived status updated. When the whole plan is settled it flips to COMPLETED.
    Returns the plan. No GL effect — this only mirrors money already posted elsewhere.
    """
    if plan.plan_status not in (PaymentPlanStatus.ACTIVE, PaymentPlanStatus.COMPLETED):
        return plan

    if settled_amount is None:
        if plan.invoice_id is not None:
            plan.invoice.refresh_from_db()
            settled_amount = plan.invoice.settled_amount
        else:
            settled_amount = plan.settled_total  # standalone plan: leave as-is
    remaining = max(int(settled_amount), 0)

    for inst in plan.installments.order_by("seq_no", "id"):
        applied = min(inst.amount, remaining)
        if applied >= inst.amount:
            status = InstallmentStatus.PAID
        elif applied > 0:
            status = InstallmentStatus.PARTIAL
        else:
            status = InstallmentStatus.PENDING
        if inst.amount_settled != applied or inst.status != status:
            inst.amount_settled = applied
            inst.status = status
            inst.save(update_fields=["amount_settled", "status", "updated_at"])
        remaining -= applied

    if plan.settled_total >= plan.total_amount and plan.plan_status != PaymentPlanStatus.COMPLETED:
        plan.plan_status = PaymentPlanStatus.COMPLETED
        plan.save(update_fields=["plan_status", "updated_at"])
        record(
            entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_COMPLETED,
            actor_user=actor_user, target=plan,
            message=f"Payment plan {plan.document_number} fully settled.",
        )
    return plan


def refresh_plans_for_invoice(invoice, *, actor_user=None):
    """Re-sync every live payment plan attached to ``invoice`` after its settled amount
    moved (a receipt, credit-note allocation or write-off).

    A plan tracks the invoice's ``settled_amount`` but does not observe it, so this is
    the push that keeps an ACTIVE/COMPLETED plan in step when cash (or non-cash credit)
    lands on the invoice. No GL effect. Safe to call when the invoice has no plan.
    """
    if invoice is None or invoice.pk is None:
        return
    from .constants import PaymentPlanStatus
    from .models import PaymentPlan

    plans = PaymentPlan.objects.filter(
        invoice=invoice,
        plan_status__in=(PaymentPlanStatus.ACTIVE, PaymentPlanStatus.COMPLETED),
    )
    for plan in plans:
        refresh_plan_progress(plan, actor_user=actor_user)


# --------------------------------------------------------------------------- #
# Concessions — discounts / waivers / scholarships (Dr allowance, Cr AR)        #
# --------------------------------------------------------------------------- #

def post_concession(concession, *, actor_user=None):
    """Post a :class:`Concession` (``Dr discounts & allowances, Cr AR control``).

    Clears ``concession.amount`` of the linked invoice's balance via
    ``amount_credited`` and refreshes any active payment plan on that invoice. Records
    a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:
        return _post_concession_atomic(concession, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=concession.entity, action=FinanceAuditAction.CONCESSION_POSTED,
            exc=exc, actor_user=actor_user, target=concession,
        )
        raise


@transaction.atomic
def _post_concession_atomic(concession, *, actor_user=None):
    """Post a draft concession: raise its journal and mark it POSTED.
    
    Steps:
      1. **Guard.** Only a DRAFT concession posts, and the amount must be positive and 
      not exceed the invoice's outstanding balance.
      2. **Post the journal** (``Dr discounts & allowances, Cr AR control``) to clear 
      that much of the invoice.
      3. **Finalise.** Link the journal, flip status to POSTED, update the invoice's 
      ``amount_credited`` and refresh any active payment plan. 
      4. Write a CONCESSION_POSTED audit record. Returns the updated ``concession``. 
      Raises ``PostingError`` on any guard failure; ``post_concession`` wraps this 
      to record a rejection on ``FinanceError``.
    """
    from .models import JournalEntry, JournalLine, PaymentPlan

    if concession.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Concession {concession.document_number or concession.pk} is "
            f"'{concession.status}'; only a draft concession can be posted.",
        )

    invoice = concession.invoice
    if invoice.status != DocumentStatus.POSTED:
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
            f"a concession can only reduce a posted invoice.",
        )

    balance = invoice.balance_due
    if balance <= 0:
        raise PostingError("Invoice has no outstanding balance to concede.")
    amount = int(concession.amount)
    if amount <= 0:
        raise PostingError("A concession must have a positive amount to post.")
    if amount > balance:
        raise PostingError(
            f"Concession amount ({amount} kobo) exceeds the outstanding balance "
            f"({balance} kobo).",
        )

    customer = concession.customer
    ar_account = customer.receivable_account
    if ar_account is None:
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")

    allowance = concession.allowance_account or resolve_account(
        concession.entity, DISCOUNTS_ALLOWED_CODE, label="discounts & allowances",
    )
    period = resolve_period(concession.entity, concession.concession_date)
    label = concession.get_kind_display()
    entry = JournalEntry.objects.create(
        entity=concession.entity, branch=concession.branch,
        date=concession.concession_date, period=period, source=JournalSource.SALES,
        narration=concession.reason or f"{label} {concession.document_number or ''}".strip(),
        reference=concession.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=allowance, debit=amount, credit=0,
        description=f"{label}: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=0, credit=amount,
        description=f"AR {label.lower()}: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    concession.allowance_account = allowance
    concession.journal = entry
    concession.status = DocumentStatus.POSTED
    concession.save(update_fields=["allowance_account", "journal", "status", "updated_at"])

    invoice.amount_credited += amount
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])

    record(
        entity=concession.entity, action=FinanceAuditAction.CONCESSION_POSTED,
        actor_user=actor_user, target=concession,
        message=f"Granted {label.lower()} of {amount} kobo on invoice "
                f"{invoice.document_number} for {customer.code}.",
        journal_id=entry.pk, amount=amount, kind=concession.kind,
        balance_after=invoice.balance_due,
    )

    # A concession settles part of the invoice — keep any active plan in step.
    for plan in PaymentPlan.objects.filter(
        invoice=invoice, plan_status=PaymentPlanStatus.ACTIVE,
    ):
        refresh_plan_progress(plan, actor_user=actor_user)
    return concession
