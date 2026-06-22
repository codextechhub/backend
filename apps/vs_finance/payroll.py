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
    SalaryCalcMethod,
    SalaryComponentKind,
    StatutoryType,
)
from .exceptions import FinanceError, PayrollError
from .posting import post_journal, resolve_period


def apply_structure(gross_amount, structure) -> dict:
    """Derive an employee's pay breakdown from a salary structure applied to a gross.

    Earnings are an informational split of the gross; deductions tagged PAYE/pension are
    what reduce it to net. Returns integer-kobo ``gross``/``basic``/``paye``/``pension``/
    ``net`` plus a ``components`` snapshot ``[{name, kind, statutory_type, amount}]`` for
    the payslip. ``net = gross - paye - pension`` always, so the accrual journal balances.
    """
    gross = int(gross_amount or 0)
    components = list(structure.components.all()) if structure is not None else []

    def value_of(component, basic):
        if component.calc_method == SalaryCalcMethod.FIXED:
            return int(component.amount or 0)
        base = basic if component.calc_method == SalaryCalcMethod.PERCENT_OF_BASIC else gross
        return base * int(component.rate_bps or 0) // 10000

    # Basic first — the base for any '% of basic' component (which must not itself be one).
    basic = sum(
        value_of(c, 0) for c in components
        if c.kind == SalaryComponentKind.EARNING and c.is_basic
    )

    paye = pension = 0
    snapshot = []
    for c in components:
        amt = value_of(c, basic)
        snapshot.append({
            "name": c.name, "kind": c.kind,
            "statutory_type": c.statutory_type, "amount": amt,
        })
        if c.kind == SalaryComponentKind.DEDUCTION:
            if c.statutory_type == StatutoryType.PAYE:
                paye += amt
            elif c.statutory_type == StatutoryType.PENSION:
                pension += amt

    return {
        "gross": gross, "basic": basic, "paye": paye, "pension": pension,
        "net": gross - paye - pension, "components": snapshot,
    }


def compute_payroll(run) -> None:
    """Derive each line's ``net_amount`` (gross − paye − pension) and roll up totals."""
    from .models import PayrollLine

    for line in run.lines.all():
        net = line.gross_amount - line.paye_amount - line.pension_amount
        if line.net_amount != net:
            PayrollLine.objects.filter(pk=line.pk).update(net_amount=net)
    run.recompute_totals(save=True)


@transaction.atomic
def generate_run_from_roster(entity, *, pay_date, period_label="", narration="",
                             currency=None, actor_user=None):
    """Raise a draft :class:`PayrollRun` with one line per active employee salary.

    Copies the recurring gross/PAYE/pension (and cost centre) from the
    :class:`EmployeeSalary` roster. Raises :class:`PayrollError` if the roster is empty.
    """
    from .models import EmployeeSalary, PayrollLine, PayrollRun

    roster = list(
        EmployeeSalary.objects.filter(entity=entity, is_active=True)
        .select_related("cost_center")
        .prefetch_related("structure__components")
        .order_by("name")
    )
    if not roster:
        raise PayrollError("No active employees on the salary roster to generate a run from.")

    run = PayrollRun.objects.create(
        entity=entity, pay_date=pay_date, period_label=period_label,
        narration=narration, currency=currency, created_by=actor_user,
    )
    for i, emp in enumerate(roster, start=1):
        if emp.structure_id:
            d = apply_structure(emp.gross_amount, emp.structure)
            paye, pension, components = d["paye"], d["pension"], d["components"]
        else:
            paye, pension, components = emp.paye_amount, emp.pension_amount, []
        PayrollLine.objects.create(
            run=run, line_no=i, employee=emp.employee, employee_name=emp.name,
            gross_amount=emp.gross_amount, paye_amount=paye,
            pension_amount=pension, cost_center=emp.cost_center, components=components,
        )
    compute_payroll(run)
    run.refresh_from_db()
    return run


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
