"""Payroll runs.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound

from core.response import success_response

from rest_framework.exceptions import ValidationError

from django.db.models import Count, Q
from vs_rbac.scoping import transaction_branch_q
from vs_rbac.scoping import (
    assert_caller_may_change,
    caller_branch_ids,
    caller_may_use_branch,
    shared_write_refusal,
)
from vs_rbac.scoping import resolve_branch as _resolve_branch

from ..constants import SalaryCalcMethod, SalaryComponentKind, StatutoryType
from ..views import resolve_entity
from ..models import (
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
    SalaryComponent,
    SalaryStructure,
)
from ..serializers import (
    EmployeeSalarySerializer,
    PayrollRunSerializer,
    SalaryStructureSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _money,
    _raised_branch,
    _bank_account_in_reach,
    _require_lines,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _transaction_branch,
)
from vs_rbac.scoping import only_branch_id, transaction_branch_scope

# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #


UNASSIGNED_REFS = ("unassigned", "none", "null")

#: What a refused write to a whole-school run names.
SHARED_RUN = "a payroll run for the whole school"


def _runs_in_reach(request, entity):
    """The runs the caller may open: their branches' own, and central ones through their share.

    A run is a transaction, read by its own branch exclusively
    (:func:`vs_rbac.scoping.transaction_branch_q`). A central run names no
    branch because it covers every branch's staff and is booked per branch:
    each :class:`PayrollRunBranch` names one, and that share is the branch's
    money. So a branch-bound reader reaches a central run through a share of
    one of their branches, or through its one journal when every line it pays
    is booked to one of their branches, and is shown only that part
    (:class:`vs_finance.serializers.PayrollRunSerializer`). Lagoon View pays
    Ikeja's and Lekki's staff in one January run: Lekki's bursar opens it and
    sees Lekki's staff and Lekki's totals. A central run with no share of
    theirs, and a central draft, which has no shares until it posts, are not
    found for them, as another branch's run is. A whole-tenant reader is not
    narrowed.
    """
    from ..models import PayrollRunBranch

    qs = PayrollRun.objects.filter(entity=entity)
    reach = caller_branch_ids(request)
    if reach is None:
        return qs
    ids = tuple(sorted(reach))
    shared_through = PayrollRunBranch.objects.filter(branch_id__in=ids).values("run_id")
    return qs.filter(
        Q(branch_id__in=ids)
        | Q(branch__isnull=True, pk__in=shared_through)
        | Q(branch__isnull=True, journal__branch_id__in=ids),
    )


def _run_data(request, run):
    """``run`` serialized for the caller, narrowed to their branches' part of a central run."""
    return PayrollRunSerializer(
        run, context={"request": request, "branch_ids": caller_branch_ids(request)},
    ).data


def _run_bank_account(request, entity, ref, run_branch):
    """The bank account a payroll run is paid from, by id or name, or None.

    A run is paid from its own branch's account, the rule every document follows
    (:func:`_resolve_bank_account`). A central run at a school with several
    branches names no account of its own: it posts one journal per branch, and
    each branch's share is paid from that branch's account when it is paid
    (:func:`vs_finance.payroll.pay_payroll`). At a school with one branch a
    central run is that branch's and takes its account like any other.
    """
    if ref in (None, ""):
        return None
    if run_branch is None and only_branch_id(entity.tenant_id) is None and \
            entity.tenant_id is not None:
        raise ValidationError({"bank_account": (
            "A payroll run for every branch is paid from each branch's own account. "
            "Name the accounts when you pay it."
        )})
    return _resolve_bank_account(
        request, entity, ref, required=False,
        document_branch=run_branch, noun="payroll run")


def _refuse_central_run_for_branch_caller(request, entity) -> None:
    """A run covering every branch's staff is raised only by a whole-school caller.

    Mrs Bello keeps Ikeja's payroll at Corona. A run for all staff would put
    Lekki's and Yaba's salaries in front of them, and it names no branch, so once
    raised it would be out of their reach anyway. At a school with one branch their
    grant covers the whole school and they are not refused.
    """
    from rest_framework.exceptions import PermissionDenied

    if transaction_branch_scope(request).is_narrowed:
        raise PermissionDenied(
            "A payroll run for all staff is raised by someone who covers the whole "
            "school. Raise one for your branch instead.",
        )


