"""Statutory payroll endpoints: national tax data, voluntary deductions, history, payslips.

Three audiences, three gates:

* **Platform staff** maintain the national data (tax tables, states, PFAs).
  Those endpoints write tables that carry no tenant, so a write needs both a
  platform account (:class:`~vs_rbac.permissions.IsVisionStaff`) and a
  platform-scoped key (``finance.statutory.create`` / ``.update``); reading is
  open to anybody who works in finance, because the tables are public law and a
  bursar needs to see which one priced a month.
* **Payroll staff** of a tenant manage voluntary deductions, read salary and
  structure history, open a person's payslip or tax summary, and read a
  return's remittance schedule, under the payroll and tax keys and the caller's
  branch reach. Every pay figure stays behind Field Access: a PDF cannot hide a
  column, so a payslip or summary is refused to a caller who may not read the
  figures on it.
* **An employee** reads their own payslips and tax summary, and nobody else's,
  with no payroll key at all: the records are about them.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.http import HttpResponse
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import can_read
from vs_rbac.permissions import (
    HasAnyModuleAccess,
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
)
from vs_rbac.scoping import WholeTenantWriteMixin, caller_branch_ids

from ..constants import (
    AccountType,
    FinanceAuditAction,
    PayeReliefBasis,
    PayeReliefKind,
)
from ..models import (
    EmployeeDeduction,
    PayeTaxBand,
    PayeTaxRelief,
    PayeTaxTable,
    PayrollDeductionType,
    PayrollTaxJurisdiction,
    PensionFundAdministrator,
    Payslip,
    SalaryStructure,
)
from ..serializers import (
    EmployeeDeductionSerializer,
    EmployeeSalaryVersionSerializer,
    PayeTaxTableSerializer,
    PayrollDeductionTypeSerializer,
    PayrollTaxJurisdictionSerializer,
    PensionFundAdministratorSerializer,
    PayslipSerializer,
    SalaryComponentSerializer,
)
from ..views import resolve_entity
from .base import _FinanceBase, _bool, _date, _int, _money, _resolve_account
from .payroll import _resolve_salary, _runs_in_reach


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #

#: The line figures a payslip or tax summary prints, as registered field keys.
PAYSLIP_FIELDS = (
    "finance.payrollrun.employee_name", "finance.payrollrun.gross_amount",
    "finance.payrollrun.paye_amount", "finance.payrollrun.pension_amount",
    "finance.payrollrun.net_amount", "finance.payrollrun.components",
)


def _require_pay_figures(request) -> None:
    """Refuse a document that prints pay figures to a caller who may not read them all."""
    hidden = [key for key in PAYSLIP_FIELDS if not can_read(request, key)]
    if hidden:
        raise PermissionDenied(
            "This document shows each person's pay, and your role may not see every "
            "figure on it.",
        )


def _pdf(content: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def _wants_pdf(request) -> bool:
    return str(request.query_params.get("output") or "").lower() == "pdf"


def _year(request) -> int:
    from vs_config.clock import tenant_today

    raw = request.query_params.get("year")
    if raw in (None, ""):
        tenant = getattr(request, "tenant", None)
        return (tenant_today(tenant) if tenant is not None else datetime.date.today()).year
    return _int(raw, "year", minimum=1900, maximum=9999)


def _platform_audit(request, action_type, row, summary, *, before=None, after=None):
    """Write a platform audit event for a change to national payroll data."""
    try:
        from vs_audit.models import AuditModuleKey
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key=AuditModuleKey.FINANCE, action_type=action_type,
            entity_type=f"vs_finance.{type(row).__name__}", entity_id=str(row.pk),
            entity_label=str(row), actor_user=request.user, tenant=getattr(request.user, "tenant", None),
            summary=summary, before_data=before or {}, diff_data=after or {},
            metadata={"finance_action": "PAYROLL_STATUTORY_DATA_CHANGED"},
        )
    except Exception:  # pragma: no cover - the audit mirror never blocks the write
        pass


class _PlatformDataView(APIView):
    """National payroll data: anybody in finance reads it, only platform staff change it."""

    rbac_modules = ["finance"]

    @property
    def rbac_permission(self):
        return "finance.statutory.update" if self.request.method == "PATCH" \
            else "finance.statutory.create"

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [(IsAuthenticatedAndActive & HasAnyModuleAccess)()]
        return [(IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission)()]


# --------------------------------------------------------------------------- #
# National tax tables                                                         #
# --------------------------------------------------------------------------- #

def _validated_bands(raw) -> list:
    """Contiguous annual bands from zero, the top one open-ended."""
    if not isinstance(raw, list) or not raw:
        raise ValidationError({"bands": "Provide the table's bands."})
    bands, expected = [], 0
    for i, row in enumerate(raw):
        where = f"bands[{i}]"
        lower = _money(row.get("lower", 0), f"{where}.lower")
        upper = row.get("upper")
        upper = None if upper in (None, "") else _money(upper, f"{where}.upper")
        rate = _int(row.get("rate_bps"), f"{where}.rate_bps", required=True, minimum=0, maximum=10000)
        if lower != expected:
            raise ValidationError({where: "Each band starts where the one before it ends, from 0."})
        if upper is not None and upper <= lower:
            raise ValidationError({where: "A band ends above where it starts."})
        if upper is None and i != len(raw) - 1:
            raise ValidationError({where: "Only the top band is open-ended."})
        bands.append(PayeTaxBand(sequence=i, lower=lower, upper=upper, rate_bps=rate))
        expected = upper
    if bands[-1].upper is not None:
        raise ValidationError({"bands": "The top band is open-ended (no upper bound)."})
    return bands


def _validated_reliefs(raw) -> list:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationError({"reliefs": "Expected a list of reliefs."})
    reliefs = []
    for i, row in enumerate(raw):
        where = f"reliefs[{i}]"
        kind = str(row.get("kind") or "").upper()
        basis = str(row.get("basis") or PayeReliefBasis.NONE).upper()
        if kind not in PayeReliefKind.values:
            raise ValidationError({f"{where}.kind": f"Use one of {', '.join(PayeReliefKind.values)}."})
        if basis not in PayeReliefBasis.values:
            raise ValidationError({f"{where}.basis": f"Use one of {', '.join(PayeReliefBasis.values)}."})
        code = str(row.get("code") or "").strip()[:24]
        name = str(row.get("name") or "").strip()[:120]
        if not code or not name:
            raise ValidationError({where: "A relief needs a code and a name."})
        cap = row.get("cap_amount")
        reliefs.append(PayeTaxRelief(
            sequence=i, code=code, name=name, kind=kind, basis=basis,
            rate_bps=_int(row.get("rate_bps", 0), f"{where}.rate_bps", minimum=0, maximum=10000) or 0,
            cap_amount=None if cap in (None, "") else _money(cap, f"{where}.cap_amount"),
            floor_amount=_money(row.get("floor_amount", 0), f"{where}.floor_amount"),
        ))
    return reliefs


def _write_rules(table, body) -> None:
    if "bands" in body:
        bands = _validated_bands(body.get("bands"))
        table.bands.all().delete()
        for band in bands:
            band.table = table
        PayeTaxBand.objects.bulk_create(bands)
    if "reliefs" in body:
        reliefs = _validated_reliefs(body.get("reliefs"))
        table.reliefs.all().delete()
        for relief in reliefs:
            relief.table = table
        PayeTaxRelief.objects.bulk_create(reliefs)


def _table_scalars(table, body) -> None:
    for field in ("name", "source_reference", "notes"):
        if field in body:
            setattr(table, field, str(body.get(field) or "").strip())
    if "minimum_tax_rate_bps" in body:
        table.minimum_tax_rate_bps = _int(
            body.get("minimum_tax_rate_bps"), "minimum_tax_rate_bps", minimum=0, maximum=10000) or 0
    if "exempt_income_threshold" in body:
        table.exempt_income_threshold = _money(
            body.get("exempt_income_threshold"), "exempt_income_threshold")
    if "is_active" in body:
        table.is_active = _bool(body.get("is_active"), default=table.is_active)


def _table_data(table):
    fresh = PayeTaxTable.objects.prefetch_related("bands", "reliefs").get(pk=table.pk)
    return PayeTaxTableSerializer(fresh).data


class PayeTaxTableListCreateView(_PlatformDataView):
    """GET the national PAYE tables / POST a new tax year's table (platform staff only).

    A table carries the year's annual bands and reliefs as data. A payroll month
    is priced on its own tax year's table, so the platform adds each year's
    before that year's first payroll.

    docstring-name: PAYE tax tables
    """

    def get(self, request):
        qs = PayeTaxTable.objects.prefetch_related("bands", "reliefs").order_by("country", "-tax_year")
        if (country := request.query_params.get("country")):
            qs = qs.filter(country=str(country).upper())
        return success_response(
            "PAYE tax tables retrieved.", data=PayeTaxTableSerializer(qs, many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from vs_audit.models import AuditActionType

        body = request.data or {}
        country = str(body.get("country") or "NG").upper()[:2]
        year = _int(body.get("tax_year"), "tax_year", required=True, minimum=1900, maximum=9999)
        if PayeTaxTable.objects.filter(country=country, tax_year=year).exists():
            raise ValidationError({"tax_year": f"There is already a {country} table for {year}."})
        table = PayeTaxTable(
            country=country, tax_year=year, name=f"{country} PAYE {year}",
            created_by=request.user, updated_by=request.user,
        )
        _table_scalars(table, body)
        table.save()
        if "bands" not in body:
            raise ValidationError({"bands": "Provide the table's bands."})
        _write_rules(table, body)
        data = _table_data(table)
        _platform_audit(request, AuditActionType.CREATE, table,
                        f"Added the {country} PAYE table for {year}.", after=data)
        return success_response(f"PAYE table for {year} created.", data=data, status=201)


class PayeTaxTableDetailView(_PlatformDataView):
    """GET / PATCH one national PAYE table (platform staff only to change).

    An edit bumps ``revision``. Lines already priced keep the bands and reliefs
    they recorded, so the change reaches the next run generated, never a past
    month's figures.

    docstring-name: PAYE tax tables
    """

    def _table(self, pk):
        table = PayeTaxTable.objects.filter(pk=pk).first()
        if table is None:
            raise NotFound("No such PAYE tax table.")
        return table

    def get(self, request, pk):
        return success_response("PAYE tax table retrieved.", data=_table_data(self._table(pk)))

    @transaction.atomic
    def patch(self, request, pk):
        from vs_audit.models import AuditActionType

        table = self._table(pk)
        body = request.data or {}
        before = _table_data(table)
        _table_scalars(table, body)
        _write_rules(table, body)
        table.revision += 1
        table.updated_by = request.user
        table.save()
        data = _table_data(table)
        _platform_audit(request, AuditActionType.UPDATE, table,
                        f"Changed the {table.country} PAYE table for {table.tax_year}.",
                        before=before, after=data)
        return success_response("PAYE tax table updated.", data=data)


# --------------------------------------------------------------------------- #
# States and PFAs                                                             #
# --------------------------------------------------------------------------- #

class TaxJurisdictionListCreateView(_PlatformDataView):
    """GET the states PAYE is remitted to / POST one (platform staff only).

    docstring-name: PAYE states
    """

    def get(self, request):
        qs = PayrollTaxJurisdiction.objects.order_by("country", "name")
        if (country := request.query_params.get("country")):
            qs = qs.filter(country=str(country).upper())
        return success_response(
            "PAYE states retrieved.", data=PayrollTaxJurisdictionSerializer(qs, many=True).data,
        )

    def post(self, request):
        from vs_audit.models import AuditActionType

        body = request.data or {}
        country = str(body.get("country") or "NG").upper()[:2]
        code = str(body.get("code") or "").strip().upper()[:8]
        name = str(body.get("name") or "").strip()[:80]
        authority = str(body.get("authority_name") or "").strip()[:160]
        if not (code and name and authority):
            raise ValidationError({"code": "A state needs a code, a name and its revenue service."})
        if PayrollTaxJurisdiction.objects.filter(country=country, code=code).exists():
            raise ValidationError({"code": "That state code already exists."})
        row = PayrollTaxJurisdiction.objects.create(
            country=country, code=code, name=name, authority_name=authority,
        )
        _platform_audit(request, AuditActionType.CREATE, row, f"Added PAYE state {name}.")
        return success_response(
            "PAYE state added.", data=PayrollTaxJurisdictionSerializer(row).data, status=201,
        )


class TaxJurisdictionDetailView(_PlatformDataView):
    """PATCH a state's name, revenue service or active flag (platform staff only).

    The code is fixed: it names the tenants' per-state PAYE accounts.

    docstring-name: PAYE states
    """

    def get(self, request, pk):
        row = PayrollTaxJurisdiction.objects.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such PAYE state.")
        return success_response("PAYE state retrieved.", data=PayrollTaxJurisdictionSerializer(row).data)

    def patch(self, request, pk):
        from vs_audit.models import AuditActionType

        row = PayrollTaxJurisdiction.objects.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such PAYE state.")
        body = request.data or {}
        before = PayrollTaxJurisdictionSerializer(row).data
        if "name" in body:
            row.name = str(body["name"] or "").strip()[:80] or row.name
        if "authority_name" in body:
            row.authority_name = str(body["authority_name"] or "").strip()[:160] or row.authority_name
        if "is_active" in body:
            row.is_active = _bool(body.get("is_active"), default=row.is_active)
        row.save()
        data = PayrollTaxJurisdictionSerializer(row).data
        _platform_audit(request, AuditActionType.UPDATE, row, f"Changed PAYE state {row.name}.",
                        before=before, after=data)
        return success_response("PAYE state updated.", data=data)


class PensionFundAdministratorListCreateView(_PlatformDataView):
    """GET the pension fund administrators / POST one (platform staff only).

    docstring-name: Pension fund administrators
    """

    def get(self, request):
        qs = PensionFundAdministrator.objects.order_by("name")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Pension fund administrators retrieved.",
            data=PensionFundAdministratorSerializer(qs, many=True).data,
        )

    def post(self, request):
        from vs_audit.models import AuditActionType

        body = request.data or {}
        code = str(body.get("code") or "").strip().upper()
        name = str(body.get("name") or "").strip()[:160]
        if not code or not name or len(code) > 12 or not code.replace("-", "").isalnum():
            raise ValidationError({"code": "A PFA needs a code (up to 12 letters or digits) and a name."})
        if PensionFundAdministrator.objects.filter(code=code).exists():
            raise ValidationError({"code": "That PFA code already exists."})
        row = PensionFundAdministrator.objects.create(code=code, name=name)
        _platform_audit(request, AuditActionType.CREATE, row, f"Added PFA {name}.")
        return success_response(
            "Pension fund administrator added.",
            data=PensionFundAdministratorSerializer(row).data, status=201,
        )


class PensionFundAdministratorDetailView(_PlatformDataView):
    """PATCH a PFA's name or active flag (platform staff only). The code is fixed.

    docstring-name: Pension fund administrators
    """

    def get(self, request, pk):
        row = PensionFundAdministrator.objects.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such pension fund administrator.")
        return success_response(
            "Pension fund administrator retrieved.", data=PensionFundAdministratorSerializer(row).data,
        )

    def patch(self, request, pk):
        from vs_audit.models import AuditActionType

        row = PensionFundAdministrator.objects.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such pension fund administrator.")
        body = request.data or {}
        before = PensionFundAdministratorSerializer(row).data
        if "name" in body:
            row.name = str(body["name"] or "").strip()[:160] or row.name
        if "is_active" in body:
            row.is_active = _bool(body.get("is_active"), default=row.is_active)
        row.save()
        data = PensionFundAdministratorSerializer(row).data
        _platform_audit(request, AuditActionType.UPDATE, row, f"Changed PFA {row.name}.",
                        before=before, after=data)
        return success_response("Pension fund administrator updated.", data=data)


# --------------------------------------------------------------------------- #
# Voluntary deductions                                                        #
# --------------------------------------------------------------------------- #

def _deduction_account(request, entity, ref):
    """The account a voluntary deduction is credited to: a liability, or a loan receivable.

    A cooperative's savings are owed to the cooperative (a liability). A staff
    loan the tenant made is recovered against the loan receivable it was booked
    to (an asset). Anything else would post a deduction as income or expense.
    """
    account = _resolve_account(request, entity, ref, "liability_account", required=True)
    if account.account_type not in (AccountType.LIABILITY, AccountType.ASSET) or not account.is_postable:
        raise ValidationError({"liability_account": (
            "Choose a postable liability account (what is owed on), or the asset "
            "account a staff loan is receivable in."
        )})
    return account


class PayrollDeductionTypeListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET / POST the tenant's voluntary deduction types (staff loans, cooperative savings).

    A type is shared configuration of the books with its own liability account,
    so creating one needs whole-tenant reach.

    docstring-name: Payroll deduction types
    """

    shared_subject = "the payroll deduction types"

    @property
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" else "finance.salary.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PayrollDeductionType.objects.filter(entity=entity).select_related("liability_account")
        return success_response(
            "Payroll deduction types retrieved.",
            data=PayrollDeductionTypeSerializer(qs.order_by("code"), many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from ..audit import record

        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code") or "").strip().upper()[:24]
        name = str(body.get("name") or "").strip()[:120]
        if not code or not name:
            raise ValidationError({"code": "A deduction type needs a code and a name."})
        if PayrollDeductionType.objects.filter(entity=entity, code=code).exists():
            raise ValidationError({"code": "That code is already used."})
        row = PayrollDeductionType.objects.create(
            entity=entity, code=code, name=name,
            liability_account=_deduction_account(request, entity, body.get("liability_account")),
        )
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_DEDUCTION_CHANGED,
            actor_user=request.user, target=row, message=f"Added payroll deduction type {code}.",
            after={"code": code, "name": name, "liability_account": row.liability_account.code},
        )
        return success_response(
            "Payroll deduction type added.",
            data=PayrollDeductionTypeSerializer(row).data, status=201,
        )


class PayrollDeductionTypeDetailView(WholeTenantWriteMixin, _FinanceBase):
    """PATCH a deduction type's name, account or active flag.

    docstring-name: Payroll deduction types
    """

    shared_subject = "the payroll deduction types"

    @property
    def rbac_permission(self):
        return "finance.salary.update" if self.request.method == "PATCH" else "finance.salary.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        row = PayrollDeductionType.objects.filter(entity=entity, pk=pk).first()
        if row is None:
            raise NotFound("Payroll deduction type not found for this entity.")
        return success_response("Payroll deduction type retrieved.",
                                data=PayrollDeductionTypeSerializer(row).data)

    @transaction.atomic
    def patch(self, request, pk):
        from ..audit import record

        entity = resolve_entity(request)
        row = PayrollDeductionType.objects.filter(entity=entity, pk=pk).first()
        if row is None:
            raise NotFound("Payroll deduction type not found for this entity.")
        body = request.data or {}
        before = PayrollDeductionTypeSerializer(row).data
        if "name" in body:
            row.name = str(body["name"] or "").strip()[:120] or row.name
        if "liability_account" in body:
            row.liability_account = _deduction_account(request, entity, body.get("liability_account"))
        if "is_active" in body:
            row.is_active = _bool(body.get("is_active"), default=row.is_active)
        row.save()
        after = PayrollDeductionTypeSerializer(row).data
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_DEDUCTION_CHANGED,
            actor_user=request.user, target=row, message=f"Changed payroll deduction type {row.code}.",
            before=dict(before), after=dict(after),
        )
        return success_response("Payroll deduction type updated.", data=after)


