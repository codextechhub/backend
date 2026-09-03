"""Payroll services - the two-step accrue-then-disburse payroll cycle.

Payroll is booked in two postings, deliberately separate because the cost is incurred
before the cash leaves (and the statutory deductions are held in between):

* **Accrual** (:func:`post_payroll`): recognise the whole cost and park each liability -
  ``Dr salary expense (Σgross), Cr PAYE payable (Σpaye), Cr pension payable (Σpension),
  Cr net wages payable (Σnet)``.
* **Disbursement** (:func:`pay_payroll`): when employees are actually paid, clear the
  net-pay liability - ``Dr net wages payable (Σnet), Cr bank (Σnet)``.

The statutory liabilities (PAYE, pension) stay on the balance sheet until remitted to
the authorities - a separate AP payment outside this module. ``net = gross - paye -
pension`` per employee; all amounts are integer kobo.
"""
from __future__ import annotations

from collections import defaultdict

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


# Calculate salary breakdown from a structure.
def apply_structure(gross_amount, structure) -> dict:
    """Derive an employee's pay breakdown from a salary structure applied to a gross.

    Earnings are an informational split of the gross; deductions tagged PAYE/pension are
    what reduce it to net. Returns integer-kobo ``gross``/``basic``/``paye``/``pension``/
    ``net`` plus a ``components`` snapshot ``[{name, kind, statutory_type, amount}]`` for
    the payslip. ``net = gross - paye - pension`` always, so the accrual journal balances.
    """
    gross = int(gross_amount or 0)  # Normalize gross pay to integer kobo.
    components = list(structure.components.all()) if structure is not None else []  # Snapshot structure components.

    # Compute one component amount.
    def value_of(component, basic):
        if component.calc_method == SalaryCalcMethod.FIXED:  # Fixed components ignore gross/basic.
            return int(component.amount or 0)  # Return fixed kobo amount.
        base = basic if component.calc_method == SalaryCalcMethod.PERCENT_OF_BASIC else gross  # Choose percentage base.
        return base * int(component.rate_bps or 0) // 10000  # Apply basis-point rate to base.

    # Basic first - the base for any '% of basic' component (which must not itself be one).  # Required dependency order.
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


# Recalculate payroll line net amounts and run totals.
def compute_payroll(run) -> None:
    """Derive each line's ``net_amount`` (gross − paye − pension) and roll up totals."""
    from .models import PayrollLine

    for line in run.lines.all():  # Walk every payroll line.
        net = line.gross_amount - line.paye_amount - line.pension_amount  # Derive net pay.
        if line.net_amount != net:  # Avoid unnecessary writes.
            PayrollLine.objects.filter(pk=line.pk).update(net_amount=net)
    run.recompute_totals(save=True)  # Roll line totals up to payroll run.


# --------------------------------------------------------------------------- #
# Central or per branch: the school's choice, and the rule that follows        #
# --------------------------------------------------------------------------- #
#
# A school runs payroll one of two ways, and it says which through one setting:
#
#   * **CENTRAL** (the default, and what every school does today) - one run covers
#     everybody the entity employs, and branch plays no part in choosing who is on
#     it.
#   * **PER_BRANCH** - the school has a payroll officer per site, and each of them
#     raises a run covering exactly her own branch's staff.
#
# The setting is not decoration. Under CENTRAL every path below is the path that
# existed before branch payroll was built, down to the SQL: the roster carries no
# branches and nothing consults them. Only a school that deliberately switches
# sees any of the new behaviour, and it cannot switch until every active person on
# its roster has a branch (:func:`assert_roster_fully_assigned`).
#
# Head office is a branch in this product - the main branch, which every school is
# required to have - so under PER_BRANCH nobody belongs to "no branch", and running
# every branch covers the whole school by construction. A salary row with no branch
# is therefore not a school-wide person; it is an unassigned row, a data gap rather
# than a meaning, which is exactly why switching is refused while one exists.