def _line_branch(request, entity, run_branch, ref, where):
    """The branch a hand-typed line of a central run is booked to, or None.

    A branch run's lines are its branch's. A central run's line may name its
    branch; one that does not takes it from the employee's salary row, or the
    school's only branch, when the run posts (:func:`vs_finance.payroll.post_payroll`).
    """
    if run_branch is not None or ref in (None, ""):
        return run_branch
    branch = _resolve_branch(entity.tenant, ref, where)
    if branch is None or not caller_may_use_branch(request, branch):
        raise ValidationError({where: "No such branch for this entity."})
    return branch


# Support the branch filter workflow.
def _filter_by_branch(qs, request, entity, *, field: str = "branch"):
    """Narrow *qs* by a ``?branch=`` parameter, or leave it alone.

    One helper for the roster and the runs list because the parameter has to
    mean the same thing on both. ``?branch=unassigned`` finds the people no
    branch owns - the ones blocking a school's switch to per-branch payroll -
    and on the runs list the central runs raised before it switched. Spelled out
    rather than left blank, because a blank parameter is how a frontend says "no
    filter at all", and the two answers are not the same list.

    A branch the caller may not work in is reported exactly like one that does
    not exist, so the parameter cannot be used to enumerate a school's sites.
    """
    branch_ref = request.query_params.get(field)
    if not branch_ref:
        return qs
    if str(branch_ref).lower() in UNASSIGNED_REFS:
        return qs.filter(**{f"{field}__isnull": True})
    branch = _resolve_branch(entity.tenant, branch_ref)
    if branch is None or not caller_may_use_branch(request, branch):
        raise ValidationError({field: "No such branch for this entity."})
    return qs.filter(**{field: branch})


