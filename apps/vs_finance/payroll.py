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
from __future__ import annotations  # Defer annotation evaluation during app import.

from collections import defaultdict  # Groups payroll gross by cost center.

from django.db import transaction  # Keeps payroll mutations atomic.

from .accounts import resolve_account  # Resolves default payroll control accounts.
from .audit import record, record_rejection  # Finance audit helpers.
from .constants import (
    DocumentStatus,  # Finance document lifecycle statuses.
    FinanceAuditAction,  # Audit action enum values.
    JournalSource,  # Journal source enum values.
    NET_WAGES_PAYABLE_CODE,  # Default net wages payable account code.
    PAYE_PAYABLE_CODE,  # Default PAYE payable account code.
    PENSION_PAYABLE_CODE,  # Default pension payable account code.
    PayrollRunStatus,  # Payroll run lifecycle statuses.
    SALARIES_EXPENSE_CODE,  # Default salaries expense account code.
    SalaryCalcMethod,  # Salary component calculation method enum.
    SalaryComponentKind,  # Salary component earning/deduction enum.
    StatutoryType,  # Statutory deduction type enum.
)
from .exceptions import FinanceError, PayrollError  # Base finance and payroll-domain errors.
from .posting import post_journal, resolve_period  # GL posting and period resolution helpers.


def apply_structure(gross_amount, structure) -> dict:  # Calculate salary breakdown from a structure.
    """Derive an employee's pay breakdown from a salary structure applied to a gross.

    Earnings are an informational split of the gross; deductions tagged PAYE/pension are
    what reduce it to net. Returns integer-kobo ``gross``/``basic``/``paye``/``pension``/
    ``net`` plus a ``components`` snapshot ``[{name, kind, statutory_type, amount}]`` for
    the payslip. ``net = gross - paye - pension`` always, so the accrual journal balances.
    """
    gross = int(gross_amount or 0)  # Normalize gross pay to integer kobo.
    components = list(structure.components.all()) if structure is not None else []  # Snapshot structure components.

    def value_of(component, basic):  # Compute one component amount.
        if component.calc_method == SalaryCalcMethod.FIXED:  # Fixed components ignore gross/basic.
            return int(component.amount or 0)  # Return fixed kobo amount.
        base = basic if component.calc_method == SalaryCalcMethod.PERCENT_OF_BASIC else gross  # Choose percentage base.
        return base * int(component.rate_bps or 0) // 10000  # Apply basis-point rate to base.

    # Basic first — the base for any '% of basic' component (which must not itself be one).  # Required dependency order.
    basic = sum(  # Sum components marked as basic earnings.
        value_of(c, 0) for c in components  # Compute fixed/gross-percent basic components.
        if c.kind == SalaryComponentKind.EARNING and c.is_basic  # Only earning/basic components contribute.
    )

    paye = pension = 0  # Statutory deduction totals.
    snapshot = []  # Payslip component snapshot.
    for c in components:  # Compute every configured component.
        amt = value_of(c, basic)  # Calculate component amount.
        snapshot.append({  # Preserve component details for payslip/history.
            "name": c.name, "kind": c.kind,  # Component name and earning/deduction kind.
            "statutory_type": c.statutory_type, "amount": amt,  # Statutory type and computed amount.
        })
        if c.kind == SalaryComponentKind.DEDUCTION:  # Only deductions reduce net pay.
            if c.statutory_type == StatutoryType.PAYE:  # PAYE deduction.
                paye += amt  # Add to PAYE liability.
            elif c.statutory_type == StatutoryType.PENSION:  # Pension deduction.
                pension += amt  # Add to pension liability.

    return {  # Return payroll line calculation result.
        "gross": gross, "basic": basic, "paye": paye, "pension": pension,  # Gross/basic/statutory totals.
        "net": gross - paye - pension, "components": snapshot,  # Net pay and payslip snapshot.
    }


def compute_payroll(run) -> None:  # Recalculate payroll line net amounts and run totals.
    """Derive each line's ``net_amount`` (gross − paye − pension) and roll up totals."""
    from .models import PayrollLine  # Local import avoids model import cycles.

    for line in run.lines.all():  # Walk every payroll line.
        net = line.gross_amount - line.paye_amount - line.pension_amount  # Derive net pay.
        if line.net_amount != net:  # Avoid unnecessary writes.
            PayrollLine.objects.filter(pk=line.pk).update(net_amount=net)  # Persist recalculated net.
    run.recompute_totals(save=True)  # Roll line totals up to payroll run.


