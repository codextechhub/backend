"""Payroll services — the two-step accrue-then-disburse payroll cycle.

Payroll is booked in two postings, deliberately separate because the cost is incurred
before the cash leaves (and the statutory deductions are held in between):

* **Accrual** (:func:`post_payroll`): recognise the whole cost and park each liability —
  ``Dr salary expense (Σgross), Cr PAYE payable (Σpaye), Cr pension payable (Σpension),
  Cr net wages payable (Σnet)``.
* **Disbursement** (:func:`pay_payroll`): when employees are actually paid, clear the
  net-pay liability — ``Dr net wages payable (Σnet), Cr bank (Σnet)``.

The statutory liabilities (PAYE, pension) stay on the balance sheet until remitted to
the authorities — a separate AP payment outside this module. ``net = gross - paye -
pension`` per employee; all amounts are integer kobo.
"""
from __future__ import annotations

from django.db import transaction

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    NET_WAGES_PAYABLE_CODE,
    PAYE_PAYABLE_CODE,
    PENSION_PAYABLE_CODE,
    PayrollRunStatus,
    SALARIES_EXPENSE_CODE,
)
from .exceptions import FinanceError, PayrollError
from .posting import post_journal, resolve_period


def compute_payroll(run) -> None:
    """Derive each line's ``net_amount`` (gross − paye − pension) and roll up totals."""
    from .models import PayrollLine

    for line in run.lines.all():
        net = line.gross_amount - line.paye_amount - line.pension_amount
        if line.net_amount != net:
            PayrollLine.objects.filter(pk=line.pk).update(net_amount=net)
    run.recompute_totals(save=True)


def _accounts_for(run):
    """Resolve the four posting accounts for a run, falling back to the seeded defaults."""
    entity = run.entity
    salary = run.salary_expense_account or resolve_account(
        entity, SALARIES_EXPENSE_CODE, label="salary expense",
    )
    paye = run.paye_payable_account or resolve_account(
        entity, PAYE_PAYABLE_CODE, label="PAYE payable",
    )
    pension = run.pension_payable_account or resolve_account(
        entity, PENSION_PAYABLE_CODE, label="pension payable",
    )
    net = run.net_payable_account or resolve_account(
        entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",
    )
    return salary, paye, pension, net


def post_payroll(run, *, actor_user=None):
    """Compute, validate and post a payroll run's **accrual** journal.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:
        return _post_payroll_atomic(run, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=run,
        )
        raise


@transaction.atomic
def _post_payroll_atomic(run, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if run.run_status != PayrollRunStatus.DRAFT:
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"only a draft can be posted.",
        )

    if not run.lines.exists():
        raise PayrollError("A payroll run must have at least one line to post.")

    compute_payroll(run)
    if run.gross_total <= 0:
        raise PayrollError("A payroll run must have a positive gross total to post.")
    for line in run.lines.all():
        if line.net_amount < 0:
            raise PayrollError(
                f"Net pay is negative for {line.employee_name or line.employee_id}: "
                f"deductions exceed gross.",
            )

    salary, paye, pension, net = _accounts_for(run)
    period = resolve_period(run.entity, run.pay_date)

    entry = JournalEntry.objects.create(
        entity=run.entity, branch=run.branch,
        date=run.pay_date, period=period, source=JournalSource.PAYROLL,
        currency=run.currency,
        narration=run.narration or f"Payroll {run.period_label or run.document_number or ''}".strip(),
        created_by=actor_user,
    )
    line_no = 1
    JournalLine.objects.create(
        entry=entry, account=salary, debit=run.gross_total, credit=0,
        description="Gross salaries", line_no=line_no,
    )
    for account, amount, label in (
        (paye, run.paye_total, "PAYE payable"),
        (pension, run.pension_total, "Pension payable"),
        (net, run.net_total, "Net wages payable"),
    ):
        if amount <= 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=account, debit=0, credit=amount,
            description=label, line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)

    run.journal = entry
    run.salary_expense_account = salary
    run.paye_payable_account = paye
    run.pension_payable_account = pension
    run.net_payable_account = net
    run.run_status = PayrollRunStatus.POSTED
    run.status = DocumentStatus.POSTED
    run.save(update_fields=[
        "journal", "salary_expense_account", "paye_payable_account",
        "pension_payable_account", "net_payable_account",
        "run_status", "status", "updated_at",
    ])

    record(
        entity=run.entity, action=FinanceAuditAction.PAYROLL_POSTED,
        actor_user=actor_user, target=run,
        message=f"Accrued payroll: gross {run.gross_total}, net {run.net_total} kobo.",
        journal_id=entry.pk, gross=run.gross_total, paye=run.paye_total,
        pension=run.pension_total, net=run.net_total,
    )
    return run


def pay_payroll(run, *, bank_account=None, pay_date=None, actor_user=None):
    """Disburse a posted run's net pay: ``Dr net wages payable, Cr bank``."""
    try:
        return _pay_payroll_atomic(
            run, bank_account=bank_account, pay_date=pay_date, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,
            exc=exc, actor_user=actor_user, target=run,
        )
        raise


@transaction.atomic
def _pay_payroll_atomic(run, *, bank_account=None, pay_date=None, actor_user=None):
    from .models import JournalEntry, JournalLine

    if run.run_status != PayrollRunStatus.POSTED:
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"it must be posted (accrued) before it can be paid.",
        )
    bank_account = bank_account or run.bank_account
    if bank_account is None:
        raise PayrollError("No bank account set to disburse the payroll from.")
    if run.net_total <= 0:
        raise PayrollError("Nothing to disburse: net total is zero.")

    net = run.net_payable_account or resolve_account(
        run.entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",
    )
    pay_date = pay_date or run.pay_date
    period = resolve_period(run.entity, pay_date)

    entry = JournalEntry.objects.create(
        entity=run.entity, branch=run.branch,
        date=pay_date, period=period, source=JournalSource.BANK,
        currency=run.currency,
        narration=f"Pay net wages {run.period_label or run.document_number or ''}".strip(),
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=net, debit=run.net_total, credit=0,
        description="Net wages payable", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=run.net_total,
        description="Net wages paid", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    run.disbursement_journal = entry
    run.bank_account = bank_account
    run.run_status = PayrollRunStatus.PAID
    run.save(update_fields=[
        "disbursement_journal", "bank_account", "run_status", "updated_at",
    ])

    record(
        entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,
        actor_user=actor_user, target=run,
        message=f"Disbursed net wages {run.net_total} kobo from {bank_account.name}.",
        journal_id=entry.pk, net=run.net_total,
    )
    return run