# Group endpoint behavior for Payroll Run List Create View.
class PayrollRunListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) payroll runs for an entity.

    docstring-name: Payroll runs
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = _runs_in_reach(request, entity).select_related("branch").prefetch_related(
            "lines__branch", "branch_shares__branch")
        if (status_val := request.query_params.get("run_status")):
            qs = qs.filter(run_status=status_val)
        qs = _filter_by_branch(qs, request, entity)
        return self.paginate(
            request, qs.order_by("-pay_date", "-id"), PayrollRunSerializer,
            context={"request": request, "branch_ids": caller_branch_ids(request)},
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import compute_payroll, ensure_no_overlapping_run

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        branch = _raised_branch(request, entity, body)
        if branch is None:
            _refuse_central_run_for_branch_caller(request, entity)
        pay_date = _date(body.get("pay_date"), "pay_date", required=True)
        # The same guard as the generated run, at the other door into the same
        # table. Typing the lines by hand rather than drawing them from the roster
        # does not make a second run for the period any less of a double payment.
        # No-ops for a central school, which is not guarded at all.
        ensure_no_overlapping_run(entity, pay_date, branch)
        run = PayrollRun.objects.create(
            entity=entity,
            branch=branch,
            pay_date=pay_date,
            period_label=body.get("period_label", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            bank_account=_run_bank_account(request, entity, body.get("bank_account"), branch),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PayrollLine.objects.create(
                run=run, line_no=i,
                branch=_line_branch(request, entity, branch, ln.get("branch"), f"lines[{i}].branch"),
                employee_name=ln.get("employee_name", ""),
                gross_amount=_money(ln.get("gross_amount", 0), f"lines[{i}].gross_amount"),
                paye_amount=_money(ln.get("paye_amount", 0), f"lines[{i}].paye_amount"),
                pension_amount=_money(ln.get("pension_amount", 0), f"lines[{i}].pension_amount"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        compute_payroll(run)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} created.",
            data=_run_data(request, run), status=201,
        )


# Group endpoint behavior for Payroll Run Summary View.
class PayrollRunSummaryView(_FinanceBase):
    """GET - header KPIs over **all** payroll runs (accurate under pagination).

    ``to_pay`` is the net pay no bank account has paid yet. A run posted one
    journal per branch reads POSTED until its last share is paid, so it counts
    its unpaid shares, not its whole net: once Ikeja's share of January is paid,
    only Lekki's is still awaiting payment.

    A branch-bound reader's figures are their branches' part alone
    (:func:`_runs_in_reach`): the runs they reach, their shares of what is
    unpaid, and their own staff on the latest run. Nothing another branch is
    paid reaches them, even summed into a total.

    docstring-name: Payroll runs
    """

    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Exists, OuterRef, Sum
        from django.db.models.functions import Coalesce

        from ..constants import PayrollRunStatus
        from ..models import PayrollRunBranch

        entity = resolve_entity(request)
        reach = caller_branch_ids(request)
        runs = _runs_in_reach(request, entity)
        posted = runs.filter(run_status=PayrollRunStatus.POSTED)
        unsplit = posted.exclude(Exists(PayrollRunBranch.objects.filter(run=OuterRef("pk"))))
        unpaid_shares = PayrollRunBranch.objects.filter(
            run__in=posted, status=PayrollRunStatus.POSTED)
        if reach is not None:
            unpaid_shares = unpaid_shares.filter(branch_id__in=tuple(sorted(reach)))
        to_pay = (
            unsplit.aggregate(net=Coalesce(Sum("net_total"), 0))["net"]
            + unpaid_shares.aggregate(net=Coalesce(Sum("net_total"), 0))["net"]
        )
        agg = {"runs": runs.count(), "to_pay": to_pay}
        latest = runs.order_by("-pay_date", "-id").first()
        employees, net = 0, 0
        if latest is not None:
            latest_lines = latest.lines.all()
            if reach is not None and latest.branch_id is None:
                latest_lines = latest_lines.filter(branch_id__in=tuple(sorted(reach)))
            figures = latest_lines.aggregate(
                count=Count("id"), net=Coalesce(Sum("net_amount"), 0))
            employees, net = figures["count"], figures["net"]
        from ..payroll import payroll_scope

        return success_response(
            "Payroll summary retrieved.",
            data={
                # How this school runs payroll, so the screen knows whether to
                # ask which branch a new run is for. It is a school setting, but
                # reading it through the config API needs `config.value.view` -
                # a settings key no payroll officer holds - and the alternative
                # was a screen that guesses. Only the scope, never the rest of
                # the school's configuration.
                "payroll_scope": payroll_scope(entity),
                "runs": agg["runs"],
                "employees": employees,
                "net": net,
                "to_pay": agg["to_pay"],
            },
        )


class _PayrollActionBase(_FinanceBase):
    """Resolve one run in the caller's reach; on a write, one they may change.

    A central run is reached by a branch-bound reader through their branch's
    share and shown narrowed to it (:func:`_runs_in_reach`). Posting, paying
    and voiding it act on every branch's share at once, so they need
    whole-tenant reach: a branch-bound caller is refused with a 403
    ``SHARED_RECORD_READ_ONLY`` before anything is posted. A branch run of one
    of the caller's branches is theirs to change.
    """

    def _run(self, request, pk):
        from rest_framework.permissions import SAFE_METHODS

        entity = resolve_entity(request)
        run = _runs_in_reach(request, entity).filter(pk=pk).select_related(
            "branch", "journal").prefetch_related(
            "lines__branch", "branch_shares__branch").first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        if request.method not in SAFE_METHODS:
            assert_caller_may_change(
                request.user, getattr(request, "tenant", None), (run.branch_id,),
                message=shared_write_refusal(SHARED_RUN),
            )
        return entity, run


# Group endpoint behavior for Payroll Run Detail View.
class PayrollRunDetailView(_PayrollActionBase):
    """docstring-name: Payroll runs"""
    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, run = self._run(request, pk)
        return success_response("Payroll run retrieved.", data=_run_data(request, run))


# Group endpoint behavior for Payroll Run Post View.
class PayrollRunPostView(_PayrollActionBase):
    """docstring-name: Post a payroll run"""
    rbac_permission = "finance.payrollrun.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import post_payroll

        _, run = self._run(request, pk)
        post_payroll(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} accrued.",
            data=_run_data(request, run),
        )


# Group endpoint behavior for Payroll Run Pay View.
class PayrollRunPayView(_PayrollActionBase):
    """POST - pay a posted run's net wages.

    A run posted as one journal is paid from ``bank_account``, its branch's own. A
    run posted one journal per branch is paid from ``bank_accounts`` (or a single
    ``bank_account``): each account pays its own branch's share, and the rest
    stay unpaid until their accounts are named.

    docstring-name: Pay a payroll run
    """
    rbac_permission = "finance.payrollrun.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import pay_payroll

        entity, run = self._run(request, pk)
        body = request.data or {}
        bank, banks = None, None
        if run.branch_shares.exists():
            refs = body.get("bank_accounts")
            if refs in (None, ""):
                refs = [body["bank_account"]] if body.get("bank_account") not in (None, "") else []
            if not isinstance(refs, list):
                raise ValidationError({"bank_accounts": "Expected a list of bank accounts."})
            banks = [
                _bank_account_in_reach(request, entity, ref, f"bank_accounts[{i}]")
                for i, ref in enumerate(refs)
            ]
        else:
            branch_id = run.journal.branch_id if run.journal_id else run.branch_id
            bank = _run_bank_account(request, entity, body.get("bank_account"), branch_id)
        pay_payroll(
            run, bank_account=bank, bank_accounts=banks,
            pay_date=_date(body.get("pay_date"), "pay_date"),
            actor_user=request.user,
        )
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} disbursed.",
            data=_run_data(request, run),
        )


