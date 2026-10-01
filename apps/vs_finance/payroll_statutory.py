"""Statutory payroll: what each person's line deducts and contributes, and where it posts.

A generated payroll line is worked out here (:func:`work_out_line`) from three
things: the person's pay terms in force on the payroll date (their salary
history, :meth:`EmployeeSalary.terms_on`), the tenant's payroll policy
(:class:`~vs_finance.models.FinancePayrollSettings`), and, for PAYE, the
national tax table of the payroll month's tax year
(:mod:`vs_finance.payroll_tax`).

**The payroll date of a month is the last day of its payroll period**: the
entity's fiscal period containing the run's pay date, or that calendar month
where no period covers it (:func:`payroll_period`). One date per month, so a
person moving branch or getting a raise mid-month is read the same way by every
run of that month: the terms in force on that day decide the whole month,
including which branch pays it. There is no apportioning by days.

**Where each item posts** (:func:`item_accounts`): PAYE to the payable account of
the line's tax state, so each state's revenue service gets its own return and
remittance; employee and employer pension to the payable account of the line's
pension fund administrator, so each PFA gets its own; NHF, NSITF and ITF to
their own payables; a voluntary deduction to its type's liability account. The
per-state and per-PFA accounts, and the tax obligations that drain them, are
created the first time a run posts to them (:func:`paye_account_for`,
:func:`pension_account_for`). A line whose state or PFA is unknown posts to the
tenant's base PAYE or pension payable, as every line did before states and PFAs
were recorded.

**Salary history** (:func:`change_terms`): an edit to a person's pay terms writes
a new :class:`~vs_finance.models.EmployeeSalaryVersion` effective from a date,
and is audited. A change can never be dated into a payroll month the person has
already been paid for, and a branch move that would leave a person on no run in
a month is refused (:func:`assert_move_reaches_a_run`). Salary structures keep
their history the same way (:func:`replace_components`).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q, Sum
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import (
    EMPLOYER_PENSION_EXPENSE_CODE,
    FinanceAuditAction,
    ITF_EXPENSE_CODE,
    ITF_PAYABLE_CODE,
    NHF_PAYABLE_CODE,
    NSITF_EXPENSE_CODE,
    NSITF_PAYABLE_CODE,
    PAYE_PAYABLE_CODE,
    PENSION_PAYABLE_CODE,
    PayeMethod,
    PayeSource,
    PayrollItemCode,
    PayrollItemKind,
    PayrollRunStatus,
    SalaryComponentKind,
    StatutoryType,
    TaxFilingFrequency,
    TaxObligationType,
)
from .exceptions import MissingAccountError, PayrollError


# --------------------------------------------------------------------------- #
# Policy and dates                                                            #
# --------------------------------------------------------------------------- #

def payroll_settings(entity):
    """The entity's stored payroll policy, or an unsaved one carrying the defaults."""
    from .models import FinancePayrollSettings

    stored = FinancePayrollSettings.objects.filter(entity=entity).first()
    return stored or FinancePayrollSettings(entity=entity)


def payroll_period(entity, date):
    """``(start, end)`` of the payroll period containing ``date``.

    The entity's fiscal period containing the date, which is the period a run's
    accrual posts into; the calendar month where no period covers it.
    """
    from .posting import resolve_period

    period = resolve_period(entity, date)
    if period is not None:
        return period.start_date, period.end_date
    start = date.replace(day=1)
    following = (start + datetime.timedelta(days=32)).replace(day=1)
    return start, following - datetime.timedelta(days=1)


def _next_day(date):
    return date + datetime.timedelta(days=1)


# --------------------------------------------------------------------------- #
# Where a person's PAYE goes                                                  #
# --------------------------------------------------------------------------- #

_ALIASES = {"fct": "federalcapitalterritory", "abuja": "federalcapitalterritory"}


def _normalise(text) -> str:
    """A state name reduced to letters, without a trailing "state", for matching."""
    value = re.sub(r"[^a-z]", "", str(text or "").lower())
    if value.endswith("state") and len(value) > len("state"):
        value = value[: -len("state")]
    return _ALIASES.get(value, value)


def jurisdiction_for_text(text, jurisdictions):
    """The jurisdiction a free-text state ("Lagos", "Lagos State", "FCT") names, or None."""
    wanted = _normalise(text)
    if not wanted:
        return None
    for jurisdiction in jurisdictions:
        if wanted in (_normalise(jurisdiction.name), jurisdiction.code.lower()):
            return jurisdiction
    return None


def active_jurisdictions(country):
    from .models import PayrollTaxJurisdiction

    return list(PayrollTaxJurisdiction.objects.filter(country=country, is_active=True))