@transaction.atomic
def generate_run_from_roster(entity, *, pay_date, period_label="", narration="",
                             currency=None, actor_user=None):  # Create a draft payroll run from active salaries.
    """Raise a draft :class:`PayrollRun` with one line per active employee salary.

    Copies the recurring gross/PAYE/pension (and cost centre) from the
    :class:`EmployeeSalary` roster. Raises :class:`PayrollError` if the roster is empty.
    """
    from .models import EmployeeSalary, PayrollLine, PayrollRun  # Payroll roster and run models.

    roster = list(  # Load active employee salaries in stable order.
        EmployeeSalary.objects.filter(entity=entity, is_active=True)  # Active salaries for this entity.
        .select_related("cost_center")  # Load cost-center relation.
        .prefetch_related("structure__components")  # Load salary structure components.
        .order_by("name")  # Stable employee order.
    )
    if not roster:  # A run needs at least one active employee.
        raise PayrollError("No active employees on the salary roster to generate a run from.")

    run = PayrollRun.objects.create(  # Create draft payroll run header.
        entity=entity, pay_date=pay_date, period_label=period_label,  # Scope and payroll period label.
        narration=narration, currency=currency, created_by=actor_user,  # Narrative, currency, and actor.
    )
    for i, emp in enumerate(roster, start=1):  # Create one line per roster entry.
        if emp.structure_id:  # Structured salaries derive statutory deductions.
            d = apply_structure(emp.gross_amount, emp.structure)  # Calculate breakdown from structure.
            paye, pension, components = d["paye"], d["pension"], d["components"]  # Extract deductions and snapshot.
        else:  # Legacy/direct salaries store deductions on the roster row.
            paye, pension, components = emp.paye_amount, emp.pension_amount, []  # Use explicit amounts.
        PayrollLine.objects.create(  # Create payroll line.
            run=run, line_no=i, employee=emp.employee, employee_name=emp.name,  # Link employee and preserve name.
            gross_amount=emp.gross_amount, paye_amount=paye,  # Gross and PAYE amounts.
            pension_amount=pension, cost_center=emp.cost_center, components=components,  # Pension, analytics, and snapshot.
        )
    compute_payroll(run)  # Calculate net amounts and totals.
    run.refresh_from_db()  # Reload computed totals.
    return run  # Return draft payroll run.


def _accounts_for(run):  # Resolve payroll accrual accounts.
    """Resolve the four posting accounts for a run, falling back to the seeded defaults."""
    entity = run.entity  # Payroll entity scopes account lookup.
    salary = run.salary_expense_account or resolve_account(  # Salary expense account.
        entity, SALARIES_EXPENSE_CODE, label="salary expense",  # Resolve default salaries expense.
    )
    paye = run.paye_payable_account or resolve_account(  # PAYE liability account.
        entity, PAYE_PAYABLE_CODE, label="PAYE payable",  # Resolve default PAYE payable.
    )
    pension = run.pension_payable_account or resolve_account(  # Pension liability account.
        entity, PENSION_PAYABLE_CODE, label="pension payable",  # Resolve default pension payable.
    )
    net = run.net_payable_account or resolve_account(  # Net wages liability account.
        entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",  # Resolve default net wages payable.
    )
    return salary, paye, pension, net  # Return expense and liability accounts.