# Group endpoint behavior for Payroll Run Cancel View.
class PayrollRunCancelView(_PayrollActionBase):
    """POST - cancel a draft run, or void a posted (un-paid) run by reversing its accrual.

    docstring-name: Cancel a payroll run
    """
    rbac_permission = "finance.payrollrun.post"  # the approver who accrues can void

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import cancel_payroll_run

        _, run = self._run(request, pk)
        cancel_payroll_run(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} cancelled.",
            data=_run_data(request, run),
        )




# --------------------------------------------------------------------------- #
# Employee salary roster                                                      #
# --------------------------------------------------------------------------- #

def _salary_rows(request, entity):
    """The salary rows the caller may read: their own branches' staff only.

    A salary follows its employee's branch, read exclusively like every money
    record (:func:`vs_rbac.scoping.transaction_branch_q`). Mrs Bello keeps Lekki's
    payroll, so they see Lekki's teachers' pay and never an Ikeja teacher's, nor
    the pay of somebody nobody has given a branch yet: that row could be anyone's,
    and placing it is the whole-school bursar's job, who sees every row.
    """
    return EmployeeSalary.objects.filter(transaction_branch_q(request), entity=entity)


# Support the resolve salary workflow.
def _resolve_salary(request, entity, pk):
    """One roster row the caller is entitled to, or 404.

    Narrowed rather than merely entity-scoped, and exclusively, like the list:
    the same rule as :func:`_salary_rows`, for a read and a write alike.
    """
    sal = _salary_rows(request, entity).filter(pk=pk).first()
    if sal is None:
        raise NotFound("Employee salary not found for this entity.")
    return sal


# Support the resolve employee workflow.
def _resolve_employee(entity, raw):
    """Resolve an account id to the person a roster row is for, or None.

    Deliberately a USER id and not a staff-record id. This app is
    domain-neutral: it knows about entities, employees and money, and nothing
    about schools or staff profiles, so a school module cannot be imported here
    to resolve one. A user is the identity every domain already shares, and it
    is what ``EmployeeSalary.employee`` has always pointed at.

    Scoped to the entity's own tenant, so a crafted id cannot attach another
    customer's account to this roster.

    Optional on purpose, and it stays optional. A school may legitimately pay
    somebody who has no account at all - a contractor, a visiting examiner - so
    ``name`` remains the required field and this only ever adds certainty where
    it is available.
    """
    if raw in (None, "", 0, "0"):
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(
        pk=raw, tenant=entity.tenant,
    ).first() if str(raw).isdigit() else None
    if user is None:
        raise ValidationError({"employee": "No such person at this customer."})
    return user


