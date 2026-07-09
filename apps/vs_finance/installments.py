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
from __future__ import annotations  # Defer annotation evaluation during app import.

import datetime  # Date arithmetic for installment due dates.
from decimal import Decimal, ROUND_HALF_UP  # Exact installment splitting and rounding.

from django.db import transaction  # Keeps plan/concession mutations atomic.

from .accounts import resolve_account  # Resolves default concession allowance account.
from .audit import record, record_rejection  # Finance audit helpers.
from .constants import (
    ConcessionKind,  # Concession category enum.
    DISCOUNTS_ALLOWED_CODE,  # Default discounts/concessions contra-revenue account.
    DocumentStatus,  # Finance document lifecycle statuses.
    FinanceAuditAction,  # Audit action enum values.
    InstallmentStatus,  # Individual installment lifecycle statuses.
    JournalSource,  # Journal source enum values.
    PaymentPlanFrequency,  # Payment plan recurrence enum.
    PaymentPlanStatus,  # Payment plan lifecycle statuses.
)
from .exceptions import FinanceError, PostingError  # Base finance and posting errors.
from .posting import post_journal, resolve_period  # GL posting and period resolution helpers.


# --------------------------------------------------------------------------- #
# Installment scheduling (no GL effect)                                        #
# --------------------------------------------------------------------------- #

#: Day deltas for the fixed-day frequencies; month-based ones are handled separately.
_DAY_DELTAS = {  # Fixed-day frequency offsets.
    PaymentPlanFrequency.WEEKLY: 7,  # Weekly installments are 7 days apart.
    PaymentPlanFrequency.FORTNIGHTLY: 14,  # Fortnightly installments are 14 days apart.
}
_MONTH_DELTAS = {  # Calendar-month frequency offsets.
    PaymentPlanFrequency.MONTHLY: 1,  # Monthly installments advance one month.
    PaymentPlanFrequency.QUARTERLY: 3,  # Quarterly installments advance three months.
}


def _add_months(d: datetime.date, months: int) -> datetime.date:  # Add calendar months with day clamping.
    """Add ``months`` to ``d``, clamping the day to the target month's last day."""
    month_index = d.month - 1 + months  # Convert to zero-based month and add offset.
    year = d.year + month_index // 12  # Carry overflow months into year.
    month = month_index % 12 + 1  # Convert zero-based month back to 1-12.
    # Last day of the target month (handles 31→30/28 etc.).  # Prevent invalid due dates.
    if month == 12:  # December has a fixed last day.
        last_day = 31  # Last day of December.
    else:  # Other months use the day before next month.
        last_day = (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).day  # Target month length.
    return datetime.date(year, month, min(d.day, last_day))  # Preserve original day where possible.


def _due_date(start: datetime.date, index: int, frequency: str) -> datetime.date:  # Compute installment due date.
    """Due date of the ``index``-th installment (0-based) for ``frequency`` from ``start``."""
    if index == 0:  # First installment is due on the start date.
        return start
    if frequency in _DAY_DELTAS:  # Day-based frequencies use simple timedelta math.
        return start + datetime.timedelta(days=_DAY_DELTAS[frequency] * index)  # Offset by fixed days.
    if frequency in _MONTH_DELTAS:  # Month-based frequencies use calendar clamping.
        return _add_months(start, _MONTH_DELTAS[frequency] * index)  # Offset by calendar months.
    raise PostingError(f"Unsupported payment-plan frequency '{frequency}'.")


def split_amount(total_kobo: int, count: int) -> list[int]:  # Split integer-kobo total into installment amounts.
    """Split ``total_kobo`` into ``count`` installments, remainder on the last one.

    Integer-exact (kobo never fractionalised): each of the first ``count-1`` installments
    gets ``floor(total/count)``; the final one absorbs the rounding remainder so the
    parts always sum back to ``total_kobo``.
    """
    if count <= 0:  # A payment plan needs at least one part.
        raise PostingError("A payment plan needs at least one installment.")
    base = int((Decimal(int(total_kobo)) / Decimal(count)).quantize(  # Round the common installment amount.
        Decimal("1"), rounding=ROUND_HALF_UP,  # Round to whole kobo with half-up discipline.
    ))
    parts = [base] * (count - 1)  # First installments use the common rounded amount.
    parts.append(int(total_kobo) - base * (count - 1))  # Final installment absorbs any remainder.
    return parts  # Return exact-sum installment amounts.


