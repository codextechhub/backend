"""Payroll services - the two-step accrue-then-disburse payroll cycle.

Payroll is booked in two postings, deliberately separate because the cost is incurred
before the cash leaves (and the statutory deductions are held in between):

* **Accrual** (:func:`post_payroll`): recognise the whole cost and park each liability -
  ``Dr salary expense (Σgross), Dr employer contribution expenses, Cr each deduction's
  and contribution's payable, Cr net wages payable (Σnet)``.
* **Disbursement** (:func:`pay_payroll`): when employees are actually paid, clear the
  net-pay liability - ``Dr net wages payable (Σnet), Cr bank (Σnet)`` - and issue
  each person's payslip.

The statutory liabilities (PAYE per state, pension per PFA, NHF, NSITF, ITF) stay on
the balance sheet until remitted through the tax returns (:mod:`vs_finance.tax_filing`).
``net = gross - paye - pension - other deductions`` per employee; all amounts are
integer kobo. What each person's line deducts and contributes is worked out in
:mod:`vs_finance.payroll_statutory`, and PAYE in :mod:`vs_finance.payroll_tax`.
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from vs_config.display import format_date

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    NET_WAGES_PAYABLE_CODE,
    PAYE_PAYABLE_CODE,
    PENSION_PAYABLE_CODE,
    PayeMethod,
    PayrollItemCode,
    PayrollItemKind,
    PayrollRunStatus,
    SALARIES_EXPENSE_CODE,
    SalaryCalcMethod,
    SalaryComponentKind,
    StatutoryType,
)
from .exceptions import FinanceError, PayrollBranchUnassignedError, PayrollError
from .payroll_statutory import ItemAccounts
from .posting import post_journal, resolve_period


# Calculate salary breakdown from a structure.
def apply_structure(gross_amount, structure, as_at=None) -> dict:
    """Split a gross over a salary structure's lines in force on ``as_at``.

    ``as_at`` None reads the structure's current lines; a payroll month passes its
    payroll date, so it is always split by the lines that were in force then.

    Returns integer-kobo ``gross``, ``basic`` (earnings flagged basic),
    ``pensionable`` (earnings flagged pensionable, or the whole gross when none
    is), ``taxable`` (gross less the earnings flagged not taxable), the
    structure's own ``paye`` and ``pension`` deductions, ``net = gross - paye -
    pension``, and a ``components`` snapshot ``[{name, kind, statutory_type,
    amount}]`` for the payslip. With no structure the whole gross is basic,
    pensionable and taxable and nothing is deducted.
    """
    gross = int(gross_amount or 0)
    components = structure.components_on(as_at) if structure is not None else []

    def value_of(component, basic):
        if component.calc_method == SalaryCalcMethod.FIXED:
            return int(component.amount or 0)
        base = basic if component.calc_method == SalaryCalcMethod.PERCENT_OF_BASIC else gross
        return base * int(component.rate_bps or 0) // 10000

    # Basic first: it is the base of any '% of basic' line, and is never one itself.
    basic = sum(
        value_of(c, 0) for c in components
        if c.kind == SalaryComponentKind.EARNING and c.is_basic
    )

    paye = pension = pensionable = untaxed = 0
    snapshot = []
    for c in components:
        amt = value_of(c, basic)
        snapshot.append({
            "name": c.name, "kind": c.kind,
            "statutory_type": c.statutory_type, "amount": amt,
        })
        if c.kind == SalaryComponentKind.EARNING:
            if c.is_pensionable:
                pensionable += amt
            if not c.is_taxable:
                untaxed += amt
        elif c.statutory_type == StatutoryType.PAYE:
            paye += amt
        elif c.statutory_type == StatutoryType.PENSION:
            pension += amt

    return {
        "gross": gross, "basic": basic if structure is not None else gross,
        "pensionable": pensionable or gross, "taxable": max(gross - untaxed, 0),
        "paye": paye, "pension": pension,
        "net": gross - paye - pension, "components": snapshot,
    }


# Recalculate payroll line net amounts and run totals.
def compute_payroll(run) -> None:
    """Derive each line's ``net_amount`` and roll the totals up onto the run.

    ``net = gross - paye - pension - other deductions``; employer contributions
    are a cost on top and never reduce it.
    """
    from .models import PayrollLine

    for line in run.lines.all():
        net = (
            line.gross_amount - line.paye_amount - line.pension_amount
            - line.other_deductions_amount
        )
        if line.net_amount != net:
            PayrollLine.objects.filter(pk=line.pk).update(net_amount=net)
    run.recompute_totals(save=True)


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

    Falls back to :data:`PAYROLL_SCOPE_CENTRAL` for anything unexpected: an
    archived definition, or a value that is not one of the two. Every set of
    books has a tenant, and every tenant (the platform's included) has at least
    one branch. Failing to the old shape is the safe direction. The worst
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


def assert_no_run_this_month(tenant) -> None:
    """Refuse a change of payroll scope once any run of the current payroll month exists.

    Greenfield runs per branch, and Yaba paid October's salaries on the 8th. If
    the head office switches to central on the 10th, the central run on the 25th
    covers everybody, Yaba's 22 staff included. The person guard in
    :func:`generate_run_from_roster` would leave them off it, but the month
    would then be paid half one way and half the other, which nobody can read
    back. So the scope changes only at the start of a payroll month, before any
    run of it is raised (a cancelled run does not count).
    """
    from vs_config.clock import tenant_today
    from vs_config.exceptions import InvalidConfigurationValue

    from .models import LedgerEntity, PayrollRun
    from .payroll_statutory import payroll_period

    today = tenant_today(tenant)
    for entity in LedgerEntity.objects.filter(tenant=tenant):
        start, end = payroll_period(entity, today)
        clash = (
            PayrollRun.objects.filter(entity=entity, pay_date__gte=start, pay_date__lte=end)
            .exclude(run_status=PayrollRunStatus.CANCELLED)
            .select_related("branch").order_by("pay_date", "id").first()
        )
        if clash is None:
            continue
        whose = clash.branch.name if clash.branch_id else "the whole school"
        raise InvalidConfigurationValue(
            f"Payroll run {clash.document_number or clash.pk} for {whose} already covers "
            f"this payroll month. Change how payroll is run at the start of a month, "
            f"before any run of it is raised, or void that run first.",
            extra={"key": PAYROLL_SCOPE_KEY},
        )


def guard_payroll_scope(value, *, tenant=None, branch=None) -> None:
    """The :mod:`vs_config` write guard behind :data:`PAYROLL_SCOPE_KEY`.

    Registered from this app's ``AppConfig.ready`` rather than called from
    :mod:`vs_config`, so the configuration engine keeps knowing nothing about
    finance. A change either way waits for a payroll month no run has started
    (:func:`assert_no_run_this_month`), and the switch into PER_BRANCH also needs
    every active person on a branch (:func:`assert_roster_fully_assigned`).
    Switching back to CENTRAL needs nothing more: a central run covers everybody
    whatever their branch says.
    """
    if tenant is None:
        return
    from vs_config.conf import get_config

    if value == get_config(PAYROLL_SCOPE_KEY, PAYROLL_SCOPE_CENTRAL, tenant=tenant):
        return
    assert_no_run_this_month(tenant)
    if value == PAYROLL_SCOPE_PER_BRANCH:
        assert_roster_fully_assigned(tenant)


def roster_for(entity, branch=None, *, on=None):
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

    A row is the branch's when the branch owns it on ``on`` (the tenant's today
    when left out), by the terms in force that day
    (:meth:`~vs_finance.models.EmployeeSalaryQuerySet.with_branch_on`): a move
    dated from April leaves the person on the old branch's roster until April.
    """
    from vs_config.clock import tenant_today

    from .models import EmployeeSalary

    qs = EmployeeSalary.objects.filter(entity=entity, is_active=True)
    if branch is not None:
        qs = qs.with_branch_on(on or tenant_today(entity.tenant)).filter(
            branch_on_id=getattr(branch, "pk", branch),
        )
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
    """Refuse a branch run that would overlap another run of the same period.

    **Only under PER_BRANCH.** A CENTRAL school may raise several runs in one
    month: a supplementary run for a late hire, or hand-typed advances. What stops
    a person being paid twice there is the person guard in
    :func:`generate_run_from_roster`, which leaves off anybody a live run of the
    period already pays, whatever its branch or scope.

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


def already_paid(entity, salaries, *, period_start, period_end) -> dict:
    """``{salary_id: run}`` of the people a live run of the period already pays.

    The person guard. A line is the person's when it names their salary row, or,
    for a line written before lines named one, the same user account. Any live
    run counts, draft included, whatever its branch or scope: a draft is a run
    somebody means to pay.
    """
    from django.db.models import Q

    from .models import PayrollLine

    ids = [s.pk for s in salaries]
    users = {s.employee_id: s.pk for s in salaries if s.employee_id}
    rows = (
        PayrollLine.objects.filter(
            run__entity=entity, run__pay_date__gte=period_start, run__pay_date__lte=period_end,
        )
        .exclude(run__run_status=PayrollRunStatus.CANCELLED)
        .filter(Q(salary_id__in=ids) | Q(salary__isnull=True, employee_id__in=list(users)))
        .select_related("run")
    )
    out = {}
    for line in rows:
        salary_id = line.salary_id or users.get(line.employee_id)
        if salary_id is not None:
            out.setdefault(salary_id, line.run)
    return out


@transaction.atomic
def generate_run_from_roster(entity, *, pay_date, branch=None, period_label="",
                             narration="", currency=None, actor_user=None):
    """Raise a draft :class:`PayrollRun` with one line per person due to be paid.

    The month's payroll date is the last day of its payroll period
    (:func:`vs_finance.payroll_statutory.payroll_period`), and each person's pay
    terms are those in force on it (:meth:`EmployeeSalary.terms_on`): their
    gross, structure, branch and state then, whichever day of the month the run
    is raised on. Each line is worked out in full - PAYE, pension, NHF, voluntary
    deductions and employer contributions - by
    :func:`vs_finance.payroll_statutory.work_out_line`.

    ``branch`` picks which of the two shapes this is. Left out it is a central
    run over the whole entity; given, it covers the people whose branch on the
    payroll date is that branch, read exclusively (:func:`roster_for` argues
    why). Deciding *whether* to pass one is the caller's job, because that is the
    school's setting rather than this function's business.

    **The person guard.** Anybody a live run of the period already pays is left
    off and listed in ``run.skipped`` (``[(name, run)]``), so nobody is paid twice
    in a month whatever the runs' branches or scopes: Mr Bello, paid by Ikeja on
    the 20th and moved to Lekki from October, is not on Lekki's September run.
    The salary rows are locked first, so two officers raising runs at the same
    moment cannot both take the same person. Raises :class:`PayrollError` when
    nobody is left to pay.

    **Pay brought forward.** Each person's
    :class:`~vs_finance.models.PayBroughtForward` rows of the tax year (a
    previous employer's months, and this employer's own months before its
    payroll ran here) enter their computed PAYE. Somebody on the run who joined
    after January of the tax year with neither recorded
    (:func:`~vs_finance.payroll_statutory.starters_without_previous_pay`) is
    listed in ``run.previous_pay_missing`` (their names), on every run until it
    is recorded; a tenant whose payroll settings require the record
    (``previous_pay_required``) is refused the run instead, while PAYE is
    computed.
    """
    from .models import PayrollLine, PayrollLineItem, PayrollRun
    from .models import EmployeeSalary
    from .payroll_statutory import (
        active_jurisdictions,
        payroll_period,
        payroll_settings,
        pay_brought_forward_for,
        starters_without_previous_pay,
        voluntary_due,
        work_out_line,
        year_to_date,
    )
    from .payroll_tax import table_for

    ensure_no_overlapping_run(entity, pay_date, branch)
    period_start, period_end = payroll_period(entity, pay_date)
    branch_id = getattr(branch, "pk", branch)

    candidates = list(
        EmployeeSalary.objects.select_for_update(of=("self",))
        .filter(entity=entity, is_active=True).order_by("pk")
    )
    rows = (
        EmployeeSalary.objects.filter(pk__in=[c.pk for c in candidates])
        .select_related("cost_center", "branch", "structure", "residence_state", "pfa")
        .prefetch_related(
            "structure__components", "versions__structure__components",
            "versions__branch", "versions__residence_state", "versions__cost_center",
        )
        .order_by("name", "pk")
    )
    due = []
    for row in rows:
        terms = row.terms_on(period_end)
        if terms is None:
            continue
        if branch_id is not None and row.branch_on(period_end) != branch_id:
            continue
        due.append((row, terms))
    if not due:
        raise PayrollError(
            f"No active employees on the {branch.name} salary roster to generate a "
            f"run from."
            if branch is not None else
            "No active employees on the salary roster to generate a run from.",
        )

    paid = already_paid(entity, [row for row, _ in due],
                        period_start=period_start, period_end=period_end)
    skipped = [(row.name, paid[row.pk]) for row, _ in due if row.pk in paid]
    due = [(row, terms) for row, terms in due if row.pk not in paid]
    if not due:
        shown = ", ".join(sorted({run.document_number or str(run.pk) for _, run in skipped}))
        raise PayrollError(
            f"Everybody on this roster is already on a payroll run for this period "
            f"({shown}). Void a run before raising it again, or nobody is paid twice.",
        )

    policy = payroll_settings(entity)
    table = None
    if policy.paye_method != PayeMethod.SUPPLIED:
        table = table_for(policy.tax_country, period_end)
    people = [row for row, _ in due]
    ytd = year_to_date(
        entity, people, year_start=period_end.replace(month=1, day=1), period_start=period_start,
    )
    previous = pay_brought_forward_for(people, period_end.year)
    missing = [
        salary for salary, _ in starters_without_previous_pay(
            entity, people, tax_year=period_end.year, upcoming_month=period_end.month,
        )
    ]
    if missing and policy.previous_pay_required and policy.paye_method != PayeMethod.SUPPLIED:
        raise PayrollError(
            f"{len(missing)} person(s) on this run joined after January and have no earlier "
            f"pay recorded for {period_end.year}. This school requires it before they are "
            f"paid, so their PAYE counts what a previous employer already taxed: record it "
            f"on their salary record (zeros where there was none), then raise the run.",
        )
    voluntary = voluntary_due(people, period_start=period_start, period_end=period_end)
    jurisdictions = active_jurisdictions(policy.tax_country)

    run = PayrollRun.objects.create(
        entity=entity, branch=branch, pay_date=pay_date, period_label=period_label,
        narration=narration, currency=currency, created_by=actor_user,
    )
    items = []
    for i, (row, terms) in enumerate(due, start=1):
        line_branch = terms.branch if terms.branch_id else None
        figures = work_out_line(
            row, terms, policy=policy, period_end=period_end, ytd=ytd[row.pk],
            voluntary=voluntary.get(row.pk, []), branch=line_branch,
            jurisdictions=jurisdictions, table=table, brought_forward=previous.get(row.pk),
        )
        line = PayrollLine.objects.create(
            run=run, line_no=i, employee_id=row.employee_id, employee_name=row.name,
            salary=row, branch_id=terms.branch_id,
            gross_amount=figures.gross, paye_amount=figures.paye,
            pension_amount=figures.pension, other_deductions_amount=figures.other_deductions,
            employer_contributions_amount=figures.employer_contributions,
            net_amount=figures.net, taxable_pay=figures.taxable_pay,
            paye_source=figures.paye_source, tax_table=figures.tax_table,
            tax_basis=figures.tax_basis, tax_state=figures.tax_state,
            pfa_id=row.pfa_id, tax_id=row.tax_id, pension_pin=row.pension_pin,
            cost_center_id=terms.cost_center_id, components=figures.components,
        )
        items.extend(PayrollLineItem(line=line, **item) for item in figures.items)
    PayrollLineItem.objects.bulk_create(items)
    compute_payroll(run)
    run.refresh_from_db()
    run.skipped = skipped
    run.previous_pay_missing = [salary.name for salary in missing]
    return run


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


# --------------------------------------------------------------------------- #
# One run, one journal per branch                                             #
# --------------------------------------------------------------------------- #
#
# A central run covers every branch's staff, but the money it moves is each
# branch's own: Ikeja's teachers are Ikeja's salary cost, and they are paid out of
# Ikeja's bank account. So a run whose staff sit in several branches posts one
# accrual journal per branch, each naming its branch, and each branch's share is
# paid from that branch's bank account (a :class:`PayrollRunBranch` per branch).
# A run whose staff all sit in one branch (a branch run, any run at a school with
# one branch, a central run that happens to pay one branch) posts one journal on
# the run itself, as every run did before.
#
# Which branch a line belongs to is decided at posting, never guessed: the run's
# own branch for a branch run, else the line's, else the branch on the
# employee's salary row, else the school's only branch. At a school with several
# branches a line none of those answers blocks the posting, by name.


def _line_branch_ids(run) -> dict:
    """Each line of ``run`` with the branch id its pay is booked to, and who has none.

    Returns ``{line: branch_id}``. A line answered by nothing at a tenant with
    several branches maps to ``_UNASSIGNED``. A tenant that owns no branch raises
    :class:`~vs_finance.exceptions.BranchlessTenantError`, so no line is ever
    booked without one.
    """
    from .branch_ledger import only_branch_id_or_several
    from .models import EmployeeSalary

    lines = list(
        run.lines.select_related("cost_center", "tax_state", "pfa")
        .prefetch_related("items__deduction_type__liability_account")
        .order_by("line_no", "id")
    )
    if run.branch_id is not None:
        return {line: run.branch_id for line in lines}

    from .payroll_statutory import payroll_period

    only = only_branch_id_or_several(run.entity.tenant_id)
    employees = {line.employee_id for line in lines if line.branch_id is None and line.employee_id}
    salary_branch = {}
    if employees:
        payroll_date = payroll_period(run.entity, run.pay_date)[1]
        rows = (
            EmployeeSalary.objects.filter(entity=run.entity, employee_id__in=employees)
            .with_branch_on(payroll_date).filter(branch_on_id__isnull=False)
            .order_by("is_active", "id").values_list("employee_id", "branch_on_id")
        )
        salary_branch = dict(rows)

    out = {}
    for line in lines:
        branch_id = line.branch_id or salary_branch.get(line.employee_id) or only
        if branch_id is None:
            branch_id = _UNASSIGNED
        out[line] = branch_id
    return out


#: The branch of a line nothing places, at a tenant with several branches.
_UNASSIGNED = object()


def _require_every_line_placed(run, placed) -> None:
    """Refuse to post while any line has no branch, naming who.

    Corona runs one payroll for Ikeja, Lekki and Yaba. If Mr Okon, a driver, has
    no branch on their salary row, nobody can say whose salary cost they are or whose
    bank pays them, and guessing would book them to a branch that never employed
    them. So the run stops, names them, and posts once they are placed.
    """
    missing = [line for line, branch_id in placed.items() if branch_id is _UNASSIGNED]
    if not missing:
        return
    names = [line.employee_name or f"line {line.line_no}" for line in missing]
    shown = ", ".join(names[:10])
    more = f" and {len(names) - 10} more" if len(names) > 10 else ""
    raise PayrollBranchUnassignedError(
        f"Payroll run {run.document_number or run.pk} pays staff with no branch: "
        f"{shown}{more}. Give each of them a branch on the salary roster (or on the "
        f"line), then post the run.",
        employees=names,
    )


def _post_accrual(run, lines, *, branch_id, accounts, resolver, period, actor_user,
                  label_suffix=""):
    """Post one accrual journal for ``lines``, booked to ``branch_id``; return it and its totals.

    ``Dr salary expense`` and ``Dr`` each employer contribution's expense, per
    cost centre, so the GL slices by department; ``Cr`` each deduction's and
    contribution's payable, as :class:`~vs_finance.payroll_statutory.ItemAccounts`
    places it (PAYE by state, pension by PFA, NHF, NSITF, ITF, voluntary
    deductions), and ``Cr net wages payable``. Every figure is the sum of these
    lines alone, so each branch's journal balances on its own:
    ``gross + employer = deductions + employer liabilities + net``. Each item is
    stamped with the accounts it posted to.
    """
    from .models import JournalEntry, JournalLine, PayrollLineItem
    from .payroll_statutory import ensure_line_items

    salary, _paye, _pension, net = accounts
    totals = {
        "gross": sum(line.gross_amount for line in lines),
        "paye": sum(line.paye_amount for line in lines),
        "pension": sum(line.pension_amount for line in lines),
        "other": sum(line.other_deductions_amount for line in lines),
        "employer": sum(line.employer_contributions_amount for line in lines),
        "net": sum(line.net_amount for line in lines),
    }
    entry = JournalEntry.objects.create(
        entity=run.entity, branch_id=branch_id,
        date=run.pay_date, period=period, source=JournalSource.PAYROLL,
        currency=run.currency,
        narration=(
            run.narration or f"Payroll {run.period_label or run.document_number or ''}".strip()
        ) + label_suffix,
        created_by=actor_user,
    )
    debits: dict = defaultdict(int)
    credits: dict = defaultdict(int)
    objects: dict = {}
    for line in lines:
        debits[(salary.pk, line.cost_center_id, "Gross salaries")] += line.gross_amount
        objects[salary.pk], objects[("cc", line.cost_center_id)] = salary, line.cost_center
        for item in ensure_line_items(line):
            liability, expense = resolver.for_item(line, item)
            PayrollLineItem.objects.filter(pk=item.pk).update(
                liability_account=liability, expense_account=expense,
            )
            objects[liability.pk] = liability
            label = item.get_code_display()
            if item.code == PayrollItemCode.VOLUNTARY:
                label = item.label or label
            credits[(liability.pk, f"{label} payable")] += item.amount
            if item.kind == PayrollItemKind.EMPLOYER and expense is not None:
                objects[expense.pk] = expense
                debits[(expense.pk, line.cost_center_id, item.get_code_display())] += item.amount
    credits[(net.pk, "Net wages payable")] += totals["net"]
    objects[net.pk] = net

    line_no = 0
    for (account_id, cc_id, label), amount in debits.items():
        if amount <= 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=objects[account_id], debit=amount, credit=0,
            description=label, cost_center=objects[("cc", cc_id)], line_no=line_no,
        )
    for (account_id, label), amount in credits.items():
        if amount <= 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=objects[account_id], debit=0, credit=amount,
            description=label, line_no=line_no,
        )
    post_journal(entry, actor_user=actor_user)
    return entry, totals


# Public wrapper for payroll accrual posting.
def post_payroll(run, *, actor_user=None):
    """Compute, validate and post a payroll run's **accrual**, one journal per branch.

    The accrual is audited once per branch share, each entry carrying that
    share's figures and filed under its branch, so a branch's bursar reads their
    own staff's cost and never the school's total; a run booked as one journal
    is audited once, under that journal's branch. Records a durable rejection
    audit on any :class:`FinanceError`, then re-raises.
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
    from vs_tenants.models import Branch

    from .models import PayrollLine, PayrollRunBranch

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
    placed = _line_branch_ids(run)
    for line in placed:  # Validate every employee line.
        if line.net_amount < 0:  # Deductions cannot exceed gross pay.
            raise PayrollError(
                f"Net pay is negative for {line.employee_name or line.employee_id}: "
                f"deductions exceed gross.",
            )
    _require_every_line_placed(run, placed)

    groups: dict = {}
    for line, branch_id in placed.items():
        groups.setdefault(branch_id, []).append(line)
        if line.branch_id != branch_id:
            PayrollLine.objects.filter(pk=line.pk).update(branch_id=branch_id)

    accounts = _accounts_for(run)  # Resolve expense and liability accounts.
    resolver = ItemAccounts(run, paye_base=accounts[1], pension_base=accounts[2])
    period = resolve_period(run.entity, run.pay_date)  # Find payroll period.
    shares = []
    if len(groups) == 1:
        (branch_id, lines), = groups.items()
        entry, _ = _post_accrual(
            run, lines, branch_id=branch_id, accounts=accounts, resolver=resolver,
            period=period, actor_user=actor_user,
        )
        run.journal = entry  # Link run to accrual journal.
    else:
        names = dict(Branch.all_objects.filter(pk__in=groups).values_list("pk", "name"))
        for branch_id in sorted(groups, key=lambda b: names.get(b, "")):
            entry, totals = _post_accrual(
                run, groups[branch_id], branch_id=branch_id, accounts=accounts,
                resolver=resolver, period=period, actor_user=actor_user,
                label_suffix=f" - {names.get(branch_id, '')}",
            )
            shares.append(PayrollRunBranch.objects.create(
                run=run, branch_id=branch_id, journal=entry,
                gross_total=totals["gross"], paye_total=totals["paye"],
                pension_total=totals["pension"], other_deductions_total=totals["other"],
                employer_contributions_total=totals["employer"], net_total=totals["net"],
                status=PayrollRunStatus.POSTED,
            ))

    salary, paye, pension, net = accounts
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

    for share in shares:
        record(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_POSTED,
            actor_user=actor_user, target=run, branch=share.branch_id,
            message=(
                f"Accrued {names.get(share.branch_id, '')}'s payroll: "
                f"gross {share.gross_total}, net {share.net_total} kobo."
            ),
            journal_id=share.journal_id, gross=share.gross_total, paye=share.paye_total,
            pension=share.pension_total, other_deductions=share.other_deductions_total,
            employer_contributions=share.employer_contributions_total, net=share.net_total,
        )
    if not shares:
        record(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_POSTED,
            actor_user=actor_user, target=run, branch=run.journal.branch_id,
            message=f"Accrued payroll: gross {run.gross_total}, net {run.net_total} kobo.",
            journal_id=run.journal_id, gross=run.gross_total, paye=run.paye_total,
            pension=run.pension_total, other_deductions=run.other_deductions_total,
            employer_contributions=run.employer_contributions_total, net=run.net_total,
        )
    return run  # Return posted payroll run.