# Support the resolve structure workflow.
def _resolve_structure(entity, raw, *, required=False):
    """Resolve a salary-structure id scoped to the entity, or None."""
    if raw in (None, "", 0, "0"):
        if required:
            raise ValidationError({"structure": "A salary structure is required."})
        return None
    structure = SalaryStructure.objects.filter(entity=entity, pk=raw).first()
    if structure is None:
        raise ValidationError({"structure": "Salary structure not found for this entity."})
    return structure


# Group endpoint behavior for Employee Salary List Create View.
class EmployeeSalaryListCreateView(_FinanceBase):
    """GET (list) / POST (add) employee salaries - the roster a run is generated from.

    Rows are read by the employee's branch, exclusively (:func:`_salary_rows`).
    A new row names its branch as a transaction does
    (:func:`vs_rbac.scoping.raised_transaction_branch`): a pinned officer's hire
    is theirs, a whole-school bursar at a school with several branches names one,
    and a school with one branch files it there without asking. A row with no
    branch at a school with several stops the payroll run it is on from posting,
    so none is created.

    docstring-name: Employee salaries
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = (
            _salary_rows(request, entity)
            .select_related("cost_center", "structure", "branch")
            .prefetch_related("structure__components")
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("search")):
            qs = qs.filter(name__icontains=search)
        qs = _filter_by_branch(qs, request, entity)
        return success_response(
            "Employee salaries retrieved.",
            data=EmployeeSalarySerializer(qs.order_by("name"), many=True,
                                          context={"request": request}).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        employee = _resolve_employee(entity, body.get("employee"))
        name = str(body.get("name", "")).strip()
        if not name and employee is not None:
            # Taken from the account when one is named, so a roster keyed on a
            # person does not also depend on somebody retyping their name the
            # same way twice.
            name = " ".join(
                part for part in (employee.first_name, employee.last_name) if part
            ).strip()
        if not name:
            raise ValidationError({"name": "An employee name is required."})
        sal = EmployeeSalary.objects.create(
            entity=entity, name=name, employee=employee,
            # Every new hire names a branch; see the view's docstring.
            branch=_transaction_branch(request, entity, body),
            structure=_resolve_structure(entity, body.get("structure")),
            gross_amount=_money(body.get("gross_amount", 0), "gross_amount"),
            paye_amount=_money(body.get("paye_amount", 0), "paye_amount"),
            pension_amount=_money(body.get("pension_amount", 0), "pension_amount"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            f"Employee salary for {name} added.",
            data=EmployeeSalarySerializer(sal, context={"request": request}).data, status=201,
        )


# Group endpoint behavior for Employee Salary Detail View.
class EmployeeSalaryDetailView(_FinanceBase):
    """PATCH / DELETE one employee salary.

    ``branch`` places or moves a person under the same rule a new row is filed
    by: a pinned officer's own branch, or any of the school's for a whole-school
    bursar, and a 403 for anybody else's. Nobody can move a person back to no
    branch at a school with several.

    docstring-name: Employee salaries
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        if self.request.method == "DELETE":
            return "finance.salary.delete"
        if self.request.method == "PATCH":
            return "finance.salary.update"
        return "finance.salary.view"

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        entity = resolve_entity(request)
        sal = _resolve_salary(request, entity, pk)
        body = request.data or {}
        if "employee" in body:
            # Linking an existing row to its person is the whole point of
            # FR-015: every row written before this shipped has a null here, and
            # there is no backfill, so this is how a school closes the gap one
            # roster row at a time.
            sal.employee = _resolve_employee(entity, body.get("employee"))
            if sal.employee is not None and not str(body.get("name", "")).strip():
                sal.name = " ".join(
                    part for part in (
                        sal.employee.first_name, sal.employee.last_name,
                    ) if part
                ).strip() or sal.name
        if "name" in body:
            sal.name = str(body["name"]).strip()
        if "branch" in body:
            # Placing and moving people is editable; see the view's docstring.
            sal.branch = _transaction_branch(request, entity, body)
        if "structure" in body:
            sal.structure = _resolve_structure(entity, body.get("structure"))
        for field in ("gross_amount", "paye_amount", "pension_amount"):
            if field in body:
                setattr(sal, field, _money(body.get(field), field))
        if "cost_center" in body:
            sal.cost_center = _resolve_cost_center(entity, body.get("cost_center"), "cost_center")
        if "is_active" in body:
            sal.is_active = _bool(body.get("is_active"), default=sal.is_active)
        sal.save()
        return success_response(
            "Employee salary updated.",
            data=EmployeeSalarySerializer(sal, context={"request": request}).data,
        )

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk):
        entity = resolve_entity(request)
        _resolve_salary(request, entity, pk).delete()
        return success_response("Employee salary removed.", data={})