def build_installments(plan, *, amounts=None):  # Build draft plan installment rows.
    """(Re)generate the installment rows for a DRAFT ``plan`` from its schedule fields.

    Replaces any existing installments. ``amounts`` may pass an explicit list of kobo
    amounts (must sum to ``plan.total_amount`` and match ``installment_count``);
    otherwise the total is split evenly with the remainder on the final installment.
    Returns the created installment rows.
    """
    from .models import PaymentPlanInstallment  # Local import avoids model import cycles.

    if plan.plan_status != PaymentPlanStatus.DRAFT:  # Only draft schedules can be changed.
        raise PostingError("Only a draft payment plan's schedule can be (re)built.")
    if plan.total_amount <= 0:  # Plan must spread a positive receivable amount.
        raise PostingError("A payment plan must spread a positive total.")

    count = int(plan.installment_count)  # Normalize requested installment count.
    if amounts is None:  # No explicit split means evenly divide the total.
        amounts = split_amount(plan.total_amount, count)  # Generate exact-sum amounts.
    else:  # Caller supplied explicit amounts.
        amounts = [int(a) for a in amounts]  # Normalize amounts to integer kobo.
        if len(amounts) != count:  # Explicit amount count must match schedule count.
            raise PostingError(
                f"Expected {count} installment amounts, got {len(amounts)}.",
            )
        if sum(amounts) != plan.total_amount:  # Explicit amounts must reconcile to plan total.
            raise PostingError(
                f"Installment amounts sum to {sum(amounts)} kobo, "
                f"but the plan total is {plan.total_amount} kobo.",
            )
        if any(a <= 0 for a in amounts):  # Each installment must be meaningful.
            raise PostingError("Every installment amount must be positive.")

    plan.installments.all().delete()  # clear any existing schedule on the draft plan  # Replace draft schedule wholesale.
    rows = [  # Prepare installment rows for bulk insert.
        PaymentPlanInstallment(  # One planned installment row.
            plan=plan, seq_no=i + 1,  # Link plan and sequence number.
            due_date=_due_date(plan.start_date, i, plan.frequency),  # Compute due date.
            amount=amounts[i],  # Store kobo amount for this installment.
        )
        for i in range(count)  # Build one row per installment.
    ]
    PaymentPlanInstallment.objects.bulk_create(rows)  # Persist schedule rows efficiently.
    return list(plan.installments.all())  # Return persisted installment rows.