def post_payroll(run, *, actor_user=None):  # Public wrapper for payroll accrual posting.
    """Compute, validate and post a payroll run's **accrual** journal.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker performs accrual posting.
        return _post_payroll_atomic(run, actor_user=actor_user)  # Post payroll accrual.
    except FinanceError as exc:  # Failed payroll posts should be auditable.
        record_rejection(  # Record durable rejection.
            entity=run.entity, action=FinanceAuditAction.PAYROLL_POST_REJECTED,  # Rejection audit action.
            exc=exc, actor_user=actor_user, target=run,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _post_payroll_atomic(run, *, actor_user=None):  # Transactional payroll accrual implementation.
    from .models import JournalEntry, JournalLine  # Journal models used for payroll entry.

    if run.run_status != PayrollRunStatus.DRAFT:  # Only draft runs can be accrued.
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"only a draft can be posted.",
        )

    if not run.lines.exists():  # Payroll accrual needs at least one employee line.
        raise PayrollError("A payroll run must have at least one line to post.")

    compute_payroll(run)  # Ensure line net amounts and totals are current.
    if run.gross_total <= 0:  # Payroll should recognize a positive salary cost.
        raise PayrollError("A payroll run must have a positive gross total to post.")
    for line in run.lines.all():  # Validate every employee line.
        if line.net_amount < 0:  # Deductions cannot exceed gross pay.
            raise PayrollError(
                f"Net pay is negative for {line.employee_name or line.employee_id}: "
                f"deductions exceed gross.",
            )

    salary, paye, pension, net = _accounts_for(run)  # Resolve expense and liability accounts.
    period = resolve_period(run.entity, run.pay_date)  # Find payroll period.

    entry = JournalEntry.objects.create(  # Create payroll accrual journal header.
        entity=run.entity, branch=run.branch,  # Scope entity and optional branch.
        date=run.pay_date, period=period, source=JournalSource.PAYROLL,  # Payroll date/period/source.
        currency=run.currency,  # Payroll currency.
        narration=run.narration or f"Payroll {run.period_label or run.document_number or ''}".strip(),  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    # Dr salary expense (gross), split by cost centre so the GL slices by department.
    # Salary is P&L, so it carries the cost centre; the PAYE/pension/net liabilities
    # below are balance-sheet control accounts and stay aggregated. Σ(gross by cost
    # centre) == run.gross_total (both sum the lines' gross_amount), so it stays balanced.  # Preserve department analytics.
    gross_by_cc: dict[int | None, int] = defaultdict(int)  # Gross salary grouped by cost center id.
    cc_objs: dict[int | None, object] = {}  # Cost center objects keyed by id.
    for line in run.lines.select_related("cost_center"):  # Walk payroll lines with analytics loaded.
        gross_by_cc[line.cost_center_id] += line.gross_amount  # Accumulate gross by cost center.
        cc_objs[line.cost_center_id] = line.cost_center  # Keep object for journal line.

    line_no = 0  # Journal line counter.
    for cc_id, amount in gross_by_cc.items():  # Emit salary expense debit lines.
        if amount == 0:  # Skip empty groups.
            continue
        line_no += 1  # Advance line number.
        JournalLine.objects.create(  # Debit salary expense.
            entry=entry, account=salary, debit=amount, credit=0,  # Dr salary expense.
            description="Gross salaries", cost_center=cc_objs[cc_id], line_no=line_no,  # Preserve cost center.
        )
    for account, amount, label in (  # Emit liability credit lines.
        (paye, run.paye_total, "PAYE payable"),  # PAYE liability.
        (pension, run.pension_total, "Pension payable"),  # Pension liability.
        (net, run.net_total, "Net wages payable"),  # Net wages liability.
    ):
        if amount <= 0:  # Skip zero liability buckets.
            continue
        line_no += 1  # Advance line number.
        JournalLine.objects.create(  # Credit payroll liability account.
            entry=entry, account=account, debit=0, credit=amount,  # Cr liability.
            description=label, line_no=line_no,  # Label and line order.
        )

    post_journal(entry, actor_user=actor_user)  # Validate and post accrual journal.

    run.journal = entry  # Link run to accrual journal.
    run.salary_expense_account = salary  # Persist salary expense account used.
    run.paye_payable_account = paye  # Persist PAYE payable account used.
    run.pension_payable_account = pension  # Persist pension payable account used.
    run.net_payable_account = net  # Persist net wages payable account used.
    run.run_status = PayrollRunStatus.POSTED  # Mark payroll accrued.
    run.status = DocumentStatus.POSTED  # Mark finance document posted.
    run.save(update_fields=[  # Persist accrual fields.
        "journal", "salary_expense_account", "paye_payable_account",  # Journal and PAYE/salary accounts.
        "pension_payable_account", "net_payable_account",  # Pension and net payable accounts.
        "run_status", "status", "updated_at",  # Lifecycle fields.
    ])

    record(  # Audit successful payroll accrual.
        entity=run.entity, action=FinanceAuditAction.PAYROLL_POSTED,  # Audit action.
        actor_user=actor_user, target=run,  # Actor and target context.
        message=f"Accrued payroll: gross {run.gross_total}, net {run.net_total} kobo.",  # Summary.
        journal_id=entry.pk, gross=run.gross_total, paye=run.paye_total,  # Journal and gross/PAYE metadata.
        pension=run.pension_total, net=run.net_total,  # Pension and net metadata.
    )
    return run  # Return posted payroll run.


def pay_payroll(run, *, bank_account=None, pay_date=None, actor_user=None):  # Public wrapper for net wage disbursement.
    """Disburse a posted run's net pay: ``Dr net wages payable, Cr bank``."""
    try:  # Atomic worker performs disbursement posting.
        return _pay_payroll_atomic(  # Pay net wages.
            run, bank_account=bank_account, pay_date=pay_date, actor_user=actor_user,  # Bank/date/actor.
        )
    except FinanceError as exc:  # Failed disbursements should be auditable.
        record_rejection(  # Record durable rejection.
            entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,  # Existing disbursement audit action.
            exc=exc, actor_user=actor_user, target=run,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _pay_payroll_atomic(run, *, bank_account=None, pay_date=None, actor_user=None):  # Transactional payroll payment.
    from .models import JournalEntry, JournalLine  # Journal models used for disbursement entry.

    if run.run_status != PayrollRunStatus.POSTED:  # Only accrued payroll can be paid.
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"it must be posted (accrued) before it can be paid.",
        )
    bank_account = bank_account or run.bank_account  # Use explicit bank or stored bank.
    if bank_account is None:  # Disbursement needs a bank account.
        raise PayrollError("No bank account set to disburse the payroll from.")
    if run.net_total <= 0:  # Nothing leaves bank when net total is zero.
        raise PayrollError("Nothing to disburse: net total is zero.")

    net = run.net_payable_account or resolve_account(  # Resolve net wages liability account.
        run.entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",  # Default account code.
    )
    pay_date = pay_date or run.pay_date  # Default disbursement date to payroll date.
    period = resolve_period(run.entity, pay_date)  # Find payment period.

    entry = JournalEntry.objects.create(  # Create disbursement journal header.
        entity=run.entity, branch=run.branch,  # Scope entity and optional branch.
        date=pay_date, period=period, source=JournalSource.BANK,  # Bank-source payment entry.
        currency=run.currency,  # Payroll currency.
        narration=f"Pay net wages {run.period_label or run.document_number or ''}".strip(),  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(  # Debit net wages liability.
        entry=entry, account=net, debit=run.net_total, credit=0,  # Dr net wages payable.
        description="Net wages payable", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(  # Credit bank for the cash paid.
        entry=entry, account=bank_account.gl_account, debit=0, credit=run.net_total,  # Cr bank.
        description="Net wages paid", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post disbursement journal.

    run.disbursement_journal = entry  # Link run to disbursement journal.
    run.bank_account = bank_account  # Persist bank account used.
    run.run_status = PayrollRunStatus.PAID  # Mark payroll paid.
    run.save(update_fields=[  # Persist disbursement fields.
        "disbursement_journal", "bank_account", "run_status", "updated_at",  # Journal, bank, status.
    ])

    record(  # Audit successful disbursement.
        entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,  # Audit action.
        actor_user=actor_user, target=run,  # Actor and target context.
        message=f"Disbursed net wages {run.net_total} kobo from {bank_account.name}.",  # Summary.
        journal_id=entry.pk, net=run.net_total,  # Structured metadata.
    )
    return run  # Return paid payroll run.


@transaction.atomic
def cancel_payroll_run(run, *, actor_user=None):  # Cancel or void a payroll run.
    """Cancel / void a payroll run raised in error, by its state:

    * **DRAFT** — nothing posted, just mark it CANCELLED.
    * **POSTED** (accrued, not yet paid) — reverse the accrual journal (an audit-correct
      mirror that backs out the salary expense and the PAYE/pension/net liabilities) and
      mark it CANCELLED.
    * **PAID** — refused: the net wages have already left the bank, so the disbursement
      must be reversed first (a real cash clawback), before the run can be voided.

    Idempotent on an already-cancelled run.
    """
    from .posting import reverse_journal  # Local import avoids circular service import.

    if run.run_status == PayrollRunStatus.CANCELLED:  # Cancellation is idempotent.
        return run
    if run.run_status == PayrollRunStatus.PAID:  # Paid payroll cannot be voided without cash reversal.
        raise PayrollError(
            "This run has been paid — the net wages already left the bank. Reverse the "
            "disbursement before voiding the run.",
        )

    if run.run_status == PayrollRunStatus.POSTED and run.journal_id is not None:  # Accrued unpaid payroll needs reversal.
        reverse_journal(run.journal, actor_user=actor_user)  # Reverse accrual journal.

    was = run.run_status  # Capture previous status for audit message.
    run.run_status = PayrollRunStatus.CANCELLED  # Mark payroll run cancelled.
    run.status = DocumentStatus.CANCELLED  # Mark finance document cancelled.
    run.save(update_fields=["run_status", "status", "updated_at"])  # Persist cancellation fields.

    record(  # Audit cancellation/void.
        entity=run.entity, action=FinanceAuditAction.PAYROLL_CANCELLED,  # Audit action.
        actor_user=actor_user, target=run,  # Actor and target context.
        message=(f"Voided payroll run {run.document_number or run.pk} "  # Posted runs are voided with reversal.
                 f"(reversed accrual journal {run.journal_id})."
                 if was == PayrollRunStatus.POSTED  # Distinguish posted vs draft path.
                 else f"Cancelled draft payroll run {run.document_number or run.pk}."),  # Draft path message.
        journal_id=run.journal_id, previous_status=was,  # Structured metadata.
    )
    return run  # Return cancelled payroll run.