# Public wrapper for net wage disbursement.
def pay_payroll(run, *, bank_account=None, bank_accounts=None, pay_date=None, actor_user=None):
    """Disburse a posted run's net pay: ``Dr net wages payable, Cr bank``, per branch.

    A run posted as one journal is paid from ``bank_account`` (or the one stored on
    the run). A run posted one journal per branch is paid a branch at a time: each
    of ``bank_accounts`` (or ``bank_account`` alone) pays the share of the branch
    it belongs to, so Ikeja's share leaves Ikeja's bank and nothing else's. The
    run reads PAID once every share is paid.
    """
    try:  # Atomic worker performs disbursement posting.
        return _pay_payroll_atomic(  # Pay net wages.
            run, bank_account=bank_account, bank_accounts=bank_accounts,
            pay_date=pay_date, actor_user=actor_user,
        )
    except FinanceError as exc:  # Failed disbursements should be auditable.
        record_rejection(  # Record durable rejection.
            entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,  # Existing disbursement audit action.
            exc=exc, actor_user=actor_user, target=run,  # Error, actor, and target context.
        )
        raise


def _require_branch_bank(bank_account, branch_id, run, whose) -> None:
    """Refuse a bank account that is not ``branch_id``'s own (the same-branch rule)."""
    from vs_rbac.scoping import same_transaction_branch

    if same_transaction_branch(run.entity.tenant_id, bank_account.branch_id, branch_id):
        return
    raise PayrollError(
        f"{whose} of payroll run {run.document_number or run.pk} is paid from its "
        f"own branch's bank account, not from {bank_account.name}.",
    )