#: The setting that decides which of the two shapes a school runs.
PAYROLL_SCOPE_KEY = "payroll.scope"
PAYROLL_SCOPE_CENTRAL = "CENTRAL"
PAYROLL_SCOPE_PER_BRANCH = "PER_BRANCH"
PAYROLL_SCOPE_CHOICES = (PAYROLL_SCOPE_CENTRAL, PAYROLL_SCOPE_PER_BRANCH)


def payroll_scope(entity) -> str:
    """Which shape *entity*'s owning school runs payroll in.

    Falls back to :data:`PAYROLL_SCOPE_CENTRAL` for anything unexpected: an entity
    with no tenant (the platform's own books), an archived definition, a value that
    is not one of the two. Failing to the old shape is the safe direction. The worst
    case that way is a school which opted in keeps running centrally until somebody
    notices; failing the other way would narrow a central school's payroll to one
    branch and quietly stop paying everybody else.
    """
    tenant = getattr(entity, "tenant", None)
    if tenant is None:
        return PAYROLL_SCOPE_CENTRAL
    from vs_config.conf import get_config

    value = get_config(PAYROLL_SCOPE_KEY, PAYROLL_SCOPE_CENTRAL, tenant=tenant)
    return value if value in PAYROLL_SCOPE_CHOICES else PAYROLL_SCOPE_CENTRAL


def is_per_branch(entity) -> bool:
    """Whether *entity*'s school has opted into per-branch payroll."""
    return payroll_scope(entity) == PAYROLL_SCOPE_PER_BRANCH


def unassigned_roster(tenant):
    """Active salary rows across *tenant*'s books that carry no branch.

    The rows that make per-branch payroll impossible, because no branch run would
    ever reach them. Tenant-wide rather than per-entity: the setting is the
    school's, so the question it has to answer is the school's too, and a group
    keeping two sets of books must not be able to switch on the strength of one.
    """
    from .models import EmployeeSalary

    return EmployeeSalary.objects.filter(
        entity__tenant=tenant, is_active=True, branch__isnull=True,
    ).order_by("name")


def assert_roster_fully_assigned(tenant) -> None:
    """Refuse the switch to PER_BRANCH while anybody active has no branch.

    This is the whole guard against the gap per-branch payroll could otherwise
    open, and it runs at the one moment somebody is paying attention. Corona has
    109 active staff; 105 carry Ikeja, Lekki or Yaba, and four - the principal, the
    group accountant and two drivers - were never assigned. Flip the school over
    anyway and the three branch runs pay 105 people, no run reaches the other four,
    and the first anybody hears of it is four people asking where January went.
    Refusing here costs the bursar four edits before she flips the switch, and
    makes that outcome unreachable rather than merely unlikely.

    Named rather than counted, and capped so the message stays a message: "4 staff
    are unassigned" sends her hunting through a roster of 109.
    """
    from vs_config.exceptions import InvalidConfigurationValue

    rows = list(unassigned_roster(tenant)[:11])
    if not rows:
        return
    shown = ", ".join(row.name for row in rows[:10])
    more = " and others" if len(rows) > 10 else ""
    raise InvalidConfigurationValue(
        f"Per-branch payroll needs every active employee on a branch, and these are "
        f"not on one yet: {shown}{more}. Assign them a branch, then switch.",
        extra={"key": PAYROLL_SCOPE_KEY},
    )


def guard_payroll_scope(value, *, tenant=None, branch=None) -> None:
    """The :mod:`vs_config` write guard behind :data:`PAYROLL_SCOPE_KEY`.

    Registered from this app's ``AppConfig.ready`` rather than called from
    :mod:`vs_config`, so the configuration engine keeps knowing nothing about
    finance. Only the switch *into* PER_BRANCH is guarded: switching back to
    CENTRAL is always safe, because a central run covers everybody whatever their
    branch says.
    """
    if value != PAYROLL_SCOPE_PER_BRANCH or tenant is None:
        return
    assert_roster_fully_assigned(tenant)


