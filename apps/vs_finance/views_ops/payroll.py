"""Payroll runs.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound

from core.response import success_response

from rest_framework.exceptions import ValidationError

from ..views import resolve_entity
from ..models import (
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
)
from ..serializers import (
    EmployeeSalarySerializer,
    PayrollRunSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _money,
    _require_lines,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
)

# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

class PayrollRunListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) payroll runs for an entity.

    docstring-name: Payroll runs
    """

    @property
    def rbac_permission(self):
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PayrollRun.objects.filter(entity=entity).prefetch_related("lines")
        if (status_val := request.query_params.get("run_status")):
            qs = qs.filter(run_status=status_val)
        return success_response(
            "Payroll runs retrieved.",
            data=PayrollRunSerializer(qs.order_by("-pay_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from ..payroll import compute_payroll

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        run = PayrollRun.objects.create(
            entity=entity,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
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


class _PayrollActionBase(_FinanceBase):
    def _run(self, request, pk):
        entity = resolve_entity(request)
        run = PayrollRun.objects.filter(entity=entity, pk=pk).first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        return entity, run


class PayrollRunDetailView(_PayrollActionBase):
    """docstring-name: Payroll runs"""
    rbac_permission = "finance.payrollrun.view"

    def get(self, request, pk):
        _, run = self._run(request, pk)
        return success_response(
            "Payroll run retrieved.", data=PayrollRunSerializer(run).data,
        )


class PayrollRunPostView(_PayrollActionBase):
    """docstring-name: Post a payroll run"""
    rbac_permission = "finance.payrollrun.post"

    def post(self, request, pk):
        from ..payroll import post_payroll

        _, run = self._run(request, pk)
        post_payroll(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} accrued.",
            data=PayrollRunSerializer(run).data,
        )


class PayrollRunPayView(_PayrollActionBase):
    """docstring-name: Pay a payroll run"""
    rbac_permission = "finance.payrollrun.pay"

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




# --------------------------------------------------------------------------- #
# Employee salary roster                                                      #
# --------------------------------------------------------------------------- #

def _resolve_salary(entity, pk):
    sal = EmployeeSalary.objects.filter(entity=entity, pk=pk).first()
    if sal is None:
        raise NotFound("Employee salary not found for this entity.")
    return sal


class EmployeeSalaryListCreateView(_FinanceBase):
    """GET (list) / POST (add) employee salaries — the roster a run is generated from.

    docstring-name: Employee salaries
    """

    @property
    def rbac_permission(self):
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = EmployeeSalary.objects.filter(entity=entity).select_related("cost_center")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("search")):
            qs = qs.filter(name__icontains=search)
        return success_response(
            "Employee salaries retrieved.",
            data=EmployeeSalarySerializer(qs.order_by("name"), many=True,
                                          context={"request": request}).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "An employee name is required."})
        sal = EmployeeSalary.objects.create(
            entity=entity, name=name,
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


class EmployeeSalaryDetailView(_FinanceBase):
    """PATCH / DELETE one employee salary. docstring-name: Employee salaries"""

    @property
    def rbac_permission(self):
        return "finance.payrollrun.view" if self.request.method == "GET" \
            else "finance.payrollrun.create"

    def patch(self, request, pk):
        entity = resolve_entity(request)
        sal = _resolve_salary(entity, pk)
        body = request.data or {}
        if "name" in body:
            sal.name = str(body["name"]).strip()
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

    def delete(self, request, pk):
        entity = resolve_entity(request)
        _resolve_salary(entity, pk).delete()
        return success_response("Employee salary removed.", data={})


class PayrollRunGenerateView(_FinanceBase):
    """POST — raise a draft payroll run from the active employee-salary roster.

    docstring-name: Generate a payroll run
    """

    rbac_permission = "finance.payrollrun.create"

    @transaction.atomic
    def post(self, request):
        from ..payroll import generate_run_from_roster

        entity = resolve_entity(request)
        body = request.data or {}
        run = generate_run_from_roster(
            entity, pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            period_label=body.get("period_label", ""), narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")), actor_user=request.user,
        )
        return success_response(
            f"Payroll run {run.document_number} generated from {run.lines.count()} employee(s).",
            data=PayrollRunSerializer(run).data, status=201,
        )