class EmployeeDeductionListCreateView(_FinanceBase):
    """GET / POST one person's voluntary deductions, within the caller's branch reach.

    docstring-name: Employee deductions
    """

    @property
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" else "finance.salary.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        salary = _resolve_salary(request, entity, pk)
        rows = salary.deductions.select_related("deduction_type").order_by("deduction_type__code", "id")
        return success_response(
            "Employee deductions retrieved.",
            data=EmployeeDeductionSerializer(rows, many=True, context={"request": request}).data,
        )

    @transaction.atomic
    def post(self, request, pk):
        from ..audit import record

        entity = resolve_entity(request)
        salary = _resolve_salary(request, entity, pk)
        body = request.data or {}
        kind = PayrollDeductionType.objects.filter(
            entity=entity, is_active=True, pk=body.get("deduction_type") or 0,
        ).first() if str(body.get("deduction_type") or "").isdigit() else None
        if kind is None:
            raise ValidationError({"deduction_type": "No such active deduction type."})
        amount = _money(body.get("amount"), "amount")
        if amount <= 0:
            raise ValidationError({"amount": "A deduction withholds a positive amount."})
        start, end = _date(body.get("start_date"), "start_date"), _date(body.get("end_date"), "end_date")
        if start and end and end < start:
            raise ValidationError({"end_date": "The end date cannot precede the start."})
        limit = body.get("total_limit")
        row = EmployeeDeduction.objects.create(
            salary=salary, deduction_type=kind, amount=amount, start_date=start, end_date=end,
            total_limit=None if limit in (None, "") else _money(limit, "total_limit"),
            reference=str(body.get("reference") or "").strip()[:64], created_by=request.user,
        )
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_DEDUCTION_CHANGED,
            actor_user=request.user, target=salary, branch=salary.branch_id,
            message=f"Added {kind.name} for {salary.name}.",
            after=dict(EmployeeDeductionSerializer(row).data),
        )
        return success_response(
            "Employee deduction added.",
            data=EmployeeDeductionSerializer(row, context={"request": request}).data, status=201,
        )