def roster_for(entity, branch=None):
    """The active salary rows a run for *branch* covers, as a queryset.

    ``branch=None`` covers the whole entity - every active row, branched or not.
    That is the central run, and what a CENTRAL school does.

    A *branch* is read **exclusively**: exactly the rows carrying that branch, and
    deliberately **not** the rows carrying none. This is the second place on the
    platform to refuse the inclusive reading (:mod:`vs_procurement` is the first,
    for a different reason), and the argument is arithmetic rather than taste.

    Suppose an unassigned row were included, the way every other finance screen
    includes a null branch. Corona has Ikeja, Lekki and Yaba, and one row nobody
    has assigned yet. January comes, each officer runs her own branch, and that one
    person is on all three runs: the accrual books the salary three times and the
    bank sends it three times. A row read inclusively is *seen* three times, which
    is harmless and often helpful; a row read inclusively is *paid* three times,
    which is neither. Paying is not reading, so payroll parts company with the
    platform default here and only here.

    Nobody is stranded by that choice, because a school cannot reach PER_BRANCH
    with an unassigned row in the first place - see
    :func:`assert_roster_fully_assigned`.
    """
    from .models import EmployeeSalary

    qs = EmployeeSalary.objects.filter(entity=entity, is_active=True)
    if branch is not None:
        # ``branch_id`` rather than ``branch``: the caller may hold either, and
        # this avoids a pointless fetch when it holds a bare id.
        qs = qs.filter(branch_id=getattr(branch, "pk", branch))
    return qs


def _period_window(entity, pay_date):
    """The date range that counts as "the same payroll period" as *pay_date*.

    ``period_label`` is free text and is deliberately **not** part of this. Two
    officers typing "Jan 2026" and "January 2026" mean the same month, and a key
    that lets those two through is a key that lets somebody be paid twice, which is
    the single thing this guard exists to prevent. ``pay_date`` on its own is just
    as weak in the other direction: a central run dated the 25th and a branch run
    dated the 31st are the same month's payroll and must still collide.

    So the period is the entity's own :class:`FiscalPeriod` containing the date -
    the same period the run's accrual will post into, and the closest thing the
    books have to an official payroll month. A date no period covers (a draft
    raised before the year is opened) falls back to the calendar month, so the
    guard never quietly stops guarding.
    """
    from django.db.models import Q

    period = resolve_period(entity, pay_date)
    if period is not None:
        return Q(pay_date__gte=period.start_date, pay_date__lte=period.end_date)
    return Q(pay_date__year=pay_date.year, pay_date__month=pay_date.month)


def ensure_no_overlapping_run(entity, pay_date, branch=None) -> None:
    """Refuse a run that would pay somebody a second time in the same period.

    **Only under PER_BRANCH.** A CENTRAL school is not guarded at all, and that is
    deliberate rather than an oversight: raising two runs in one month has always
    been allowed there, schools do it for advances and supplementary payments, and
    this change promised to leave a central school's payroll exactly as it found
    it. The double payment per-branch payroll could introduce is the branch/central
    one, and that only exists once a school has switched.

    Under PER_BRANCH coverage nests, and the rule falls out of it: a central run
    covers everybody, a branch run covers its own site, and two different branches
    never share a person. So two live runs may share a period only when both are
    branch-scoped and to *different* branches. Everything else - branch against the
    same branch, branch against central, central against central - is refused.

    Cancelled runs do not count: voiding a run is precisely how a school corrects
    the one it raised in error before raising the right one.
    """
    from django.db.models import Q

    from .models import PayrollRun

    if not is_per_branch(entity):
        return

    live = PayrollRun.objects.filter(
        _period_window(entity, pay_date), entity=entity,
    ).exclude(run_status=PayrollRunStatus.CANCELLED)
    if branch is None:
        clash = live.select_related("branch").first()  # central meets everybody
    else:
        branch_id = getattr(branch, "pk", branch)
        clash = (
            live.filter(Q(branch__isnull=True) | Q(branch_id=branch_id))
            .select_related("branch")
            .first()
        )
    if clash is None:
        return
    whose = clash.branch.name if clash.branch_id else "the whole school"
    raise PayrollError(
        f"A payroll run for {whose} ({clash.document_number or clash.pk}) already "
        f"covers this period. Void it before raising another, or these staff are "
        f"paid twice.",
    )