def _post_disbursement(run, *, net_total, bank_account, branch_id, pay_date, actor_user):
    """Post one ``Dr net wages payable, Cr bank`` journal booked to ``branch_id``."""
    from .models import JournalEntry, JournalLine

    net = run.net_payable_account or resolve_account(  # Resolve net wages liability account.
        run.entity, NET_WAGES_PAYABLE_CODE, label="net wages payable",  # Default account code.
    )
    entry = JournalEntry.objects.create(
        entity=run.entity, branch_id=branch_id,
        date=pay_date, period=resolve_period(run.entity, pay_date), source=JournalSource.BANK,
        currency=run.currency,
        narration=f"Pay net wages {run.period_label or run.document_number or ''}".strip(),
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=net, debit=net_total, credit=0,
        description="Net wages payable", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=net_total,
        description="Net wages paid", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)
    return entry


@transaction.atomic
# Transactional payroll payment.
def _pay_payroll_atomic(run, *, bank_account=None, bank_accounts=None, pay_date=None,
                        actor_user=None):
    if run.run_status != PayrollRunStatus.POSTED:  # Only accrued payroll can be paid.
        raise PayrollError(
            f"Payroll run {run.document_number or run.pk} is '{run.run_status}', "
            f"it must be posted (accrued) before it can be paid.",
        )
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
        remedy=f"Date the payroll payment {format_date(run.pay_date, run.entity.tenant)} or later.",
        tenant=run.entity.tenant,
    )

    if run.branch_shares.exists():
        return _pay_branch_shares(
            run, list(bank_accounts or ([bank_account] if bank_account else [])),
            pay_date=pay_date, actor_user=actor_user,
        )

    bank_account = bank_account or run.bank_account  # Use explicit bank or stored bank.
    if bank_account is None:  # Disbursement needs a bank account.
        raise PayrollError("No bank account set to disburse the payroll from.")
    branch_id = run.journal.branch_id if run.journal_id else run.branch_id
    _require_branch_bank(bank_account, branch_id, run, "The pay")
    entry = _post_disbursement(
        run, net_total=run.net_total, bank_account=bank_account, branch_id=branch_id,
        pay_date=pay_date, actor_user=actor_user,
    )

    run.disbursement_journal = entry  # Link run to disbursement journal.
    run.bank_account = bank_account  # Persist bank account used.
    run.run_status = PayrollRunStatus.PAID  # Mark payroll paid.
    run.save(update_fields=[
        "disbursement_journal", "bank_account", "run_status", "updated_at",  # Journal, bank, status.
    ])

    record(  # Audit successful disbursement.
        entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,  # Audit action.
        actor_user=actor_user, target=run, branch=branch_id,  # The journal's branch.
        message=f"Disbursed net wages {run.net_total} kobo from {bank_account.name}.",  # Summary.
        journal_id=entry.pk, net=run.net_total,  # Structured metadata.
    )
    from .payslips import issue_payslips

    issue_payslips(run, run.lines.all(), actor_user=actor_user)
    return run  # Return paid payroll run.