# Group endpoint behavior for Payroll Run Generate View.
class PayrollRunGenerateView(_FinanceBase):
    """POST - raise a draft payroll run from the active employee-salary roster.

    Central or per branch, whichever the school has chosen. Under CENTRAL the run
    covers the whole roster, names no branch, and is raised only by a caller who
    covers the whole school; it posts one journal per branch. Under PER_BRANCH the
    caller's branch decides which roster rows it covers, and it covers that
    branch's staff and nobody else's, so the same person is never on two runs.

    docstring-name: Generate a payroll run
    """

    rbac_permission = "finance.payrollrun.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import generate_run_from_roster, is_per_branch

        entity = resolve_entity(request)
        body = request.data or {}
        # Under CENTRAL the run covers every branch and names none.
        branch = (
            _raised_branch(request, entity, body) if is_per_branch(entity) else None
        )
        if branch is None:
            _refuse_central_run_for_branch_caller(request, entity)
        run = generate_run_from_roster(
            entity, pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            branch=branch,
            period_label=body.get("period_label", ""), narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")), actor_user=request.user,
        )
        return success_response(
            f"Payroll run {run.document_number} generated from {run.lines.count()} employee(s).",
            data=_run_data(request, run), status=201,
        )


# --------------------------------------------------------------------------- #
# Salary structures (reusable pay templates)                                  #
# --------------------------------------------------------------------------- #

_VALID_KINDS = {SalaryComponentKind.EARNING, SalaryComponentKind.DEDUCTION}
_VALID_METHODS = {
    SalaryCalcMethod.FIXED, SalaryCalcMethod.PERCENT_OF_GROSS, SalaryCalcMethod.PERCENT_OF_BASIC,
}
_VALID_STATUTORY = {StatutoryType.PAYE, StatutoryType.PENSION}