def tax_state_for(terms, branch, jurisdictions):
    """The state a person's PAYE is remitted to: their residence, else their branch's state.

    The residence on the pay terms in force wins. A person with none recorded is
    taken to live in the state of the branch that pays them, read from the
    branch's state; a branch whose state is blank or not recognised leaves the
    line with no state, and its PAYE posts to the tenant's base PAYE payable.
    """
    if getattr(terms, "residence_state_id", None):
        return terms.residence_state
    if branch is not None:
        return jurisdiction_for_text(getattr(branch, "state", ""), jurisdictions)
    return None


# --------------------------------------------------------------------------- #
# Year to date                                                                #
# --------------------------------------------------------------------------- #

def _live_lines():
    from .models import PayrollLine

    return PayrollLine.objects.exclude(run__run_status=PayrollRunStatus.CANCELLED)


def year_to_date(entity, salaries, *, year_start, period_start) -> dict:
    """``{salary_id: YearToDate}`` of each person's earlier months this tax year.

    Counts the lines of runs that were not cancelled, dated from the start of
    the tax year up to (not including) this payroll period. A line is the
    person's when it names their salary row, or, for a line written before lines
    named one, when it names the same user account.
    """
    from .models import PayrollLineItem
    from .payroll_tax import YearToDate

    out = {s.pk: YearToDate() for s in salaries}
    if not salaries:
        return out
    window = _live_lines().filter(
        run__entity=entity, run__pay_date__gte=year_start, run__pay_date__lt=period_start,
    )
    by_user = {s.employee_id: s.pk for s in salaries if s.employee_id}

    def add(rows, key_of):
        for row in rows:
            salary_id = key_of(row)
            if salary_id is None:
                continue
            ytd = out[salary_id]
            ytd.gross += int(row["gross"] or 0)
            ytd.taxable_pay += int(row["taxable"] or 0)
            ytd.paye += int(row["paye"] or 0)
            ytd.pension += int(row["pension"] or 0)

    figures = dict(
        gross=Sum("gross_amount"), taxable=Sum("taxable_pay"),
        paye=Sum("paye_amount"), pension=Sum("pension_amount"),
    )
    add(
        window.filter(salary_id__in=list(out)).values("salary_id").annotate(**figures),
        lambda row: row["salary_id"],
    )
    if by_user:
        add(
            window.filter(salary__isnull=True, employee_id__in=list(by_user))
            .values("employee_id").annotate(**figures),
            lambda row: by_user.get(row["employee_id"]),
        )
    nhf = (
        PayrollLineItem.objects.filter(
            code=PayrollItemCode.NHF, line__in=window.filter(salary_id__in=list(out)),
        )
        .values("line__salary_id").annotate(total=Sum("amount"))
    )
    for row in nhf:
        out[row["line__salary_id"]].nhf += int(row["total"] or 0)
    return out


def voluntary_due(salaries, *, period_start, period_end) -> dict:
    """``{salary_id: [(assignment, amount)]}`` of the voluntary deductions due this month.

    An assignment is due when it is active, its type is active, and its dates
    overlap the payroll period. One with a ``total_limit`` withholds no more than
    what is left of it after the lines of runs that were not cancelled.
    """
    from .models import EmployeeDeduction, PayrollLineItem

    rows = list(
        EmployeeDeduction.objects.filter(
            salary__in=salaries, is_active=True, deduction_type__is_active=True,
        )
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=period_end))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=period_start))
        .select_related("deduction_type")
        .order_by("salary_id", "deduction_type__code", "id")
    )
    limited = [row.pk for row in rows if row.total_limit is not None]
    withheld = {}
    if limited:
        withheld = dict(
            PayrollLineItem.objects.filter(employee_deduction_id__in=limited)
            .exclude(line__run__run_status=PayrollRunStatus.CANCELLED)
            .values("employee_deduction_id").annotate(total=Sum("amount"))
            .values_list("employee_deduction_id", "total")
        )
    out: dict = {}
    for row in rows:
        amount = int(row.amount)
        if row.total_limit is not None:
            amount = min(amount, int(row.total_limit) - int(withheld.get(row.pk) or 0))
        if amount > 0:
            out.setdefault(row.salary_id, []).append((row, amount))
    return out


# --------------------------------------------------------------------------- #
# One person's line                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class LineFigures:
    """Everything a generated payroll line and its items are written from."""

    gross: int
    paye: int
    pension: int
    other_deductions: int
    employer_contributions: int
    taxable_pay: int
    paye_source: str
    components: list
    tax_state: object = None
    tax_table: object = None
    tax_basis: dict = field(default_factory=dict)
    items: list = field(default_factory=list)

    @property
    def net(self) -> int:
        return self.gross - self.paye - self.pension - self.other_deductions


def _pct(base, rate_bps) -> int:
    return int(base) * int(rate_bps) // 10000


