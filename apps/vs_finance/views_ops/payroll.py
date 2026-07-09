"""Payroll runs.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from django.db import transaction  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from rest_framework.exceptions import ValidationError  # Import dependency used by this finance module.

from django.db.models import Count  # Import dependency used by this finance module.

from ..constants import SalaryCalcMethod, SalaryComponentKind, StatutoryType  # Import dependency used by this finance module.
from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    EmployeeSalary,  # Finance processing step.
    PayrollLine,  # Finance processing step.
    PayrollRun,  # Finance processing step.
    SalaryComponent,  # Finance processing step.
    SalaryStructure,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    EmployeeSalarySerializer,  # Finance processing step.
    PayrollRunSerializer,  # Finance processing step.
    SalaryStructureSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _bool,  # Finance processing step.
    _date,  # Finance processing step.
    _money,  # Finance processing step.
    _require_lines,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

class PayrollRunListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) payroll runs for an entity.

    docstring-name: Payroll runs
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = PayrollRun.objects.filter(entity=entity).prefetch_related("lines")  # Query finance data from the database.
        if (status_val := request.query_params.get("run_status")):  # Branch when this finance condition is true.
            qs = qs.filter(run_status=status_val)  # Store intermediate finance value.
        return self.paginate(  # Return the computed finance response.
            request, qs.order_by("-pay_date", "-id"), PayrollRunSerializer)  # Finance processing step.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from ..payroll import compute_payroll  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        lines = _require_lines(body)  # Store intermediate finance value.
        run = PayrollRun.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),  # Store intermediate finance value.
            period_label=body.get("period_label", ""),  # Store intermediate finance value.
            narration=body.get("narration", ""),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            bank_account=_resolve_bank_account(  # Store intermediate finance value.
                entity, body.get("bank_account"), required=False),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for i, ln in enumerate(lines, start=1):  # Iterate through finance records.
            PayrollLine.objects.create(  # Query finance data from the database.
                run=run, line_no=i,  # Store intermediate finance value.
                employee_name=ln.get("employee_name", ""),  # Store intermediate finance value.
                gross_amount=_money(ln.get("gross_amount", 0), f"lines[{i}].gross_amount"),  # Store intermediate finance value.
                paye_amount=_money(ln.get("paye_amount", 0), f"lines[{i}].paye_amount"),  # Store intermediate finance value.
                pension_amount=_money(ln.get("pension_amount", 0), f"lines[{i}].pension_amount"),  # Store intermediate finance value.
                cost_center=_resolve_cost_center(  # Store intermediate finance value.
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
            )  # Continue structured finance payload.
        compute_payroll(run)  # Finance processing step.
        run.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payroll run {run.document_number} created.",  # Finance processing step.
            data=PayrollRunSerializer(run).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PayrollRunSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — header KPIs over **all** payroll runs (accurate under pagination).

    docstring-name: Payroll runs
    """

    rbac_permission = "finance.payrollrun.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

        from ..constants import PayrollRunStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        runs = PayrollRun.objects.filter(entity=entity)  # Query finance data from the database.
        agg = runs.aggregate(  # Store intermediate finance value.
            runs=Count("id"),  # Store intermediate finance value.
            to_pay=Coalesce(  # Store intermediate finance value.
                Sum("net_total", filter=Q(run_status=PayrollRunStatus.POSTED)), 0),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        latest = runs.order_by("-pay_date", "-id").first()  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Payroll summary retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "runs": agg["runs"],  # Finance processing step.
                "employees": latest.lines.count() if latest else 0,  # Finance processing step.
                "net": latest.net_total if latest else 0,  # Finance processing step.
                "to_pay": agg["to_pay"],  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class _PayrollActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _run(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        run = PayrollRun.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if run is None:  # Branch when this finance condition is true.
            raise NotFound("Payroll run not found for this entity.")  # Surface validation or finance error.
        return entity, run  # Return the computed finance response.


class PayrollRunDetailView(_PayrollActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Payroll runs"""
    rbac_permission = "finance.payrollrun.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, run = self._run(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Payroll run retrieved.", data=PayrollRunSerializer(run).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PayrollRunPostView(_PayrollActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Post a payroll run"""
    rbac_permission = "finance.payrollrun.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..payroll import post_payroll  # Import dependency used by this finance module.

        _, run = self._run(request, pk)  # Store intermediate finance value.
        post_payroll(run, actor_user=request.user)  # Store intermediate finance value.
        run.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payroll run {run.document_number} accrued.",  # Finance processing step.
            data=PayrollRunSerializer(run).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PayrollRunPayView(_PayrollActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Pay a payroll run"""
    rbac_permission = "finance.payrollrun.pay"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..payroll import pay_payroll  # Import dependency used by this finance module.

        entity, run = self._run(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)  # Store intermediate finance value.
        pay_payroll(  # Finance processing step.
            run, bank_account=bank,  # Store intermediate finance value.
            pay_date=_date(body.get("pay_date"), "pay_date"),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        run.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payroll run {run.document_number} disbursed.",  # Finance processing step.
            data=PayrollRunSerializer(run).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PayrollRunCancelView(_PayrollActionBase):  # Class groups related finance API or service behavior.
    """POST — cancel a draft run, or void a posted (un-paid) run by reversing its accrual.

    docstring-name: Cancel a payroll run
    """
    rbac_permission = "finance.payrollrun.post"  # the approver who accrues can void

    def post(self, request, pk):  # Function handles this finance operation.
        from ..payroll import cancel_payroll_run  # Import dependency used by this finance module.

        _, run = self._run(request, pk)  # Store intermediate finance value.
        cancel_payroll_run(run, actor_user=request.user)  # Store intermediate finance value.
        run.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payroll run {run.document_number} cancelled.",  # Finance processing step.
            data=PayrollRunSerializer(run).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.




# --------------------------------------------------------------------------- #
# Employee salary roster                                                      #
# --------------------------------------------------------------------------- #

def _resolve_salary(entity, pk):  # Function handles this finance operation.
    sal = EmployeeSalary.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
    if sal is None:  # Branch when this finance condition is true.
        raise NotFound("Employee salary not found for this entity.")  # Surface validation or finance error.
    return sal  # Return the computed finance response.


def _resolve_structure(entity, raw, *, required=False):  # Function handles this finance operation.
    """Resolve a salary-structure id scoped to the entity, or None."""
    if raw in (None, "", 0, "0"):  # Branch when this finance condition is true.
        if required:  # Branch when this finance condition is true.
            raise ValidationError({"structure": "A salary structure is required."})  # Surface validation or finance error.
        return None  # Return the computed finance response.
    structure = SalaryStructure.objects.filter(entity=entity, pk=raw).first()  # Query finance data from the database.
    if structure is None:  # Branch when this finance condition is true.
        raise ValidationError({"structure": "Salary structure not found for this entity."})  # Surface validation or finance error.
    return structure  # Return the computed finance response.


class EmployeeSalaryListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (add) employee salaries — the roster a run is generated from.

    docstring-name: Employee salaries
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (  # Store intermediate finance value.
            EmployeeSalary.objects.filter(entity=entity)  # Query finance data from the database.
            .select_related("cost_center", "structure")  # Finance processing step.
            .prefetch_related("structure__components")  # Finance processing step.
        )  # Continue structured finance payload.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(name__icontains=search)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Employee salaries retrieved.",  # Finance processing step.
            data=EmployeeSalarySerializer(qs.order_by("name"), many=True,  # Store intermediate finance value.
                                          context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "An employee name is required."})  # Surface validation or finance error.
        sal = EmployeeSalary.objects.create(  # Query finance data from the database.
            entity=entity, name=name,  # Store intermediate finance value.
            structure=_resolve_structure(entity, body.get("structure")),  # Store intermediate finance value.
            gross_amount=_money(body.get("gross_amount", 0), "gross_amount"),  # Store intermediate finance value.
            paye_amount=_money(body.get("paye_amount", 0), "paye_amount"),  # Store intermediate finance value.
            pension_amount=_money(body.get("pension_amount", 0), "pension_amount"),  # Store intermediate finance value.
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),  # Store intermediate finance value.
            is_active=_bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Employee salary for {name} added.",  # Finance processing step.
            data=EmployeeSalarySerializer(sal, context={"request": request}).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class EmployeeSalaryDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """PATCH / DELETE one employee salary. docstring-name: Employee salaries"""

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        if self.request.method == "DELETE":  # Branch when this finance condition is true.
            return "finance.salary.delete"  # Return the computed finance response.
        if self.request.method == "PATCH":  # Branch when this finance condition is true.
            return "finance.salary.update"  # Return the computed finance response.
        return "finance.salary.view"  # Return the computed finance response.

    def patch(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        sal = _resolve_salary(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if "name" in body:  # Branch when this finance condition is true.
            sal.name = str(body["name"]).strip()  # Store intermediate finance value.
        if "structure" in body:  # Branch when this finance condition is true.
            sal.structure = _resolve_structure(entity, body.get("structure"))  # Store intermediate finance value.
        for field in ("gross_amount", "paye_amount", "pension_amount"):  # Iterate through finance records.
            if field in body:  # Branch when this finance condition is true.
                setattr(sal, field, _money(body.get(field), field))  # Finance processing step.
        if "cost_center" in body:  # Branch when this finance condition is true.
            sal.cost_center = _resolve_cost_center(entity, body.get("cost_center"), "cost_center")  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            sal.is_active = _bool(body.get("is_active"), default=sal.is_active)  # Store intermediate finance value.
        sal.save()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Employee salary updated.",  # Finance processing step.
            data=EmployeeSalarySerializer(sal, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def delete(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        _resolve_salary(entity, pk).delete()  # Finance processing step.
        return success_response("Employee salary removed.", data={})  # Return the computed finance response.


class PayrollRunGenerateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST — raise a draft payroll run from the active employee-salary roster.

    docstring-name: Generate a payroll run
    """

    rbac_permission = "finance.payrollrun.create"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from ..payroll import generate_run_from_roster  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        run = generate_run_from_roster(  # Store intermediate finance value.
            entity, pay_date=_date(body.get("pay_date"), "pay_date", required=True),  # Store intermediate finance value.
            period_label=body.get("period_label", ""), narration=body.get("narration", ""),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")), actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Payroll run {run.document_number} generated from {run.lines.count()} employee(s).",  # Finance processing step.
            data=PayrollRunSerializer(run).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Salary structures (reusable pay templates)                                  #
# --------------------------------------------------------------------------- #

_VALID_KINDS = {SalaryComponentKind.EARNING, SalaryComponentKind.DEDUCTION}  # Store intermediate finance value.
_VALID_METHODS = {  # Store intermediate finance value.
    SalaryCalcMethod.FIXED, SalaryCalcMethod.PERCENT_OF_GROSS, SalaryCalcMethod.PERCENT_OF_BASIC,  # Finance processing step.
}  # Continue structured finance payload.
_VALID_STATUTORY = {StatutoryType.PAYE, StatutoryType.PENSION}  # Store intermediate finance value.


def _save_components(structure, raw):  # Function handles this finance operation.
    """Validate and replace a structure's components from a request body list.

    Earnings carry no statutory type; deductions must be PAYE or pension so the run's
    accrual journal stays balanced (``net = gross - paye - pension``).
    """
    if not isinstance(raw, list):  # Branch when this finance condition is true.
        raise ValidationError({"components": "Expected a list of components."})  # Surface validation or finance error.

    rows = []  # Store intermediate finance value.
    for i, c in enumerate(raw):  # Iterate through finance records.
        where = f"components[{i}]"  # Store intermediate finance value.
        name = str(c.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({where: "A component name is required."})  # Surface validation or finance error.
        kind = c.get("kind", SalaryComponentKind.EARNING)  # Store intermediate finance value.
        if kind not in _VALID_KINDS:  # Branch when this finance condition is true.
            raise ValidationError({f"{where}.kind": "Must be EARNING or DEDUCTION."})  # Surface validation or finance error.
        method = c.get("calc_method", SalaryCalcMethod.PERCENT_OF_GROSS)  # Store intermediate finance value.
        if method not in _VALID_METHODS:  # Branch when this finance condition is true.
            raise ValidationError({f"{where}.calc_method": "Unknown calc method."})  # Surface validation or finance error.

        statutory = StatutoryType.NONE  # Store intermediate finance value.
        if kind == SalaryComponentKind.DEDUCTION:  # Branch when this finance condition is true.
            statutory = c.get("statutory_type")  # Store intermediate finance value.
            if statutory not in _VALID_STATUTORY:  # Branch when this finance condition is true.
                raise ValidationError(  # Surface validation or finance error.
                    {f"{where}.statutory_type": "Deductions must be PAYE or PENSION."},  # Continue structured finance payload.
                )  # Continue structured finance payload.

        rate_bps = int(c.get("rate_bps") or 0)  # Store intermediate finance value.
        amount = _money(c.get("amount", 0), f"{where}.amount")  # Store intermediate finance value.
        if method == SalaryCalcMethod.FIXED and amount <= 0:  # Branch when this finance condition is true.
            raise ValidationError({f"{where}.amount": "Fixed components need a positive amount."})  # Surface validation or finance error.
        if method != SalaryCalcMethod.FIXED and not (0 < rate_bps <= 1_000_000):  # Branch when this finance condition is true.
            raise ValidationError({f"{where}.rate_bps": "Percent components need a rate in basis points."})  # Surface validation or finance error.

        rows.append(SalaryComponent(  # Finance processing step.
            structure=structure, name=name, kind=kind, calc_method=method,  # Store intermediate finance value.
            rate_bps=rate_bps, amount=amount,  # Store intermediate finance value.
            is_basic=bool(c.get("is_basic", False)) and kind == SalaryComponentKind.EARNING,  # Store intermediate finance value.
            statutory_type=statutory, sequence=int(c.get("sequence", i)),  # Store intermediate finance value.
        ))  # Continue structured finance payload.

    structure.components.all().delete()  # Finance processing step.
    SalaryComponent.objects.bulk_create(rows)  # Query finance data from the database.


class SalaryStructureListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) reusable salary structures for an entity.

    docstring-name: Salary structures
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (  # Store intermediate finance value.
            SalaryStructure.objects.filter(entity=entity)  # Query finance data from the database.
            .prefetch_related("components")  # Finance processing step.
            .annotate(employee_count_annot=Count("employee_salaries", distinct=True))  # Store intermediate finance value.
        )  # Continue structured finance payload.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Salary structures retrieved.",  # Finance processing step.
            data=SalaryStructureSerializer(qs.order_by("name"), many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A structure name is required."})  # Surface validation or finance error.
        if SalaryStructure.objects.filter(entity=entity, name__iexact=name).exists():  # Branch when this finance condition is true.
            raise ValidationError({"name": "A structure with this name already exists."})  # Surface validation or finance error.
        structure = SalaryStructure.objects.create(  # Query finance data from the database.
            entity=entity, name=name,  # Store intermediate finance value.
            description=str(body.get("description", "")).strip(),  # Store intermediate finance value.
            is_active=_bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        _save_components(structure, body.get("components", []))  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Salary structure '{name}' created.",  # Finance processing step.
            data=SalaryStructureSerializer(structure).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class SalaryStructureDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET / PATCH / DELETE one salary structure. docstring-name: Salary structures"""

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.salary.view" if self.request.method == "GET" \
            else "finance.salary.update"  # Finance processing step.

    def _structure(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        structure = SalaryStructure.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if structure is None:  # Branch when this finance condition is true.
            raise NotFound("Salary structure not found for this entity.")  # Surface validation or finance error.
        return entity, structure  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        _, structure = self._structure(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Salary structure retrieved.", data=SalaryStructureSerializer(structure).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def patch(self, request, pk):  # Function handles this finance operation.
        entity, structure = self._structure(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if "name" in body:  # Branch when this finance condition is true.
            name = str(body["name"]).strip()  # Store intermediate finance value.
            if not name:  # Branch when this finance condition is true.
                raise ValidationError({"name": "A structure name is required."})  # Surface validation or finance error.
            if (  # Check whether another salary structure already uses this name.
                SalaryStructure.objects.filter(entity=entity, name__iexact=name)  # Query matching salary structures.
                .exclude(pk=structure.pk)  # Ignore the current salary structure.
                .exists()  # Test whether a duplicate structure remains.
            ):  # Start the duplicate-name validation block.
                raise ValidationError({"name": "A structure with this name already exists."})  # Surface validation or finance error.
            structure.name = name  # Store intermediate finance value.
        if "description" in body:  # Branch when this finance condition is true.
            structure.description = str(body["description"]).strip()  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            structure.is_active = _bool(body.get("is_active"), default=structure.is_active)  # Store intermediate finance value.
        structure.save()  # Finance processing step.
        if "components" in body:  # Branch when this finance condition is true.
            _save_components(structure, body.get("components", []))  # Finance processing step.
        structure.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Salary structure updated.", data=SalaryStructureSerializer(structure).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def delete(self, request, pk):  # Function handles this finance operation.
        _, structure = self._structure(request, pk)  # Store intermediate finance value.
        if structure.employee_salaries.exists():  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"structure": "This structure is assigned to employees; reassign them first."},  # Continue structured finance payload.
            )  # Continue structured finance payload.
        structure.delete()  # Finance processing step.
        return success_response("Salary structure removed.", data={})  # Return the computed finance response.