def _pay_branch_shares(run, bank_accounts, *, pay_date, actor_user):
    """Pay each named account's branch share of a run posted one journal per branch.

    Mr Bello pays January's run from Ikeja's and Lekki's accounts on the 25th and
    Yaba's on the 27th, when its transfer clears: each account pays its own
    branch's share, a share is paid once, and the run reads PAID when the last
    one is.
    """
    if not bank_accounts:
        raise PayrollError(
            "This run pays several branches' staff. Name each branch's bank account "
            "to pay its share from.",
        )
    shares = {
        share.branch_id: share
        for share in run.branch_shares.select_for_update().select_related("branch")
    }
    chosen = {}
    for bank in bank_accounts:
        share = shares.get(bank.branch_id)
        if share is None:
            where = bank.branch.name if bank.branch_id else "no branch"
            raise PayrollError(
                f"{bank.name} belongs to {where}, and payroll run "
                f"{run.document_number or run.pk} pays no staff there. Pay each "
                f"branch's share from that branch's own account.",
            )
        if share.status == PayrollRunStatus.PAID:
            raise PayrollError(f"{share.branch.name}'s share of this run is already paid.")
        if share.branch_id in chosen:
            raise PayrollError(f"Name one account for {share.branch.name}'s share, not two.")
        chosen[share.branch_id] = (share, bank)

    for share, bank in chosen.values():
        _require_branch_bank(bank, share.branch_id, run, f"{share.branch.name}'s share")
        entry = _post_disbursement(
            run, net_total=share.net_total, bank_account=bank, branch_id=share.branch_id,
            pay_date=pay_date, actor_user=actor_user,
        )
        share.disbursement_journal = entry
        share.bank_account = bank
        share.status = PayrollRunStatus.PAID
        share.save(update_fields=["disbursement_journal", "bank_account", "status", "updated_at"])
        record(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_PAID,
            actor_user=actor_user, target=run, branch=share.branch_id,
            message=(
                f"Disbursed {share.branch.name}'s net wages {share.net_total} kobo "
                f"from {bank.name}."
            ),
            journal_id=entry.pk, net=share.net_total, branch_id=share.branch_id,
        )
        from .payslips import issue_payslips

        issue_payslips(run, run.lines.filter(branch_id=share.branch_id), actor_user=actor_user)

    if all(share.status == PayrollRunStatus.PAID for share in shares.values()):
        run.run_status = PayrollRunStatus.PAID
        run.save(update_fields=["run_status", "updated_at"])
    return run


