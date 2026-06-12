"""Payroll runs.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound

from core.response import success_response

from ..views import resolve_entity
from ..models import (
    PayrollLine,
    PayrollRun,
)
from ..serializers import (
    PayrollRunSerializer,
)


from .base import (
    _FinanceBase,
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