def activate_payment_plan(plan, *, actor_user=None):  # Public wrapper for plan activation.
    """Commit a DRAFT plan: validate the schedule, mark ACTIVE, sync any settlement."""
    try:  # Atomic worker performs state transition.
        return _activate_payment_plan_atomic(plan, actor_user=actor_user)  # Activate the plan.
    except FinanceError as exc:  # Failed activation should be auditable.
        record_rejection(  # Record durable rejection.
            entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_ACTIVATED,  # Existing activation audit action.
            exc=exc, actor_user=actor_user, target=plan,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _activate_payment_plan_atomic(plan, *, actor_user=None):  # Transactional payment-plan activation.
    if plan.plan_status != PaymentPlanStatus.DRAFT:  # Only draft plans can activate.
        raise PostingError(
            f"Payment plan {plan.document_number or plan.pk} is "
            f"'{plan.plan_status}'; only a draft plan can be activated.",
        )
    if not plan.installments.exists():  # Activation requires a built schedule.
        raise PostingError("Build the installment schedule before activating the plan.")
    if plan.scheduled_total != plan.total_amount:  # Schedule amounts must reconcile to plan total.
        raise PostingError(
            f"Installments sum to {plan.scheduled_total} kobo but the plan total is "
            f"{plan.total_amount} kobo; rebuild the schedule.",
        )

    # Snapshot the invoice settlement that predates the plan. Because the plan spreads
    # the *outstanding balance* (total minus what's already settled — e.g. a waiver),
    # that prior settlement is baked into total_amount and must not also be counted as
    # installment progress. Only settlement beyond this baseline advances the schedule.
    if plan.invoice_id is not None:  # Linked invoice plans need settlement baseline.
        plan.invoice.refresh_from_db()  # Reload invoice settlement fields.
        plan.baseline_settled = plan.invoice.settled_amount  # Snapshot pre-plan settlement.
    plan.plan_status = PaymentPlanStatus.ACTIVE  # Move plan into active lifecycle state.
    plan.save(update_fields=["plan_status", "baseline_settled", "updated_at"])  # Persist activation fields.
    record(  # Audit successful activation.
        entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_ACTIVATED,  # Audit action.
        actor_user=actor_user, target=plan,  # Actor and target context.
        message=f"Activated {plan.installment_count}-installment plan "  # Human-readable activation message.
                f"for {plan.customer.code} ({plan.total_amount} kobo).",  # Customer and amount.
        total=plan.total_amount, installments=plan.installment_count,  # Structured metadata.
    )
    # Reflect any settlement already on the linked invoice.  # Keep schedule progress current immediately.
    refresh_plan_progress(plan, actor_user=actor_user)  # Apply post-baseline settlement to installments.
    return plan  # Return activated plan.


def cancel_payment_plan(plan, *, actor_user=None):  # Cancel a payment plan.
    """Cancel a plan that is no longer being followed. Idempotent on terminal states."""
    if plan.plan_status in (PaymentPlanStatus.COMPLETED, PaymentPlanStatus.CANCELLED):  # Terminal states are idempotent.
        return plan
    plan.plan_status = PaymentPlanStatus.CANCELLED  # Mark plan cancelled.
    plan.save(update_fields=["plan_status", "updated_at"])  # Persist lifecycle change.
    record(  # Audit cancellation.
        entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_CANCELLED,  # Audit action.
        actor_user=actor_user, target=plan,  # Actor and target context.
        message=f"Cancelled payment plan {plan.document_number} for {plan.customer.code}.",  # Human-readable message.
    )
    return plan  # Return cancelled plan.


@transaction.atomic
def refresh_plan_progress(plan, *, settled_amount=None, actor_user=None):  # Recalculate installment settlement statuses.
    """Distribute settlement across the plan's installments oldest-first.

    ``settled_amount`` (kobo) overrides the source figure; otherwise, for a plan linked
    to an invoice, the settlement *since activation* is used — the invoice's
    :attr:`Invoice.settled_amount` (cash + non-cash credits) minus the plan's
    ``baseline_settled`` snapshot, so pre-plan credits/waivers baked into the plan total
    aren't double-counted. Each installment is filled in sequence, its ``amount_settled``
    and derived status updated. When the whole plan is settled it flips to COMPLETED.
    Returns the plan. No GL effect — this only mirrors money already posted elsewhere.
    """
    if plan.plan_status not in (PaymentPlanStatus.ACTIVE, PaymentPlanStatus.COMPLETED):  # Draft/cancelled plans do not track progress.
        return plan

    if settled_amount is None:  # Derive settlement from linked invoice or current plan state.
        if plan.invoice_id is not None:  # Invoice-backed plans use invoice settlement.
            plan.invoice.refresh_from_db()  # Reload invoice settled amount.
            # Only settlement *after* the plan's baseline counts toward installments —
            # pre-plan credits/waivers are already reflected in the smaller total_amount.  # Avoid double-counting.
            settled_amount = plan.invoice.settled_amount - plan.baseline_settled  # Post-baseline settlement.
        else:  # Standalone plans have no invoice source.
            settled_amount = plan.settled_total  # standalone plan: leave as-is
    remaining = max(int(settled_amount), 0)  # Clamp negative settlement to zero.

    for inst in plan.installments.order_by("seq_no", "id"):  # Fill installments oldest-first.
        applied = min(inst.amount, remaining)  # Amount applied to this installment.
        if applied >= inst.amount:  # Full installment settled.
            status = InstallmentStatus.PAID  # Mark paid.
        elif applied > 0:  # Some settlement reached this installment.
            status = InstallmentStatus.PARTIAL  # Mark partial.
        else:  # No settlement applied to this installment.
            status = InstallmentStatus.PENDING  # Mark pending.
        if inst.amount_settled != applied or inst.status != status:  # Avoid unnecessary writes.
            inst.amount_settled = applied  # Store applied amount.
            inst.status = status  # Store derived status.
            inst.save(update_fields=["amount_settled", "status", "updated_at"])  # Persist installment progress.
        remaining -= applied  # Reduce settlement available for later installments.

    if plan.settled_total >= plan.total_amount and plan.plan_status != PaymentPlanStatus.COMPLETED:  # Plan is fully settled.
        plan.plan_status = PaymentPlanStatus.COMPLETED  # Mark plan complete.
        plan.save(update_fields=["plan_status", "updated_at"])  # Persist completion status.
        record(  # Audit completion once.
            entity=plan.entity, action=FinanceAuditAction.PAYMENT_PLAN_COMPLETED,  # Audit action.
            actor_user=actor_user, target=plan,  # Actor and target context.
            message=f"Payment plan {plan.document_number} fully settled.",  # Human-readable message.
        )
    return plan  # Return refreshed plan.


def refresh_plans_for_invoice(invoice, *, actor_user=None):  # Sync active/completed plans for a changed invoice.
    """Re-sync every live payment plan attached to ``invoice`` after its settled amount
    moved (a receipt, credit-note allocation or write-off).

    A plan tracks the invoice's ``settled_amount`` but does not observe it, so this is
    the push that keeps an ACTIVE/COMPLETED plan in step when cash (or non-cash credit)
    lands on the invoice. No GL effect. Safe to call when the invoice has no plan.
    """
    if invoice is None or invoice.pk is None:  # Unsaved/missing invoices cannot have plans.
        return
    from .constants import PaymentPlanStatus  # Local import mirrors model query lifecycle enum.
    from .models import PaymentPlan  # Local import avoids model import cycles.

    plans = PaymentPlan.objects.filter(  # Find live plans tied to this invoice.
        invoice=invoice,  # Scope to changed invoice.
        plan_status__in=(PaymentPlanStatus.ACTIVE, PaymentPlanStatus.COMPLETED),  # Only live/completed plans need sync.
    )
    for plan in plans:  # Refresh each matching plan.
        refresh_plan_progress(plan, actor_user=actor_user)  # Recompute installment statuses.


# --------------------------------------------------------------------------- #
# Concessions — discounts / waivers / scholarships (Dr allowance, Cr AR)        #
# --------------------------------------------------------------------------- #

def post_concession(concession, *, actor_user=None):  # Public wrapper for concession posting.
    """Post a :class:`Concession` (``Dr discounts & allowances, Cr AR control``).

    Clears ``concession.amount`` of the linked invoice's balance via
    ``amount_credited`` and refreshes any active payment plan on that invoice. Records
    a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker performs concession accounting.
        return _post_concession_atomic(concession, actor_user=actor_user)  # Post concession.
    except FinanceError as exc:  # Failed concessions should be auditable.
        record_rejection(  # Record durable rejection.
            entity=concession.entity, action=FinanceAuditAction.CONCESSION_POSTED,  # Audit action.
            exc=exc, actor_user=actor_user, target=concession,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _post_concession_atomic(concession, *, actor_user=None):  # Transactional concession posting implementation.
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
    from .models import JournalEntry, JournalLine, PaymentPlan  # Journal and plan models used by posting.

    if concession.status != DocumentStatus.DRAFT:  # Only draft concessions can post.
        raise PostingError(
            f"Concession {concession.document_number or concession.pk} is "
            f"'{concession.status}'; only a draft concession can be posted.",
        )

    invoice = concession.invoice  # Invoice receiving the concession credit.
    if invoice.status != DocumentStatus.POSTED:  # Only posted invoices have AR to reduce.
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
            f"a concession can only reduce a posted invoice.",
        )

    balance = invoice.balance_due  # Current outstanding invoice balance.
    if balance <= 0:  # Nothing remains to concede.
        raise PostingError("Invoice has no outstanding balance to concede.")
    amount = int(concession.amount)  # Normalize concession amount to integer kobo.
    if amount <= 0:  # Concession must reduce a positive amount.
        raise PostingError("A concession must have a positive amount to post.")
    if amount > balance:  # Cannot credit more than invoice balance.
        raise PostingError(
            f"Concession amount ({amount} kobo) exceeds the outstanding balance "
            f"({balance} kobo).",
        )

    customer = concession.customer  # Customer controls AR account.
    ar_account = customer.receivable_account  # AR control account for credit side.
    if ar_account is None:  # AR cannot be credited without a control account.
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")

    allowance = concession.allowance_account or resolve_account(  # Use explicit allowance account or default.
        concession.entity, DISCOUNTS_ALLOWED_CODE, label="discounts & allowances",  # Resolve default allowance account.
    )
    period = resolve_period(concession.entity, concession.concession_date)  # Resolve concession period.
    label = concession.get_kind_display()  # Human concession kind label.
    entry = JournalEntry.objects.create(  # Create concession journal header.
        entity=concession.entity, branch=concession.branch,  # Scope entity and optional branch.
        date=concession.concession_date, period=period, source=JournalSource.SALES,  # Sales-side concession entry.
        narration=concession.reason or f"{label} {concession.document_number or ''}".strip(),  # Narration from reason/kind.
        reference=concession.reference, created_by=actor_user,  # External reference and actor.
    )
    JournalLine.objects.create(  # Debit discounts/allowances contra-revenue.
        entry=entry, account=allowance, debit=amount, credit=0,  # Dr allowance.
        description=f"{label}: {customer.code}", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(  # Credit AR to reduce invoice balance.
        entry=entry, account=ar_account, debit=0, credit=amount,  # Cr receivables.
        description=f"AR {label.lower()}: {customer.code}", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post concession journal.

    concession.allowance_account = allowance  # Persist account used.
    concession.journal = entry  # Link concession to journal.
    concession.status = DocumentStatus.POSTED  # Mark concession posted.
    concession.save(update_fields=["allowance_account", "journal", "status", "updated_at"])  # Persist posting fields.

    invoice.amount_credited += amount  # Increase non-cash credit applied to invoice.
    invoice.refresh_payment_status(save=False)  # Recompute invoice payment status.
    invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])  # Persist invoice settlement fields.

    record(  # Audit successful concession.
        entity=concession.entity, action=FinanceAuditAction.CONCESSION_POSTED,  # Audit action.
        actor_user=actor_user, target=concession,  # Actor and target context.
        message=f"Granted {label.lower()} of {amount} kobo on invoice "  # Human-readable message.
                f"{invoice.document_number} for {customer.code}.",  # Invoice and customer context.
        journal_id=entry.pk, amount=amount, kind=concession.kind,  # Structured concession metadata.
        balance_after=invoice.balance_due,  # Remaining invoice balance.
    )

    # A concession settles part of the invoice — keep any active plan in step.  # Payment plans mirror settlement.
    for plan in PaymentPlan.objects.filter(  # Find active plans linked to the invoice.
        invoice=invoice, plan_status=PaymentPlanStatus.ACTIVE,  # Active invoice-backed plans.
    ):
        refresh_plan_progress(plan, actor_user=actor_user)  # Recompute installment progress.
    return concession  # Return posted concession.