class EmployeeDeductionDetailView(_FinanceBase):
    """PATCH or DELETE (stop) one person's voluntary deduction, within branch reach.

    docstring-name: Employee deductions
    """

    @property
    def rbac_permission(self):
        if self.request.method == "DELETE":
            return "finance.salary.delete"
        return "finance.salary.update" if self.request.method == "PATCH" else "finance.salary.view"

    def _row(self, request, pk):
        entity = resolve_entity(request)
        row = EmployeeDeduction.objects.filter(pk=pk, salary__entity=entity).select_related(
            "salary", "deduction_type").first()
        if row is None:
            raise NotFound("Employee deduction not found for this entity.")
        _resolve_salary(request, entity, row.salary_id)
        return entity, row

    @transaction.atomic
    def patch(self, request, pk):
        from ..audit import record

        entity, row = self._row(request, pk)
        body = request.data or {}
        before = dict(EmployeeDeductionSerializer(row).data)
        if "amount" in body:
            row.amount = _money(body.get("amount"), "amount")
        for field in ("start_date", "end_date"):
            if field in body:
                setattr(row, field, _date(body.get(field), field))
        if "total_limit" in body:
            limit = body.get("total_limit")
            row.total_limit = None if limit in (None, "") else _money(limit, "total_limit")
        if "is_active" in body:
            row.is_active = _bool(body.get("is_active"), default=row.is_active)
        row.save()
        after = dict(EmployeeDeductionSerializer(row).data)
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_DEDUCTION_CHANGED,
            actor_user=request.user, target=row.salary, branch=row.salary.branch_id,
            message=f"Changed {row.deduction_type.name} for {row.salary.name}.",
            before=before, after=after,
        )
        return success_response(
            "Employee deduction updated.",
            data=EmployeeDeductionSerializer(row, context={"request": request}).data,
        )

    @transaction.atomic
    def delete(self, request, pk):
        from ..audit import record

        entity, row = self._row(request, pk)
        row.is_active = False
        row.save(update_fields=["is_active", "updated_at"])
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_DEDUCTION_CHANGED,
            actor_user=request.user, target=row.salary, branch=row.salary.branch_id,
            message=f"Stopped {row.deduction_type.name} for {row.salary.name}.",
        )
        return success_response("Employee deduction stopped.", data={})