def work_out_line(salary, terms, *, policy, period_end, ytd, voluntary, branch, jurisdictions,
                  table=None) -> LineFigures:
    """One person's pay for the month whose payroll date is ``period_end``.

    ``terms`` are the pay terms in force on that date; ``ytd`` the
    :class:`~vs_finance.payroll_tax.YearToDate` of the earlier months;
    ``voluntary`` the ``[(assignment, amount)]`` due; ``table`` the national tax
    table (required when PAYE is computed).

    Under a COMPUTED policy the employee pension is the policy rate of pensionable
    pay and PAYE comes from the table, unless the salary row carries an explicit
    override. Under SUPPLIED both come from the structure's PAYE and pension
    lines, or the roster's typed figures. NHF, the voluntary deductions and the
    employer contributions follow their own switches in either case.
    """
    from .payroll import apply_structure
    from .payroll_tax import compute_paye

    gross = int(terms.gross_amount or 0)
    structure = terms.structure if terms.structure_id else None
    split = apply_structure(gross, structure, as_at=period_end)
    supplied = policy.paye_method == PayeMethod.SUPPLIED
    items = []

    def item(kind, code, amount, *, basis=0, rate=0, label="", **extra):
        if amount > 0:
            items.append({
                "kind": kind, "code": code, "amount": int(amount), "basis_amount": int(basis),
                "rate_bps": int(rate), "label": label or PayrollItemCode(code).label, **extra,
            })

    pensionable, basic = split["pensionable"], split["basic"] or gross
    if supplied:
        paye = split["paye"] if structure is not None else int(terms.paye_amount or 0)
        pension = split["pension"] if structure is not None else int(terms.pension_amount or 0)
        item(PayrollItemKind.DEDUCTION, PayrollItemCode.PENSION, pension)
        components = split["components"]
    else:
        pension = (
            _pct(pensionable, policy.employee_pension_rate_bps)
            if policy.employee_pension_enabled else 0
        )
        item(PayrollItemKind.DEDUCTION, PayrollItemCode.PENSION, pension,
             basis=pensionable, rate=policy.employee_pension_rate_bps)
        components = [
            c for c in split["components"]
            if not (c["kind"] == SalaryComponentKind.DEDUCTION
                    and c["statutory_type"] in (StatutoryType.PAYE, StatutoryType.PENSION))
        ]

    nhf = _pct(basic, policy.nhf_rate_bps) if policy.nhf_enabled else 0
    item(PayrollItemKind.DEDUCTION, PayrollItemCode.NHF, nhf, basis=basic, rate=policy.nhf_rate_bps)
    voluntary_total = 0
    for assignment, amount in voluntary:
        voluntary_total += amount
        item(PayrollItemKind.DEDUCTION, PayrollItemCode.VOLUNTARY, amount,
             label=assignment.deduction_type.name,
             deduction_type_id=assignment.deduction_type_id, employee_deduction_id=assignment.pk)

    employer = 0
    for enabled, code, base, rate in (
        (policy.employer_pension_enabled, PayrollItemCode.EMPLOYER_PENSION, pensionable,
         policy.employer_pension_rate_bps),
        (policy.nsitf_enabled, PayrollItemCode.NSITF, gross, policy.nsitf_rate_bps),
        (policy.itf_enabled, PayrollItemCode.ITF, gross, policy.itf_rate_bps),
    ):
        amount = _pct(base, rate) if enabled else 0
        employer += amount
        item(PayrollItemKind.EMPLOYER, code, amount, basis=base, rate=rate)

    taxable = split["taxable"]
    basis: dict = {}
    if supplied:
        source = PayeSource.SUPPLIED
    else:
        if table is None:
            raise PayrollError("PAYE is computed, but no tax table was given to compute it.")
        result = compute_paye(
            table.snapshot(), month=period_end.month, prior=ytd, gross_now=gross,
            taxable_now=taxable, pension_now=pension, nhf_now=nhf,
            annual_rent=salary.annual_rent,
        )
        paye, basis, source = result.amount, result.working, PayeSource.COMPUTED
        if salary.paye_override is not None:
            basis["override"] = {
                "amount": int(salary.paye_override), "reason": salary.paye_override_reason,
                "computed": paye,
            }
            paye, source = int(salary.paye_override), PayeSource.OVERRIDE
    item(PayrollItemKind.DEDUCTION, PayrollItemCode.PAYE, paye)

    return LineFigures(
        gross=gross, paye=int(paye), pension=int(pension),
        other_deductions=nhf + voluntary_total, employer_contributions=employer,
        taxable_pay=taxable, paye_source=source, components=components,
        tax_state=tax_state_for(terms, branch, jurisdictions),
        tax_table=None if supplied else table, tax_basis=basis, items=items,
    )


# --------------------------------------------------------------------------- #
# Where each item posts                                                       #
# --------------------------------------------------------------------------- #