@transaction.atomic
def generate_run_from_roster(entity, *, pay_date, branch=None, period_label="",
                             narration="", currency=None,
                             actor_user=None):  # Create a draft payroll run from active salaries.
    """Raise a draft :class:`PayrollRun` with one line per active employee salary.

    Copies the recurring gross/PAYE/pension (and cost centre) from the
    :class:`EmployeeSalary` roster. Raises :class:`PayrollError` if the roster is empty.

    ``branch`` picks which of the two shapes this is. Left out - the default, and
    what every existing caller does - it is a central run over the whole entity and
    behaves exactly as before, right down to the query. Given a branch it covers
    only that branch's roster rows; :func:`roster_for` argues why that reading is
    exclusive. Deciding *whether* to pass one is the caller's job, because that is
    the school's setting rather than this function's business.
    """
    from .models import PayrollLine, PayrollRun

    ensure_no_overlapping_run(entity, pay_date, branch)

    roster = list(  # Load active employee salaries in stable order.
        roster_for(entity, branch)
        .select_related("cost_center")
        .prefetch_related("structure__components")
        .order_by("name")
    )
    if not roster:  # A run needs at least one active employee.
        raise PayrollError(
            f"No active employees on the {branch.name} salary roster to generate a "
            f"run from."
            if branch is not None else
            "No active employees on the salary roster to generate a run from.",
        )

    run = PayrollRun.objects.create(
        entity=entity, branch=branch, pay_date=pay_date, period_label=period_label,  # Scope, branch, period label.
        narration=narration, currency=currency, created_by=actor_user,  # Narrative, currency, and actor.
    )
    for i, emp in enumerate(roster, start=1):  # Create one line per roster entry.
        if emp.structure_id:  # Structured salaries derive statutory deductions.
            d = apply_structure(emp.gross_amount, emp.structure)  # Calculate breakdown from structure.
            paye, pension, components = d["paye"], d["pension"], d["components"]  # Extract deductions and snapshot.
        else:  # Legacy/direct salaries store deductions on the roster row.
            paye, pension, components = emp.paye_amount, emp.pension_amount, []  # Use explicit amounts.
        PayrollLine.objects.create(
            run=run, line_no=i, employee=emp.employee, employee_name=emp.name,  # Link employee and preserve name.
            gross_amount=emp.gross_amount, paye_amount=paye,  # Gross and PAYE amounts.
            pension_amount=pension, cost_center=emp.cost_center, components=components,  # Pension, analytics, and snapshot.
        )
    compute_payroll(run)  # Calculate net amounts and totals.
    run.refresh_from_db()
    return run  # Return draft payroll run.


# Resolve payroll accrual accounts.
def _accounts_for(run):
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


# Public wrapper for payroll accrual posting.
def post_payroll(run, *, actor_user=None):
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
        raise