# --------------------------------------------------------------------------- #
# History                                                                     #
# --------------------------------------------------------------------------- #

class EmployeeSalaryHistoryView(_FinanceBase):
    """GET one person's pay terms, version by version, oldest first.

    docstring-name: Employee salaries
    """

    rbac_permission = "finance.salary.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        salary = _resolve_salary(request, entity, pk)
        versions = salary.versions.select_related(
            "branch", "structure", "cost_center", "residence_state", "created_by",
        ).order_by("effective_from", "id")
        return success_response(
            "Salary history retrieved.",
            data=EmployeeSalaryVersionSerializer(
                versions, many=True, context={"request": request}).data,
        )


class SalaryStructureHistoryView(_FinanceBase):
    """GET every line a structure has had, with the dates each was in force.

    docstring-name: Salary structures
    """

    rbac_permission = "finance.salary.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        structure = SalaryStructure.objects.filter(entity=entity, pk=pk).first()
        if structure is None:
            raise NotFound("Salary structure not found for this entity.")
        rows = structure.components.order_by("effective_from", "sequence", "id")
        return success_response(
            "Salary structure history retrieved.",
            data=SalaryComponentSerializer(rows, many=True).data,
        )


# --------------------------------------------------------------------------- #
# Payslips and tax summaries for payroll staff                                #
# --------------------------------------------------------------------------- #