def _statutory_account(entity, *, base_code, code, name, base_label):
    """The account ``code`` of ``entity``, created beside ``base_code`` when it is new."""
    from .accounts import resolve_account
    from .models import Account

    account = Account.objects.filter(entity=entity, code=code).first()
    if account is None:
        base = resolve_account(entity, base_code, label=base_label)
        account, _ = Account.objects.get_or_create(
            entity=entity, code=code,
            defaults={
                "name": name, "account_type": base.account_type, "parent": base.parent,
                "is_postable": True, "ifrs_line": base.ifrs_line,
                "subtype": base.subtype,
            },
        )
    if not (account.is_active and account.is_postable):
        raise MissingAccountError(code, label=name)
    return account


def _obligation(entity, *, code, name, obligation_type, account, authority, base_code, filing_day):
    """The tax obligation that drains ``account``, created on first use."""
    from .models import TaxObligation

    base = TaxObligation.objects.filter(entity=entity, code=base_code).first()
    TaxObligation.objects.get_or_create(
        entity=entity, code=code,
        defaults={
            "name": name[:160], "obligation_type": obligation_type,
            "liability_account": account, "authority_name": authority[:160],
            "frequency": base.frequency if base else TaxFilingFrequency.MONTHLY,
            "filing_day": base.filing_day if base else filing_day,
        },
    )


def paye_account_for(entity, jurisdiction):
    """The PAYE payable of one state, and its obligation to that state's revenue service."""
    account = _statutory_account(
        entity, base_code=PAYE_PAYABLE_CODE, code=f"{PAYE_PAYABLE_CODE}-{jurisdiction.code}",
        name=f"PAYE Payable - {jurisdiction.name}", base_label="PAYE payable",
    )
    _obligation(
        entity, code=f"PAYE-{jurisdiction.code}", name=f"PAYE - {jurisdiction.name}",
        obligation_type=TaxObligationType.PAYE, account=account,
        authority=jurisdiction.authority_name, base_code="PAYE", filing_day=10,
    )
    return account


def pension_account_for(entity, pfa):
    """The pension payable of one PFA, and its obligation to that PFA."""
    account = _statutory_account(
        entity, base_code=PENSION_PAYABLE_CODE, code=f"{PENSION_PAYABLE_CODE}-{pfa.code}",
        name=f"Pension Payable - {pfa.name}"[:160], base_label="pension payable",
    )
    _obligation(
        entity, code=f"PENSION-{pfa.code}", name=f"Pension - {pfa.name}",
        obligation_type=TaxObligationType.PENSION, account=account,
        authority=pfa.name, base_code="PENSION", filing_day=7,
    )
    return account


#: Fixed (liability code, expense code) of the items that post to the default chart.
_FIXED_ACCOUNTS = {
    PayrollItemCode.NHF: (NHF_PAYABLE_CODE, None),
    PayrollItemCode.NSITF: (NSITF_PAYABLE_CODE, NSITF_EXPENSE_CODE),
    PayrollItemCode.ITF: (ITF_PAYABLE_CODE, ITF_EXPENSE_CODE),
}


class ItemAccounts:
    """Resolves, once per account per posting, where each line item posts."""

    def __init__(self, run, *, paye_base, pension_base):
        self.run = run
        self.entity = run.entity
        self.paye_base = paye_base
        self.pension_base = pension_base
        self._cache: dict = {}

    def _code(self, code, label):
        from .accounts import resolve_account

        if code not in self._cache:
            self._cache[code] = resolve_account(self.entity, code, label=label)
        return self._cache[code]

    def _keyed(self, key, build):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def for_item(self, line, item):
        """``(liability account, expense account or None)`` for one item of ``line``."""
        code = item.code
        if code == PayrollItemCode.PAYE:
            if line.tax_state_id:
                return self._keyed(("PAYE", line.tax_state_id),
                                   lambda: paye_account_for(self.entity, line.tax_state)), None
            return self.paye_base, None
        if code in (PayrollItemCode.PENSION, PayrollItemCode.EMPLOYER_PENSION):
            liability = self.pension_base
            if line.pfa_id:
                liability = self._keyed(("PFA", line.pfa_id),
                                        lambda: pension_account_for(self.entity, line.pfa))
            if code == PayrollItemCode.EMPLOYER_PENSION:
                return liability, self._code(EMPLOYER_PENSION_EXPENSE_CODE, "employer pension expense")
            return liability, None
        if code == PayrollItemCode.VOLUNTARY:
            if item.deduction_type_id is None:
                raise PayrollError(f"A voluntary deduction on line {line.line_no} names no deduction type.")
            return item.deduction_type.liability_account, None
        liability_code, expense_code = _FIXED_ACCOUNTS[code]
        label = PayrollItemCode(code).label
        return (
            self._code(liability_code, f"{label} payable"),
            self._code(expense_code, f"{label} expense") if expense_code else None,
        )


