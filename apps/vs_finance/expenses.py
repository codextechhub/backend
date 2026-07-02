"""Expense-claim services — staff reimbursements as a mini accounts-payable cycle.

An employee spends their own money on the entity's behalf; the entity owes them. That
liability is recognised on posting and cleared when they are paid back:

* **Post** (:func:`post_expense_claim`): ``Dr expense(s) (+ Dr recoverable input VAT),
  Cr accrued reimbursement`` — the amount owed to the employee.
* **Settle** (:func:`settle_expense_claim`): ``Dr accrued reimbursement, Cr bank`` —
  the reimbursement payment.

All amounts are integer kobo; tax uses the same basis-point discipline as the rest of
the engine.
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    ACCRUED_REIMBURSEMENT_CODE,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import ExpenseClaimError, FinanceError
from .posting import post_journal, resolve_period
from .receivables import compute_line_net, compute_tax


def price_expense_claim(claim) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the claim totals."""
    from .models import ExpenseClaimLine

    for line in claim.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            ExpenseClaimLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    claim.recompute_totals(save=True)


def post_expense_claim(claim, *, actor_user=None):
    """Price, validate and post an :class:`ExpenseClaim`, raising its liability journal.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:
        return _post_expense_claim_atomic(claim, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=claim,
        )
        raise


@transaction.atomic
def _post_expense_claim_atomic(claim, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if claim.status != DocumentStatus.DRAFT:
        raise ExpenseClaimError(
            f"Expense claim {claim.document_number or claim.pk} is '{claim.status}', "
            f"only a draft can be posted.",
        )

    price_expense_claim(claim)
    if claim.total <= 0:
        raise ExpenseClaimError("An expense claim must have a positive total to post.")

    reimbursement = claim.reimbursement_account or resolve_account(
        claim.entity, ACCRUED_REIMBURSEMENT_CODE, label="accrued reimbursement",
    )
    period = resolve_period(claim.entity, claim.claim_date)

    entry = JournalEntry.objects.create(
        entity=claim.entity, branch=claim.branch,
        date=claim.claim_date, period=period,
        source=JournalSource.PURCHASE, currency=claim.currency,
        narration=claim.narration or claim.title or f"Expense claim {claim.document_number or ''}".strip(),
        created_by=actor_user,
    )

    line_no = 0
    # Dr expense, grouped by (account, cost centre) so the cost-centre split survives into
    # the GL. Expense is P&L, so it carries the analytics; the input-tax and reimbursement
    # liability lines (below) do not.
    expense_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    expense_objs: dict[tuple[int, int | None], tuple] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in claim.lines.select_related(
        "expense_account", "tax_code__paid_account", "cost_center",
    ):
        key = (line.expense_account_id, line.cost_center_id)
        expense_by_key[key] += line.net_amount
        expense_objs[key] = (line.expense_account, line.cost_center)
        if line.tax_amount:
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None
            if tax_acc is None:
                raise ExpenseClaimError(
                    f"Tax code '{line.tax_code.code}' has no paid (input) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    for (acc_id, cc_id), amount in expense_by_key.items():
        if amount == 0:
            continue
        line_no += 1
        expense_account, cost_center = expense_objs[(acc_id, cc_id)]
        JournalLine.objects.create(
            entry=entry, account=expense_account, debit=amount, credit=0,
            description="Expense", cost_center=cost_center, line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,
            description="Input tax", line_no=line_no,
        )
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=reimbursement, debit=0, credit=claim.total,
        description=f"Owed to {claim.claimant_name or claim.claimant_id or 'claimant'}",
        line_no=line_no,
    )

    post_journal(entry, actor_user=actor_user)

    claim.journal = entry
    claim.reimbursement_account = reimbursement
    claim.status = DocumentStatus.POSTED
    claim.refresh_payment_status(save=False)
    claim.save(update_fields=[
        "journal", "reimbursement_account", "status", "payment_status", "updated_at",
    ])

    record(
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_POSTED,
        actor_user=actor_user, target=claim,
        message=f"Posted expense claim ({claim.total} kobo).",
        journal_id=entry.pk, total=claim.total, tax=claim.tax_total,
    )
    return claim


def settle_expense_claim(claim, *, bank_account, pay_date, amount=None, actor_user=None):
    """Reimburse a posted claim: ``Dr accrued reimbursement, Cr bank``.

    ``amount`` defaults to the full outstanding balance; a smaller amount records a
    partial reimbursement. Records a durable rejection audit on any FinanceError.
    """
    try:
        return _settle_expense_claim_atomic(
            claim, bank_account=bank_account, pay_date=pay_date,
            amount=amount, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_SETTLED,
            exc=exc, actor_user=actor_user, target=claim,
        )
        raise


@transaction.atomic
def _settle_expense_claim_atomic(claim, *, bank_account, pay_date, amount=None, actor_user=None):
    from .models import JournalEntry, JournalLine

    if claim.status != DocumentStatus.POSTED:
        raise ExpenseClaimError("Only a posted expense claim can be settled.")
    outstanding = claim.balance_due
    if outstanding <= 0:
        raise ExpenseClaimError("This claim has no outstanding balance to settle.")
    pay = outstanding if amount is None else min(int(amount), outstanding)
    if pay <= 0:
        raise ExpenseClaimError("Settlement amount must be positive.")

    reimbursement = claim.reimbursement_account or resolve_account(
        claim.entity, ACCRUED_REIMBURSEMENT_CODE, label="accrued reimbursement",
    )
    period = resolve_period(claim.entity, pay_date)

    entry = JournalEntry.objects.create(
        entity=claim.entity, branch=claim.branch,
        date=pay_date, period=period, source=JournalSource.BANK,
        currency=claim.currency,
        narration=f"Reimburse {claim.document_number or claim.pk}",
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=reimbursement, debit=pay, credit=0,
        description="Reimbursement", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=pay,
        description="Reimbursement paid", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    claim.amount_paid += pay
    claim.refresh_payment_status(save=False)
    claim.save(update_fields=["amount_paid", "payment_status", "updated_at"])

    record(
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_SETTLED,
        actor_user=actor_user, target=claim,
        message=f"Reimbursed {pay} kobo on claim {claim.document_number or claim.pk}.",
        journal_id=entry.pk, amount=pay, payment_status=claim.payment_status,
    )
    return claim


@transaction.atomic
def void_expense_claim(claim, *, actor_user=None):
    """Void a **posted, un-reimbursed** expense claim.

    Reverses the posting journal (an audit-correct mirror entry that backs out the
    expense and the accrued-reimbursement liability) and marks the claim CANCELLED —
    the "undo" for a claim posted in error, without hand-reversing the journal.

    Refuses once any reimbursement has been paid: the cash has already left the bank,
    so that must be handled first (reverse the reimbursement) before the claim can be
    voided. ``reject`` remains the path for a claim still in DRAFT.
    """
    from .posting import reverse_journal

    if claim.status != DocumentStatus.POSTED:
        raise ExpenseClaimError(
            f"Only a posted claim can be voided (this is '{claim.status}'); "
            f"a draft is rejected, not voided.",
        )
    if claim.amount_paid > 0:
        raise ExpenseClaimError(
            "This claim has already been reimbursed; reverse the reimbursement "
            "before voiding the claim.",
        )
    if claim.journal_id is None:
        raise ExpenseClaimError("Claim has no posting journal to reverse.")

    reverse_journal(claim.journal, actor_user=actor_user)
    claim.status = DocumentStatus.CANCELLED
    claim.save(update_fields=["status", "updated_at"])
    record(
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_VOIDED,
        actor_user=actor_user, target=claim,
        message=f"Voided expense claim {claim.document_number or claim.pk} "
                f"(reversed journal {claim.journal_id}).",
        journal_id=claim.journal_id, total=claim.total,
    )
    return claim
