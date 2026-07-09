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
from __future__ import annotations  # Defer annotation evaluation for app startup.

from collections import defaultdict  # Groups journal amounts by account/cost-center keys.

from django.db import transaction  # Keeps claim posting and settlement writes atomic.

from .accounts import resolve_account  # Resolves configured control accounts by code.
from .audit import record, record_rejection  # Finance audit logging helpers.
from .constants import (  # Import project symbols used by this module.
    ACCRUED_REIMBURSEMENT_CODE,  # Default reimbursement liability account code.
    DocumentStatus,  # Draft/posted/cancelled lifecycle statuses.
    FinanceAuditAction,  # Audit action enum values.
    InvoicePaymentStatus,  # Imported payment status enum used by claim model semantics.
    JournalSource,  # Journal origin categories.
)  # Close the grouped expression.
from .exceptions import ExpenseClaimError, FinanceError  # Domain and base finance errors.
from .posting import post_journal, resolve_period  # GL posting and period lookup helpers.
from .receivables import compute_line_net, compute_tax  # Shared line pricing/tax helpers.


def price_expense_claim(claim) -> None:  # Recalculate claim line and header totals.
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the claim totals."""
    from .models import ExpenseClaimLine  # Local import avoids model import cycles.

    for line in claim.lines.all():  # Reprice every claim line.
        net = compute_line_net(line.quantity, line.unit_price)  # Compute net amount in kobo.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use tax code basis points when present.
        tax = compute_tax(net, rate)  # Compute recoverable input tax in kobo.
        if line.net_amount != net or line.tax_amount != tax:  # Avoid unnecessary writes when values are unchanged.
            ExpenseClaimLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)  # Persist recalculated amounts.
    claim.recompute_totals(save=True)  # Roll line totals up to the claim header.


def post_expense_claim(claim, *, actor_user=None):  # Public wrapper that posts a claim and audits failures.
    """Price, validate and post an :class:`ExpenseClaim`, raising its liability journal.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker performs the accounting mutation.
        return _post_expense_claim_atomic(claim, actor_user=actor_user)  # Post the claim in one transaction.
    except FinanceError as exc:  # Failed postings should leave durable audit evidence.
        record_rejection(  # Record the rejection outside the rolled-back posting work.
            entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_POST_REJECTED,  # Audit action for rejected claim post.
            exc=exc, actor_user=actor_user, target=claim,  # Capture error and actor context.
        )  # Close the grouped expression.
        raise  # Preserve original exception for the caller.