def ensure_line_items(line) -> list:
    """The line's items, adding PAYE and pension items for a line that has none of its own.

    A hand-typed line, or a draft generated before lines carried items, holds its
    PAYE and pension as figures only; posting reads every deduction from items, so
    those figures become items first.
    """
    from .models import PayrollLineItem

    items = list(line.items.all())
    have = {item.code for item in items}
    for code, amount in ((PayrollItemCode.PAYE, line.paye_amount),
                         (PayrollItemCode.PENSION, line.pension_amount)):
        if amount > 0 and code not in have:
            items.append(PayrollLineItem.objects.create(
                line=line, kind=PayrollItemKind.DEDUCTION, code=code, amount=amount,
                label=PayrollItemCode(code).label,
            ))
    return items


# --------------------------------------------------------------------------- #
# Salary history                                                              #
# --------------------------------------------------------------------------- #

#: Pay terms that are versioned, as (field, how the audit shows it).
TERM_FIELDS = (
    "branch_id", "structure_id", "gross_amount", "paye_amount", "pension_amount",
    "cost_center_id", "residence_state_id",
)

#: Profile fields kept on the salary row itself, audited when they change.
PROFILE_FIELDS = ("name", "employee_id", "tax_id", "pfa_id", "pension_pin", "annual_rent")


def last_paid_period_end(salary):
    """The end of the latest payroll period a live run pays ``salary`` in, or None."""
    latest = (
        _live_lines().filter(salary=salary)
        .order_by("-run__pay_date").values_list("run__pay_date", flat=True).first()
    )
    if latest is None:
        return None
    return payroll_period(salary.entity, latest)[1]


def default_effective_from(salary):
    """When an undated change takes effect: the first payroll month not yet paid.

    The day after the last period the person was paid in, so a change never
    rewrites a month already run; "from the start" for somebody never paid yet.
    """
    from .models import PAYROLL_HISTORY_START

    end = last_paid_period_end(salary)
    return PAYROLL_HISTORY_START if end is None else _next_day(end)


def assert_effective_date_open(salary, effective_from) -> None:
    """Refuse a change dated into a payroll month the person has already been paid for."""
    from vs_config.display import format_date

    end = last_paid_period_end(salary)
    if end is not None and effective_from <= end:
        tenant = salary.entity.tenant
        raise ValidationError({"effective_from": (
            f"{salary.name} has already been paid up to "
            f"{format_date(end, tenant)}. Date the change from "
            f"{format_date(_next_day(end), tenant)} or later, or void that run first."
        )})


def assert_move_reaches_a_run(salary, branch_id, effective_from) -> None:
    """Refuse a branch move that would leave the person on no run in a month.

    Under per-branch payroll each branch pays the people whose branch is its own
    on the payroll date. Mrs Okafor works at Lekki and is moved to Ikeja from
    22 September; Ikeja raised September's run on the 20th, before the move, so
    she is not on it, and Lekki's run on the 25th no longer reaches her. Neither
    branch would pay her September. So the move is refused while the new branch
    already has a run for that month that she is not on: date it from the next
    month (Lekki pays September), or void Ikeja's run and raise it again.

    The month checked is the one containing the effective date, or the current
    month for a change dated "from the start". A central run covers everybody,
    so a tenant running payroll centrally is never refused here.
    """
    from vs_config.clock import tenant_today

    from .models import PAYROLL_HISTORY_START, PayrollRun
    from .payroll import is_per_branch

    entity = salary.entity
    if branch_id is None or not is_per_branch(entity):
        return
    when = effective_from
    if when <= PAYROLL_HISTORY_START:
        when = tenant_today(entity.tenant)
    start, end = payroll_period(entity, when)
    clash = (
        PayrollRun.objects.filter(entity=entity, pay_date__gte=start, pay_date__lte=end)
        .filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        .exclude(run_status=PayrollRunStatus.CANCELLED)
        .exclude(lines__salary=salary)
        .select_related("branch")
        .first()
    )
    if clash is None:
        return
    whose = clash.branch.name if clash.branch_id else "The whole-school run"
    raise ValidationError({"branch": (
        f"{whose} has already raised payroll run {clash.document_number or clash.pk} for "
        f"this month without {salary.name}, so after this move no run would pay them for "
        f"it. Date the move from the next month, or void that run and raise it again."
    )})


def assert_branch_required(entity, branch_id, is_active) -> None:
    """Under per-branch payroll an active person must have a branch, or no run reaches them."""
    from .payroll import is_per_branch

    if is_active and branch_id is None and is_per_branch(entity):
        raise ValidationError({"branch": (
            "This school runs payroll per branch, so every active employee needs a "
            "branch: no branch's run would pay somebody without one."
        )})