class PayrollLinePayslipView(_FinanceBase):
    """GET one line's payslip as a PDF (``?output=json`` for its content).

    Reached through the run, so the caller's branch reach applies: a branch-bound
    reader opens only lines of their own branches. Refused to a caller who may not
    read every pay figure on it.

    docstring-name: Payslips
    """

    rbac_permission = "finance.payrollrun.view"

    def get(self, request, pk, line_pk):
        from ..payslips import payslip_context, payslip_pdf_bytes

        entity = resolve_entity(request)
        run = _runs_in_reach(request, entity).filter(pk=pk).first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        line = run.lines.filter(pk=line_pk).select_related(
            "run__entity__tenant", "branch", "tax_state", "pfa", "tax_table",
        ).first()
        reach = caller_branch_ids(request)
        if line is None or (reach is not None and line.branch_id not in reach):
            raise NotFound("Payroll line not found on this run.")
        _require_pay_figures(request)
        if str(request.query_params.get("output") or "").lower() == "json":
            return success_response("Payslip retrieved.", data=payslip_context(line))
        return _pdf(payslip_pdf_bytes(line), f"payslip-{run.pay_date:%Y-%m}-{line.line_no}.pdf")


class EmployeeSalaryTaxSummaryView(_FinanceBase):
    """GET one person's tax year (``?year=``), as JSON or ``?output=pdf``.

    docstring-name: Employee salaries
    """

    rbac_permission = "finance.salary.view"

    def get(self, request, pk):
        from ..payslips import tax_summary, tax_summary_pdf_bytes

        entity = resolve_entity(request)
        salary = _resolve_salary(request, entity, pk)
        _require_pay_figures(request)
        summary = tax_summary(entity, year=_year(request), salary=salary)
        if _wants_pdf(request):
            return _pdf(tax_summary_pdf_bytes(summary), f"tax-summary-{summary['year']}.pdf")
        return success_response("Tax summary retrieved.", data=summary)