@transaction.atomic
# Cancel or void a payroll run.
def cancel_payroll_run(run, *, actor_user=None):
    """Cancel / void a payroll run raised in error, by its state:

    * **DRAFT** - nothing posted, just mark it CANCELLED.
    * **POSTED** (accrued, not yet paid) - reverse the accrual (an audit-correct
      mirror that backs out the salary expense and the PAYE/pension/net liabilities)
      and mark it CANCELLED. A run posted one journal per branch has each branch's
      journal reversed on its own, so each reversal is booked to its branch.
    * **PAID** - refused: the net wages have already left the bank, so the disbursement
      must be reversed first (a real cash clawback), before the run can be voided. A
      run with any branch's share already paid is refused the same way.

    Voiding a run posted per branch is audited once per share, under its branch.
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
    shares = list(run.branch_shares.select_for_update().select_related("branch"))
    paid = [share.branch.name for share in shares if share.status == PayrollRunStatus.PAID]
    if paid:
        raise PayrollError(
            f"{', '.join(paid)} already paid its share of this run - those net wages "
            f"left the bank. Reverse the disbursement before voiding the run.",
        )

    if run.run_status == PayrollRunStatus.POSTED:  # Accrued unpaid payroll needs reversal.
        if run.journal_id is not None:
            reverse_journal(run.journal, actor_user=actor_user, document_owner=run)
        for share in shares:
            reverse_journal(share.journal, actor_user=actor_user, document_owner=run)
            share.status = PayrollRunStatus.CANCELLED
            share.save(update_fields=["status", "updated_at"])

    was = run.run_status  # Capture previous status for audit message.
    run.run_status = PayrollRunStatus.CANCELLED  # Mark payroll run cancelled.
    run.status = DocumentStatus.CANCELLED  # Mark finance document cancelled.
    run.save(update_fields=["run_status", "status", "updated_at"])

    label = run.document_number or run.pk
    if was == PayrollRunStatus.POSTED and shares:
        for share in shares:
            record(
                entity=run.entity, action=FinanceAuditAction.PAYROLL_CANCELLED,
                actor_user=actor_user, target=run, branch=share.branch_id,
                message=(
                    f"Voided {share.branch.name}'s share of payroll run {label} "
                    f"(reversed accrual journal {share.journal_id})."
                ),
                journal_id=share.journal_id, previous_status=was,
            )
    elif was == PayrollRunStatus.POSTED:
        record(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_CANCELLED,
            actor_user=actor_user, target=run,
            branch=run.journal.branch_id if run.journal_id else run.branch_id,
            message=f"Voided payroll run {label} (reversed accrual journal {run.journal_id}).",
            journal_id=run.journal_id, previous_status=was,
        )
    else:
        record(
            entity=run.entity, action=FinanceAuditAction.PAYROLL_CANCELLED,
            actor_user=actor_user, target=run,
            message=f"Cancelled draft payroll run {label}.",
            journal_id=run.journal_id, previous_status=was,
        )
    return run  # Return cancelled payroll run.