@transaction.atomic
# Transactional payroll accrual implementation.
def _post_payroll_atomic(run, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if run.run_status != PayrollRunStatus.DRAFT:  # Only draft runs can be accrued.
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"only a draft can be posted.",
        )

    if not run.lines.exists():
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

    entry = JournalEntry.objects.create(
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
    for line in run.lines.select_related("cost_center"):
        gross_by_cc[line.cost_center_id] += line.gross_amount  # Accumulate gross by cost center.
        cc_objs[line.cost_center_id] = line.cost_center  # Keep object for journal line.

    line_no = 0  # Journal line counter.
    for cc_id, amount in gross_by_cc.items():  # Emit salary expense debit lines.
        if amount == 0:  # Skip empty groups.
            continue
        line_no += 1  # Advance line number.
        JournalLine.objects.create(
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
        JournalLine.objects.create(
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
    run.save(update_fields=[
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


# Public wrapper for net wage disbursement.
def pay_payroll(run, *, bank_account=None, pay_date=None, actor_user=None):
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
        raise


@transaction.atomic
# Transactional payroll payment.
def _pay_payroll_atomic(run, *, bank_account=None, pay_date=None, actor_user=None):
    from .models import JournalEntry, JournalLine

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

    pay_date = pay_date or run.pay_date  # Default disbursement date to payroll date.
    # Net wages cannot be disbursed before the payroll accrual that raised the
    # payable, or the liability carries a debit balance until the run date.
    from .chronology import ensure_on_or_after
    ensure_on_or_after(
        subject=f"Payroll payment for {run.document_number or run.pk}",
        subject_date=pay_date,
        source=f"payroll run {run.document_number or run.pk}",
        source_date=run.pay_date,
        remedy=f"Date the payroll payment {run.pay_date} or later.",
    )

    net = run.net_payable_account or resolve_account(  # Resolve net wages liability account.
        run.entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",  # Default account code.
    )
    period = resolve_period(run.entity, pay_date)  # Find payment period.

    entry = JournalEntry.objects.create(
        entity=run.entity, branch=run.branch,  # Scope entity and optional branch.
        date=pay_date, period=period, source=JournalSource.BANK,  # Bank-source payment entry.
        currency=run.currency,  # Payroll currency.
        narration=f"Pay net wages {run.period_label or run.document_number or ''}".strip(),  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(
        entry=entry, account=net, debit=run.net_total, credit=0,  # Dr net wages payable.
        description="Net wages payable", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=run.net_total,  # Cr bank.
        description="Net wages paid", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post disbursement journal.

    run.disbursement_journal = entry  # Link run to disbursement journal.
    run.bank_account = bank_account  # Persist bank account used.
    run.run_status = PayrollRunStatus.PAID  # Mark payroll paid.
    run.save(update_fields=[
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
# Cancel or void a payroll run.
def cancel_payroll_run(run, *, actor_user=None):
    """Cancel / void a payroll run raised in error, by its state:

    * **DRAFT** - nothing posted, just mark it CANCELLED.
    * **POSTED** (accrued, not yet paid) - reverse the accrual journal (an audit-correct
      mirror that backs out the salary expense and the PAYE/pension/net liabilities) and
      mark it CANCELLED.
    * **PAID** - refused: the net wages have already left the bank, so the disbursement
      must be reversed first (a real cash clawback), before the run can be voided.

    Idempotent on an already-cancelled run.
    """
    from .posting import reverse_journal

    if run.run_status == PayrollRunStatus.CANCELLED:  # Cancellation is idempotent.
        return run
    if run.run_status == PayrollRunStatus.PAID:  # Paid payroll cannot be voided without cash reversal.
        raise PayrollError(
            "This run has been paid - the net wages already left the bank. Reverse the "
            "disbursement before voiding the run.",
        )

    if run.run_status == PayrollRunStatus.POSTED and run.journal_id is not None:  # Accrued unpaid payroll needs reversal.
        reverse_journal(run.journal, actor_user=actor_user, document_owner=run)  # Reverse accrual journal.

    was = run.run_status  # Capture previous status for audit message.
    run.run_status = PayrollRunStatus.CANCELLED  # Mark payroll run cancelled.
    run.status = DocumentStatus.CANCELLED  # Mark finance document cancelled.
    run.save(update_fields=["run_status", "status", "updated_at"])

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