# Support the save components workflow.
def _save_components(structure, raw):
    """Validate and replace a structure's components from a request body list.

    Earnings carry no statutory type; deductions must be PAYE or pension so the run's
    accrual journal stays balanced (``net = gross - paye - pension``).
    """
    if not isinstance(raw, list):
        raise ValidationError({"components": "Expected a list of components."})

    rows = []
    for i, c in enumerate(raw):
        where = f"components[{i}]"
        name = str(c.get("name", "")).strip()
        if not name:
            raise ValidationError({where: "A component name is required."})
        kind = c.get("kind", SalaryComponentKind.EARNING)
        if kind not in _VALID_KINDS:
            raise ValidationError({f"{where}.kind": "Must be EARNING or DEDUCTION."})
        method = c.get("calc_method", SalaryCalcMethod.PERCENT_OF_GROSS)
        if method not in _VALID_METHODS:
            raise ValidationError({f"{where}.calc_method": "Unknown calc method."})

        statutory = StatutoryType.NONE
        if kind == SalaryComponentKind.DEDUCTION:
            statutory = c.get("statutory_type")
            if statutory not in _VALID_STATUTORY:
                raise ValidationError(
                    {f"{where}.statutory_type": "Deductions must be PAYE or PENSION."},
                )

        rate_bps = int(c.get("rate_bps") or 0)
        amount = _money(c.get("amount", 0), f"{where}.amount")
        if method == SalaryCalcMethod.FIXED and amount <= 0:
            raise ValidationError({f"{where}.amount": "Fixed components need a positive amount."})
        if method != SalaryCalcMethod.FIXED and not (0 < rate_bps <= 1_000_000):
            raise ValidationError({f"{where}.rate_bps": "Percent components need a rate in basis points."})

        rows.append(SalaryComponent(
            structure=structure, name=name, kind=kind, calc_method=method,
            rate_bps=rate_bps, amount=amount,
            is_basic=bool(c.get("is_basic", False)) and kind == SalaryComponentKind.EARNING,
            statutory_type=statutory, sequence=int(c.get("sequence", i)),
        ))

    structure.components.all().delete()
    SalaryComponent.objects.bulk_create(rows)


# Group endpoint behavior for Salary Structure List Create View.
class SalaryStructureListCreateView(_FinanceBase):
    """GET (list) / POST (create) reusable salary structures for an entity.

    docstring-name: Salary structures
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = (
            SalaryStructure.objects.filter(entity=entity)
            .prefetch_related("components")
            .annotate(employee_count_annot=Count("employee_salaries", distinct=True))
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Salary structures retrieved.",
            data=SalaryStructureSerializer(qs.order_by("name"), many=True).data,
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A structure name is required."})
        if SalaryStructure.objects.filter(entity=entity, name__iexact=name).exists():
            raise ValidationError({"name": "A structure with this name already exists."})
        structure = SalaryStructure.objects.create(
            entity=entity, name=name,
            description=str(body.get("description", "")).strip(),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        _save_components(structure, body.get("components", []))
        return success_response(
            f"Salary structure '{name}' created.",
            data=SalaryStructureSerializer(structure).data, status=201,
        )


# Group endpoint behavior for Salary Structure Detail View.
class SalaryStructureDetailView(_FinanceBase):
    """GET / PATCH / DELETE one salary structure. docstring-name: Salary structures"""

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.view" if self.request.method == "GET" \
            else "finance.salary.update"

    # Support the structure workflow.
    def _structure(self, request, pk):
        entity = resolve_entity(request)
        structure = SalaryStructure.objects.filter(entity=entity, pk=pk).first()
        if structure is None:
            raise NotFound("Salary structure not found for this entity.")
        return entity, structure

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, structure = self._structure(request, pk)
        return success_response(
            "Salary structure retrieved.", data=SalaryStructureSerializer(structure).data,
        )

    @transaction.atomic
    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        entity, structure = self._structure(request, pk)
        body = request.data or {}
        if "name" in body:
            name = str(body["name"]).strip()
            if not name:
                raise ValidationError({"name": "A structure name is required."})
            if (  # Check whether another salary structure already uses this name.
                SalaryStructure.objects.filter(entity=entity, name__iexact=name)
                .exclude(pk=structure.pk)
                .exists()
            ):  # Start the duplicate-name validation block.
                raise ValidationError({"name": "A structure with this name already exists."})
            structure.name = name
        if "description" in body:
            structure.description = str(body["description"]).strip()
        if "is_active" in body:
            structure.is_active = _bool(body.get("is_active"), default=structure.is_active)
        structure.save()
        if "components" in body:
            _save_components(structure, body.get("components", []))
        structure.refresh_from_db()
        return success_response(
            "Salary structure updated.", data=SalaryStructureSerializer(structure).data,
        )

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk):
        _, structure = self._structure(request, pk)
        if structure.employee_salaries.exists():
            raise ValidationError(
                {"structure": "This structure is assigned to employees; reassign them first."},
            )
        structure.delete()
        return success_response("Salary structure removed.", data={})