def owning_branch_id(salary, date=None):
    """The branch that owns ``salary`` on ``date``, by default the tenant's today.

    :meth:`EmployeeSalary.branch_on` on the tenant's clock. The audit trail of a
    roster row files each entry under this branch, so a change made in March
    to somebody moving from April is in the trail of the branch paying them in
    March.
    """
    if date is None:
        from vs_config.clock import tenant_today

        date = tenant_today(salary.entity.tenant)
    return salary.branch_on(date)


def assert_one_active_row(salary) -> None:
    """Refuse ``salary`` being active while its person already has an active row.

    Each member of staff is paid in full by one branch, so PAYE is worked out
    once on their whole pay. Tunde with N234,567 on an Ikeja row and N54,321 on
    a Lekki row would have each row taxed on its own, and the Lekki part falls
    under the tax-free band. A person changing branch keeps their row and is
    moved on it; the refusal says so.

    Called wherever a row becomes active for a person: added, linked to an
    account, or put back on the payroll. The person's account is locked first,
    so two officers adding the same person at the same moment cannot both
    succeed, which also holds on a database still waiting for the constraint
    (see :class:`~vs_finance.models.EmployeeSalary`). A row with no person
    linked, or an inactive row, is never refused.
    """
    from django.contrib.auth import get_user_model

    from .models import EmployeeSalary

    if not salary.is_active or salary.employee_id is None:
        return
    list(get_user_model().objects.select_for_update().filter(pk=salary.employee_id))
    other = (
        EmployeeSalary.objects.filter(employee_id=salary.employee_id, is_active=True)
        .exclude(pk=salary.pk).select_related("entity").order_by("pk").first()
    )
    if other is None:
        return
    from vs_tenants.models import Branch

    branch = Branch.objects.filter(pk=owning_branch_id(other)).values_list("name", flat=True).first()
    where = f"at {branch}" if branch else "without a branch"
    raise ValidationError({"employee": (
        f"{other.name} is already on the payroll {where}. Each person is paid by one "
        f"branch: to move them, change the branch on their existing salary record "
        f"instead of adding another."
    )})


def _terms_snapshot(source) -> dict:
    return {name: getattr(source, name) for name in TERM_FIELDS}


def _jsonable(values: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in values.items()}


@transaction.atomic
def change_terms(salary, values: dict, *, effective_from=None, reason="", actor_user=None):
    """Write a new version of ``salary``'s pay terms from ``values``, audited.

    ``values`` maps fields of :data:`TERM_FIELDS` (ids for the foreign keys) to
    their new value; the rest carry over from the terms in force. Undated, the
    change takes effect from the first payroll month not yet paid
    (:func:`default_effective_from`). A row with no history yet first records its
    current terms as the version "from the start", so the months before the
    change keep reading what they were paid on. Returns the new version, or None
    when nothing changed.
    """
    from .models import PAYROLL_HISTORY_START, EmployeeSalaryVersion

    salary = type(salary).objects.select_for_update().get(pk=salary.pk)
    current = salary.terms_on(datetime.date.max) or salary
    after = {**_terms_snapshot(current), **values}
    before = _terms_snapshot(current)
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    if not changed:
        return None

    effective_from = effective_from or default_effective_from(salary)
    assert_effective_date_open(salary, effective_from)
    if "branch_id" in changed:
        assert_move_reaches_a_run(salary, after["branch_id"], effective_from)
    assert_branch_required(salary.entity, after["branch_id"], salary.is_active)

    if not salary.versions.exists():
        EmployeeSalaryVersion.objects.create(
            salary=salary, effective_from=PAYROLL_HISTORY_START,
            reason="Terms in force before salary history was kept.", **before,
        )
    version = EmployeeSalaryVersion.objects.create(
        salary=salary, effective_from=effective_from, reason=reason[:255],
        created_by=actor_user, **after,
    )
    _mirror_latest(salary)
    record(
        entity=salary.entity, action=FinanceAuditAction.SALARY_CHANGED, actor_user=actor_user,
        target=salary, branch=owning_branch_id(salary),
        message=f"Changed {salary.name}'s pay terms from {effective_from.isoformat()}.",
        before=_jsonable({k: before.get(k) for k in changed}),
        after=_jsonable(changed), effective_from=effective_from.isoformat(),
        version_id=version.pk, reason=reason[:255],
    )
    return version


def _mirror_latest(salary) -> None:
    """Copy the latest version's terms onto the salary row's own columns."""
    latest = salary.versions.order_by("-effective_from", "-id").first()
    if latest is None:
        return
    for name in TERM_FIELDS:
        setattr(salary, name, getattr(latest, name))
    salary.save(update_fields=[
        "branch", "structure", "gross_amount", "paye_amount", "pension_amount",
        "cost_center", "residence_state", "updated_at",
    ])