class TaxFilingScheduleView(_FinanceBase):
    """GET who is behind a payroll return: each person's PAYE, pension or levy on it.

    For the state revenue service's or PFA's schedule. A branch-bound reader sees
    their own branches' people only; a caller who may not read pay figures is
    refused.

    docstring-name: Tax filings
    """

    rbac_permission = "finance.tax.view"

    def get(self, request, pk):
        from ..payroll_statutory import remittance_schedule
        from .tax import _filings_in_reach

        entity = resolve_entity(request)
        filing = _filings_in_reach(request, entity).filter(pk=pk).select_related(
            "obligation").first()
        if filing is None:
            raise NotFound("Tax filing not found for this entity.")
        _require_pay_figures(request)
        rows = remittance_schedule(filing, branch_ids=caller_branch_ids(request))
        return success_response("Remittance schedule retrieved.", data={
            "obligation": filing.obligation.code,
            "authority_name": filing.obligation.authority_name,
            "rows": rows,
            "employee_total": sum(r["employee_amount"] for r in rows),
            "employer_total": sum(r["employer_amount"] for r in rows),
            "total": sum(r["total"] for r in rows),
        })


# --------------------------------------------------------------------------- #
# An employee's own                                                           #
# --------------------------------------------------------------------------- #

def _own_payslips(request):
    """The caller's own payslips, where their books show payslips in the app."""
    from ..models import FinancePayrollSettings

    hidden = FinancePayrollSettings.objects.filter(payslip_in_app=False).values("entity_id")
    return (
        Payslip.objects.filter(employee=request.user, entity__tenant_id=request.user.tenant_id)
        .exclude(entity_id__in=hidden)
        .select_related("entity", "run", "branch", "line")
    )


