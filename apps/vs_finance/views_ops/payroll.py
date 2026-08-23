"""Payroll runs.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound

from core.response import success_response

from rest_framework.exceptions import ValidationError

from django.db.models import Count
from vs_rbac.scoping import branch_q  # include_shared spelled out per call site
from vs_rbac.scoping import caller_may_use_branch
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
    _require_lines,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
)

# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #


# Support the branch rule workflow.
def _branch_rule(entity) -> dict:
    """How :func:`_raised_branch` should treat a caller entitled to several branches.

    The one thing the payroll screens read the school's ``payroll.scope`` setting
    for, and the reason it is a helper rather than a literal at each call site: the
    two payroll write paths must not be able to drift apart on it.

    Under **CENTRAL** the answer is ``shared_when_ambiguous=True``, which is
    precisely what payroll did before per-branch runs existed. A run covers
    everybody the school employs, so a payroll officer covering Ikeja and Lekki who
    names no site meant "the school", and asking her to pick would be asking her to
    narrow a run that is not narrowed. Nothing about a central school's payroll
    changes.

    Under **PER_BRANCH** the answer is the ordinary finance rule: ask her. The two
    runs pay different people, only she knows which one she is raising, and
    guessing "the school" would raise a run that pays Yaba's staff as well - which
    is the one thing she is not entitled to do.
    """
    from ..payroll import is_per_branch

    return {"shared_when_ambiguous": not is_per_branch(entity)}


UNASSIGNED_REFS = ("unassigned", "none", "null")


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
        qs = PayrollRun.objects.filter(
            branch_q(request, include_shared=True), entity=entity,
        ).select_related("branch").prefetch_related("lines")
        if (status_val := request.query_params.get("run_status")):
            qs = qs.filter(run_status=status_val)
        qs = _filter_by_branch(qs, request, entity)
        return self.paginate(
            request, qs.order_by("-pay_date", "-id"), PayrollRunSerializer)

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import compute_payroll, ensure_no_overlapping_run

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        branch = _raised_branch(request, entity, body, **_branch_rule(entity))
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
            bank_account=_resolve_bank_account(
                entity, body.get("bank_account"), required=False),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PayrollLine.objects.create(
                run=run, line_no=i,
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
            data=PayrollRunSerializer(run).data, status=201,
        )


# Group endpoint behavior for Payroll Run Summary View.
class PayrollRunSummaryView(_FinanceBase):
    """GET - header KPIs over **all** payroll runs (accurate under pagination).

    docstring-name: Payroll runs
    """

    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Q, Sum
        from django.db.models.functions import Coalesce

        from ..constants import PayrollRunStatus

        entity = resolve_entity(request)
        runs = PayrollRun.objects.filter(branch_q(request, include_shared=True), entity=entity)
        agg = runs.aggregate(
            runs=Count("id"),
            to_pay=Coalesce(
                Sum("net_total", filter=Q(run_status=PayrollRunStatus.POSTED)), 0),
        )
        latest = runs.order_by("-pay_date", "-id").first()
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
                "employees": latest.lines.count() if latest else 0,
                "net": latest.net_total if latest else 0,
                "to_pay": agg["to_pay"],
            },
        )


# Define Payroll Action Base values.
class _PayrollActionBase(_FinanceBase):
    # Support the run workflow.
    def _run(self, request, pk):
        entity = resolve_entity(request)
        run = PayrollRun.objects.filter(
            branch_q(request, include_shared=True), entity=entity, pk=pk,
        ).select_related("branch").first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        return entity, run


# Group endpoint behavior for Payroll Run Detail View.
class PayrollRunDetailView(_PayrollActionBase):
    """docstring-name: Payroll runs"""
    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, run = self._run(request, pk)
        return success_response(
            "Payroll run retrieved.", data=PayrollRunSerializer(run).data,
        )


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
            data=PayrollRunSerializer(run).data,
        )


# Group endpoint behavior for Payroll Run Pay View.
class PayrollRunPayView(_PayrollActionBase):
    """docstring-name: Pay a payroll run"""
    rbac_permission = "finance.payrollrun.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import pay_payroll

        entity, run = self._run(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)
        pay_payroll(
            run, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date"),
            actor_user=request.user,
        )
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} disbursed.",
            data=PayrollRunSerializer(run).data,
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
            data=PayrollRunSerializer(run).data,
        )




# --------------------------------------------------------------------------- #
# Employee salary roster                                                      #
# --------------------------------------------------------------------------- #

# Support the resolve salary workflow.
def _resolve_salary(request, entity, pk):
    """One roster row the caller is entitled to, or 404.

    Narrowed rather than merely entity-scoped. Without this an Ikeja payroll
    officer could rewrite a Lekki teacher's gross pay by guessing a primary key,
    which is the write-side half of the hole ``654e7af`` closed on the read side -
    and pay is the most sensitive column finance has.

    Inclusive, like every other finance read: an unassigned row is visible to
    everybody, because somebody has to be able to assign it, and until it is
    assigned no branch owns it.
    """
    sal = EmployeeSalary.objects.filter(
        branch_q(request, include_shared=True), entity=entity, pk=pk,
    ).first()
    if sal is None:
        raise NotFound("Employee salary not found for this entity.")
    return sal


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
        # ``include_shared=True``: reading the roster is inclusive even though
        # *running* it is exclusive, and the split is deliberate. Somebody has to
        # be able to see an unassigned row in order to assign it, and while it is
        # unassigned no branch owns it; but a branch run must not pay it, because
        # every branch's run would. Seeing a person costs nothing. Paying them
        # three times costs three salaries.
        qs = (
            EmployeeSalary.objects.filter(
                branch_q(request, include_shared=True), entity=entity,
            )
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
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "An employee name is required."})
        sal = EmployeeSalary.objects.create(
            entity=entity, name=name,
            # A pinned officer's new hire is hers; an unpinned bursar's is
            # unassigned until somebody says otherwise, which is what every row on
            # every roster is today. ``_branch_rule`` decides only the officer who
            # covers two branches and names neither: asked under PER_BRANCH,
            # because a row filed unassigned there is a person no run pays, and
            # left alone under CENTRAL, where nothing turns on the answer.
            branch=_raised_branch(request, entity, body, **_branch_rule(entity)),
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
    """PATCH / DELETE one employee salary. docstring-name: Employee salaries"""

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
        if "name" in body:
            sal.name = str(body["name"]).strip()
        if "branch" in body:
            # Assigning people to branches is the work a school does *before* it
            # switches to per-branch payroll, so this has to be editable rather
            # than write-once. Reusing ``_raised_branch`` keeps it under the same
            # rule as every other branch a caller names: her own branch or, if she
            # is unpinned, any of the school's, and a 403 for anyone else's. A
            # pinned officer cannot push somebody back to unassigned, because
            # ``_raised_branch`` reads a blank from her as "mine".
            sal.branch = _raised_branch(request, entity, body, **_branch_rule(entity))
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
    covers the whole roster exactly as it always has. Under PER_BRANCH the caller's
    branch decides which roster rows it covers, and it covers that branch's staff
    and nobody else's, so the same person is never on two runs.

    docstring-name: Generate a payroll run
    """

    rbac_permission = "finance.payrollrun.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import generate_run_from_roster, is_per_branch

        entity = resolve_entity(request)
        body = request.data or {}
        # A central school's generated run gets no branch at all - not even a
        # pinned officer's - because that is what this endpoint did before
        # per-branch payroll existed, and stamping one now would be a change with
        # no purpose: the run covers the whole roster either way. Reading the
        # setting first also means a central school never meets the "which branch
        # do you mean?" refusal, so nothing here can start failing for a school
        # that has not opted in.
        branch = (
            _raised_branch(request, entity, body) if is_per_branch(entity) else None
        )
        run = generate_run_from_roster(
            entity, pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            branch=branch,
            period_label=body.get("period_label", ""), narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")), actor_user=request.user,
        )
        return success_response(
            f"Payroll run {run.document_number} generated from {run.lines.count()} employee(s).",
            data=PayrollRunSerializer(run).data, status=201,
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