def record_creation(salary, *, effective_from=None, actor_user=None) -> None:
    """Record a new roster row's first terms as a version and audit the hire.

    Undated, the terms apply "from the start": a person added today is paid by
    whichever month's run is raised next, as a person always has been.
    """
    from .models import PAYROLL_HISTORY_START, EmployeeSalaryVersion

    assert_branch_required(salary.entity, salary.branch_id, salary.is_active)
    effective_from = effective_from or PAYROLL_HISTORY_START
    EmployeeSalaryVersion.objects.create(
        salary=salary, effective_from=effective_from, created_by=actor_user,
        reason="Added to the payroll.", **_terms_snapshot(salary),
    )
    record(
        entity=salary.entity, action=FinanceAuditAction.SALARY_CREATED, actor_user=actor_user,
        target=salary, branch=owning_branch_id(salary),
        message=f"Added {salary.name} to the payroll.",
        after=_jsonable({**_terms_snapshot(salary), **{k: getattr(salary, k) for k in PROFILE_FIELDS}}),
        effective_from=effective_from.isoformat(),
    )


def record_profile_change(salary, before: dict, *, actor_user=None) -> None:
    """Audit a change to the statutory profile kept on the salary row itself."""
    after = {k: getattr(salary, k) for k in before}
    changed = {k: v for k, v in after.items() if before[k] != v}
    if not changed:
        return
    record(
        entity=salary.entity, action=FinanceAuditAction.SALARY_CHANGED, actor_user=actor_user,
        target=salary, branch=owning_branch_id(salary),
        message=f"Changed {salary.name}'s payroll details.",
        before=_jsonable({k: before[k] for k in changed}), after=_jsonable(changed),
    )


def record_override_change(salary, before_amount, before_reason, *, actor_user=None) -> None:
    """Audit setting, changing or clearing a person's PAYE override.

    An override replaces the computed PAYE on every run until it is cleared, so
    it is recorded on its own with its reason, apart from ordinary edits. The
    amount and reason travel in ``before``/``after`` under the salary row's
    field names, where the trail applies Field Access, and never in the
    message, which is mirrored to the platform trail and cannot be filtered.
    """
    if before_amount == salary.paye_override and before_reason == salary.paye_override_reason:
        return
    if salary.paye_override is None:
        message = f"Cleared {salary.name}'s PAYE override; PAYE is computed again."
    else:
        message = f"Set {salary.name}'s PAYE by override."
    record(
        entity=salary.entity, action=FinanceAuditAction.PAYE_OVERRIDE_CHANGED,
        actor_user=actor_user, target=salary, branch=owning_branch_id(salary), message=message,
        before={"paye_override": before_amount, "paye_override_reason": before_reason},
        after={"paye_override": salary.paye_override,
               "paye_override_reason": salary.paye_override_reason},
    )


@transaction.atomic
def deactivate_salary(salary, *, actor_user=None):
    """Take a person off the payroll without deleting their history, audited."""
    if not salary.is_active:
        return salary
    salary.is_active = False
    salary.save(update_fields=["is_active", "updated_at"])
    record(
        entity=salary.entity, action=FinanceAuditAction.SALARY_DEACTIVATED,
        actor_user=actor_user, target=salary, branch=owning_branch_id(salary),
        message=f"Took {salary.name} off the payroll.",
    )
    return salary


# --------------------------------------------------------------------------- #
# Structure history                                                           #
# --------------------------------------------------------------------------- #

def _structure_last_paid_end(structure):
    """The end of the latest payroll period a live run paid anybody on ``structure`` in."""
    from .models import EmployeeSalary

    users = EmployeeSalary.objects.filter(
        Q(structure=structure) | Q(versions__structure=structure),
    ).values("pk")
    latest = (
        _live_lines().filter(salary__in=users)
        .order_by("-run__pay_date").values_list("run__pay_date", flat=True).first()
    )
    if latest is None:
        return None
    return payroll_period(structure.entity, latest)[1]


def _component_row(component) -> dict:
    return {
        "name": component.name, "kind": component.kind, "calc_method": component.calc_method,
        "rate_bps": component.rate_bps, "amount": component.amount,
        "is_basic": component.is_basic, "is_pensionable": component.is_pensionable,
        "is_taxable": component.is_taxable, "statutory_type": component.statutory_type,
        "sequence": component.sequence,
    }