class MyPayslipListView(APIView):
    """GET the caller's own payslips, newest first. Nobody else's are ever listed.

    docstring-name: My payslips
    """

    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        rows = _own_payslips(request).order_by("-pay_date", "-id")[:120]
        return success_response("Payslips retrieved.", data=PayslipSerializer(rows, many=True).data)


class MyPayslipDetailView(APIView):
    """GET one of the caller's own payslips, as JSON or ``?output=pdf``.

    docstring-name: My payslips
    """

    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request, pk):
        from ..payslips import payslip_context, payslip_pdf_bytes

        payslip = _own_payslips(request).filter(pk=pk).first()
        if payslip is None:
            raise NotFound("Payslip not found.")
        if _wants_pdf(request):
            return _pdf(payslip_pdf_bytes(payslip.line), f"payslip-{payslip.pay_date:%Y-%m}.pdf")
        return success_response("Payslip retrieved.", data=payslip_context(payslip.line))


class MyTaxSummaryView(APIView):
    """GET the caller's own tax year (``?year=``), per set of books, or ``?output=pdf``.

    A person paid from two sets of books gets one summary each; the PDF is of
    the first unless ``?entity=`` names one.

    docstring-name: My payslips
    """

    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        from ..models import LedgerEntity
        from ..payslips import tax_summary, tax_summary_pdf_bytes

        year = _year(request)
        entity_ids = _own_payslips(request).filter(pay_date__year=year).values("entity_id")
        entities = LedgerEntity.objects.filter(pk__in=entity_ids).order_by("code")
        if (code := request.query_params.get("entity")):
            entities = entities.filter(code=str(code).upper())
        summaries = [tax_summary(e, year=year, employee=request.user) for e in entities]
        if _wants_pdf(request):
            if not summaries:
                raise NotFound(f"No payslips for {year}.")
            return _pdf(tax_summary_pdf_bytes(summaries[0]), f"tax-summary-{year}.pdf")
        return success_response("Tax summary retrieved.", data=summaries)