@transaction.atomic  # Apply the decorator to this callable.
def _post_expense_claim_atomic(claim, *, actor_user=None):  # Transactional claim posting implementation.
    from .models import JournalEntry, JournalLine  # Journal models used to create the GL entry.

    if claim.status != DocumentStatus.DRAFT:  # Only draft claims can be posted.
        raise ExpenseClaimError(  # Raise the domain error for this path.
            f"Expense claim {claim.document_number or claim.pk} is '{claim.status}', "
            f"only a draft can be posted.",
        )  # Close the grouped expression.

    price_expense_claim(claim)  # Ensure line and header amounts are current before posting.
    if claim.total <= 0:  # A claim must create a positive liability.
        raise ExpenseClaimError("An expense claim must have a positive total to post.")

    reimbursement = claim.reimbursement_account or resolve_account(  # Use configured claim account or default control account.
        claim.entity, ACCRUED_REIMBURSEMENT_CODE, label="accrued reimbursement",  # Resolve accrued reimbursement liability.
    )  # Close the grouped expression.
    period = resolve_period(claim.entity, claim.claim_date)  # Find the open accounting period.

    entry = JournalEntry.objects.create(  # Create the expense claim journal header.
        entity=claim.entity, branch=claim.branch,  # Scope entry to entity and optional branch.
        date=claim.claim_date, period=period,  # Use claim date and resolved period.
        source=JournalSource.PURCHASE, currency=claim.currency,  # Treat the claim as a purchase-side journal.
        narration=claim.narration or claim.title or f"Expense claim {claim.document_number or ''}".strip(),  # Prefer explicit narration.
        created_by=actor_user,  # Attribute the journal to the acting user.
    )  # Close the grouped expression.

    line_no = 0  # Journal line counter.
    # Dr expense, grouped by (account, cost centre) so the cost-centre split survives into
    # the GL. Expense is P&L, so it carries the analytics; the input-tax and reimbursement
    # liability lines (below) do not.  # Preserve analytics only where financially meaningful.
    expense_by_key: dict[tuple[int, int | None], int] = defaultdict(int)  # Net expense grouped by account and cost center.
    expense_objs: dict[tuple[int, int | None], tuple] = {}  # Account/cost-center objects for grouped expense lines.
    tax_by_account: dict[int, int] = defaultdict(int)  # Recoverable input tax grouped by tax account.
    tax_objs: dict[int, object] = {}  # Tax account objects for grouped tax lines.
    for line in claim.lines.select_related(  # Load posting targets for each claim line.
        "expense_account", "tax_code__paid_account", "cost_center",  # Expense, tax, and analytics relations.
    ):  # Start the nested execution block.
        key = (line.expense_account_id, line.cost_center_id)  # Group expense by account and cost center.
        expense_by_key[key] += line.net_amount  # Accumulate net expense amount.
        expense_objs[key] = (line.expense_account, line.cost_center)  # Store objects for journal creation.
        if line.tax_amount:  # Tax-bearing lines require an input tax account.
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None  # Resolve recoverable tax account.
            if tax_acc is None:  # A tax amount without a paid account cannot post.
                raise ExpenseClaimError(  # Raise the domain error for this path.
                    f"Tax code '{line.tax_code.code}' has no paid (input) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )  # Close the grouped expression.
            tax_by_account[tax_acc.id] += line.tax_amount  # Accumulate input tax amount.
            tax_objs[tax_acc.id] = tax_acc  # Store the tax account object.

    for (acc_id, cc_id), amount in expense_by_key.items():  # Emit grouped expense debit lines.
        if amount == 0:  # Skip empty groups.
            continue  # Skip to the next loop iteration.
        line_no += 1  # Advance the journal line number.
        expense_account, cost_center = expense_objs[(acc_id, cc_id)]  # Retrieve objects for this group.
        JournalLine.objects.create(  # Debit expense for the grouped amount.
            entry=entry, account=expense_account, debit=amount, credit=0,  # Dr expense.
            description="Expense", cost_center=cost_center, line_no=line_no,  # Preserve cost-center analytics.
        )  # Close the grouped expression.
    for acc_id, amount in tax_by_account.items():  # Emit grouped input tax debit lines.
        line_no += 1  # Advance the journal line number.
        JournalLine.objects.create(  # Debit recoverable input tax.
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,  # Dr input tax.
            description="Input tax", line_no=line_no,  # Label the tax line.
        )  # Close the grouped expression.
    line_no += 1  # Final line is the reimbursement liability credit.
    JournalLine.objects.create(  # Credit accrued reimbursement for the total owed.
        entry=entry, account=reimbursement, debit=0, credit=claim.total,  # Cr reimbursement liability.
        description=f"Owed to {claim.claimant_name or claim.claimant_id or 'claimant'}",  # Identify claimant where possible.
        line_no=line_no,  # Store line order.
    )  # Close the grouped expression.

    post_journal(entry, actor_user=actor_user)  # Validate balance and mark journal posted.

    claim.journal = entry  # Link the claim to its posting journal.
    claim.reimbursement_account = reimbursement  # Persist the liability account used.
    claim.status = DocumentStatus.POSTED  # Mark claim as posted.
    claim.refresh_payment_status(save=False)  # Initialize paid/unpaid status.
    claim.save(update_fields=[  # Persist only fields changed by posting.
        "journal", "reimbursement_account", "status", "payment_status", "updated_at",  # Posting and settlement fields.
    ])  # Execute the module statement.

    record(  # Audit the successful claim posting.
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_POSTED,  # Audit action for posted claim.
        actor_user=actor_user, target=claim,  # Actor and target context.
        message=f"Posted expense claim ({claim.total} kobo).",  # Human-readable audit message.
        journal_id=entry.pk, total=claim.total, tax=claim.tax_total,  # Structured audit metadata.
    )  # Close the grouped expression.
    return claim  # Return the posted claim.


def settle_expense_claim(claim, *, bank_account, pay_date, amount=None, actor_user=None):  # Public wrapper for reimbursements.
    """Reimburse a posted claim: ``Dr accrued reimbursement, Cr bank``.

    ``amount`` defaults to the full outstanding balance; a smaller amount records a
    partial reimbursement. Records a durable rejection audit on any FinanceError.
    """
    try:  # Atomic worker performs reimbursement accounting.
        return _settle_expense_claim_atomic(  # Settle all or part of the claim.
            claim, bank_account=bank_account, pay_date=pay_date,  # Payment source and date.
            amount=amount, actor_user=actor_user,  # Optional partial amount and actor.
        )  # Close the grouped expression.
    except FinanceError as exc:  # Failed settlements should be auditable.
        record_rejection(  # Record the rejected settlement attempt.
            entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_SETTLED,  # Existing audit action for settlement attempts.
            exc=exc, actor_user=actor_user, target=claim,  # Capture error and actor context.
        )  # Close the grouped expression.
        raise  # Preserve original exception for the caller.


@transaction.atomic  # Apply the decorator to this callable.
def _settle_expense_claim_atomic(claim, *, bank_account, pay_date, amount=None, actor_user=None):  # Transactional reimbursement.
    from .models import JournalEntry, JournalLine  # Journal models used to create reimbursement entry.

    if claim.status != DocumentStatus.POSTED:  # Only posted liabilities can be settled.
        raise ExpenseClaimError("Only a posted expense claim can be settled.")
    outstanding = claim.balance_due  # Amount still owed to the claimant.
    if outstanding <= 0:  # Fully reimbursed claims cannot be settled again.
        raise ExpenseClaimError("This claim has no outstanding balance to settle.")
    pay = outstanding if amount is None else min(int(amount), outstanding)  # Default to full balance and cap partials.
    if pay <= 0:  # Reject zero or negative settlement requests.
        raise ExpenseClaimError("Settlement amount must be positive.")

    reimbursement = claim.reimbursement_account or resolve_account(  # Use stored liability account or resolve default.
        claim.entity, ACCRUED_REIMBURSEMENT_CODE, label="accrued reimbursement",  # Resolve accrued reimbursement account.
    )  # Close the grouped expression.
    period = resolve_period(claim.entity, pay_date)  # Find the open accounting period for payment date.

    entry = JournalEntry.objects.create(  # Create reimbursement payment journal header.
        entity=claim.entity, branch=claim.branch,  # Scope entry to entity and optional branch.
        date=pay_date, period=period, source=JournalSource.BANK,  # Bank source because cash leaves the bank.
        currency=claim.currency,  # Use claim currency.
        narration=f"Reimburse {claim.document_number or claim.pk}",  # Identify the reimbursed claim.
        created_by=actor_user,  # Attribute the journal to the acting user.
    )  # Close the grouped expression.
    JournalLine.objects.create(  # Debit accrued reimbursement to clear liability.
        entry=entry, account=reimbursement, debit=pay, credit=0,  # Dr liability.
        description="Reimbursement", line_no=1,  # First reimbursement line.
    )  # Close the grouped expression.
    JournalLine.objects.create(  # Credit bank for cash paid out.
        entry=entry, account=bank_account.gl_account, debit=0, credit=pay,  # Cr bank account.
        description="Reimbursement paid", line_no=2,  # Second reimbursement line.
    )  # Close the grouped expression.
    post_journal(entry, actor_user=actor_user)  # Validate balance and mark journal posted.

    claim.amount_paid += pay  # Increase reimbursed amount.
    claim.refresh_payment_status(save=False)  # Recompute unpaid/partial/paid status.
    claim.save(update_fields=["amount_paid", "payment_status", "updated_at"])  # Persist settlement fields.

    record(  # Audit the successful reimbursement.
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_SETTLED,  # Audit action for settlement.
        actor_user=actor_user, target=claim,  # Actor and target context.
        message=f"Reimbursed {pay} kobo on claim {claim.document_number or claim.pk}.",  # Human-readable audit message.
        journal_id=entry.pk, amount=pay, payment_status=claim.payment_status,  # Structured audit metadata.
    )  # Close the grouped expression.
    return claim  # Return the settled claim.


@transaction.atomic  # Apply the decorator to this callable.
def void_expense_claim(claim, *, actor_user=None):  # Reverse a posted unreimbursed expense claim.
    """Void a **posted, un-reimbursed** expense claim.

    Reverses the posting journal (an audit-correct mirror entry that backs out the
    expense and the accrued-reimbursement liability) and marks the claim CANCELLED —
    the "undo" for a claim posted in error, without hand-reversing the journal.

    Refuses once any reimbursement has been paid: the cash has already left the bank,
    so that must be handled first (reverse the reimbursement) before the claim can be
    voided. ``reject`` remains the path for a claim still in DRAFT.
    """
    from .posting import reverse_journal  # Local import avoids widening the module dependency graph.

    if claim.status != DocumentStatus.POSTED:  # Only posted claims have a journal to reverse.
        raise ExpenseClaimError(  # Raise the domain error for this path.
            f"Only a posted claim can be voided (this is '{claim.status}'); "
            f"a draft is rejected, not voided.",
        )  # Close the grouped expression.
    if claim.amount_paid > 0:  # Do not void claims after cash has already been paid.
        raise ExpenseClaimError(  # Raise the domain error for this path.
            "This claim has already been reimbursed; reverse the reimbursement "
            "before voiding the claim.",
        )  # Close the grouped expression.
    if claim.journal_id is None:  # A posted claim should always have a journal.
        raise ExpenseClaimError("Claim has no posting journal to reverse.")

    reverse_journal(claim.journal, actor_user=actor_user)  # Post the mirror reversal journal.
    claim.status = DocumentStatus.CANCELLED  # Mark the claim voided/cancelled.
    claim.save(update_fields=["status", "updated_at"])  # Persist only lifecycle fields.
    record(  # Audit the successful void.
        entity=claim.entity, action=FinanceAuditAction.EXPENSE_CLAIM_VOIDED,  # Audit action for void.
        actor_user=actor_user, target=claim,  # Actor and target context.
        message=f"Voided expense claim {claim.document_number or claim.pk} "  # Human-readable audit message.
                f"(reversed journal {claim.journal_id}).",  # Include reversed journal id.
        journal_id=claim.journal_id, total=claim.total,  # Structured audit metadata.
    )  # Close the grouped expression.
    return claim  # Return the voided claim.