@transaction.atomic
def replace_components(structure, rows, *, effective_from=None, actor_user=None, creating=False):
    """Close ``structure``'s current lines and write ``rows`` effective from a date, audited.

    ``rows`` are unsaved :class:`SalaryComponent` objects. Undated, the new lines
    take effect from the first payroll month nobody on the structure has been paid
    for yet; a date inside a month already paid is refused. A structure nobody
    has been paid on takes its new lines "from the start". The closed lines stay,
    so the months they priced can be read and recomputed.
    """
    from vs_config.display import format_date

    from .models import PAYROLL_HISTORY_START, SalaryComponent

    end = _structure_last_paid_end(structure)
    if effective_from is None:
        effective_from = PAYROLL_HISTORY_START if end is None else _next_day(end)
    elif end is not None and effective_from <= end:
        tenant = structure.entity.tenant
        raise ValidationError({"effective_from": (
            f"Staff on '{structure.name}' have already been paid up to "
            f"{format_date(end, tenant)}. Date the change from "
            f"{format_date(_next_day(end), tenant)} or later."
        )})
    current = list(structure.components.filter(effective_to__isnull=True))
    before = [_component_row(c) for c in current]
    if current:
        structure.components.filter(pk__in=[c.pk for c in current]).update(
            effective_to=effective_from - datetime.timedelta(days=1),
        )
    for row in rows:
        row.structure = structure
        row.effective_from = effective_from
        row.created_by = actor_user
    SalaryComponent.objects.bulk_create(rows)
    after = [_component_row(c) for c in rows]
    if creating or before != after:
        record(
            entity=structure.entity, action=FinanceAuditAction.SALARY_STRUCTURE_CHANGED,
            actor_user=actor_user, target=structure,
            message=(
                f"Set up salary structure '{structure.name}'." if creating else
                f"Changed salary structure '{structure.name}' from {effective_from.isoformat()}."
            ),
            before={"components": before}, after={"components": after},
            effective_from=effective_from.isoformat(),
        )
    return effective_from


# --------------------------------------------------------------------------- #
# Remittance schedules                                                        #
# --------------------------------------------------------------------------- #

def remittance_schedule(filing, branch_ids=None) -> list:
    """Each person's part of a PAYE, pension or other payroll return, from their line items.

    A state's revenue service wants each employee's PAYE, and a PFA each member's
    contribution with their PIN; a return holds only the totals. Every payroll
    item is stamped at posting with the payable account it was credited to, and
    the return declares the journals that posted them, so the people behind a
    return are the items on its payable account whose run journals it declares:
    the run's own journal, or, for a run booked per branch, the share journal of
    the item's own branch. A draft return is read from the lines it would declare
    if filed now. ``branch_ids`` narrows to a branch-bound reader's branches.

    Returns one row per payroll line: who, their tax number and PIN, the branch
    and state, the pay date, and the employee's and employer's amounts.
    """
    from django.db.models import F

    from .models import JournalLine, PayrollLineItem, TaxFilingLine
    from .tax_filing import FILED_STATUSES, work_out_return

    if filing.filing_status in FILED_STATUSES:
        entry_ids = set(
            TaxFilingLine.objects.filter(filing=filing)
            .values_list("journal_line__entry_id", flat=True)
        )
    else:
        ids = [line.id for line in work_out_return(filing).lines]
        entry_ids = set(JournalLine.objects.filter(pk__in=ids).values_list("entry_id", flat=True))
    if not entry_ids:
        return []
    account_id = filing.obligation.liability_account_id
    whole = PayrollLineItem.objects.filter(
        liability_account_id=account_id, line__run__journal_id__in=entry_ids,
    )
    split = PayrollLineItem.objects.filter(
        liability_account_id=account_id,
        line__run__branch_shares__journal_id__in=entry_ids,
        line__run__branch_shares__branch_id=F("line__branch_id"),
    )
    # A voided run's accrual and its reversal are both declared and net to nil.
    items = (whole | split).exclude(line__run__run_status=PayrollRunStatus.CANCELLED).distinct()
    if branch_ids is not None:
        items = items.filter(line__branch_id__in=tuple(sorted(branch_ids)))
    rows: dict = {}
    for item in items.select_related(
        "line__run", "line__branch", "line__tax_state", "line__pfa",
    ).order_by("line__run__pay_date", "line__employee_name", "line_id"):
        line = item.line
        row = rows.setdefault(line.pk, {
            "line_id": line.pk, "employee_name": line.employee_name,
            "tax_id": line.tax_id, "pension_pin": line.pension_pin,
            "pfa": line.pfa.name if line.pfa_id else "",
            "tax_state": line.tax_state.name if line.tax_state_id else "",
            "branch_id": line.branch_id, "branch_name": line.branch.name if line.branch_id else "",
            "pay_date": line.run.pay_date.isoformat(),
            "run": line.run.document_number or str(line.run_id),
            "employee_amount": 0, "employer_amount": 0, "total": 0,
        })
        key = "employer_amount" if item.kind == PayrollItemKind.EMPLOYER else "employee_amount"
        row[key] += item.amount
        row["total"] += item.amount
    return list(rows.values())
