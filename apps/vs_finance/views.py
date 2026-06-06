"""REST API for vs_finance — entity-scoped reads, documents and key actions.

Every endpoint is scoped to a :class:`~vs_finance.models.LedgerEntity` (the ledger's
tenant — *never* a School): pass ``?entity=<id or code>``. Master-data and document
lists use the platform's paginated envelope (:class:`core.pagination.XVSPagination`)
and RBAC gate (``finance.<resource>.<action>``); the financial statements are returned
as plain JSON by dedicated report endpoints. Domain errors raised by the services are
rendered by ``core.exceptions.custom_exception_handler`` (the typed-exception path), so
the views stay thin.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin
from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .models import (
    Account,
    FiscalPeriod,
    Invoice,
    JournalEntry,
    LedgerEntity,
)
from .money import format_naira
from .serializers import (
    AccountSerializer,
    FiscalPeriodSerializer,
    InvoiceSerializer,
    JournalEntryDetailSerializer,
    JournalEntryListSerializer,
    LedgerEntitySerializer,
)


# --------------------------------------------------------------------------- #
# Entity scoping                                                              #
# --------------------------------------------------------------------------- #

def resolve_entity(request):
    """Resolve the ``?entity=`` query param (id or code) to a :class:`LedgerEntity`.

    Raises DRF :class:`ValidationError` when missing, :class:`NotFound` when unknown —
    both rendered into the standard error envelope by the custom exception handler.
    """
    raw = request.query_params.get("entity")
    if not raw:
        raise ValidationError({"entity": "An 'entity' query parameter (id or code) is required."})
    qs = LedgerEntity.objects.all()
    entity = (
        qs.filter(pk=int(raw)).first() if str(raw).isdigit()
        else qs.filter(code=str(raw).upper()).first()
    )
    if entity is None:
        raise NotFound(f"No ledger entity matches '{raw}'.")
    return entity


class EntityScopedListMixin:
    """A ListAPIView whose queryset is filtered to the resolved entity via ``entity_qs``."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def get_queryset(self):
        self.entity = resolve_entity(self.request)
        return self.entity_qs(self.entity)

    def entity_qs(self, entity):  # pragma: no cover - overridden
        raise NotImplementedError


def _resolve_period(entity, request, *, param="period"):
    """Resolve an optional ``?period=<id or period_no>`` for this entity, or ``None``."""
    raw = request.query_params.get(param)
    if not raw:
        return None
    qs = FiscalPeriod.objects.filter(entity=entity)
    period = (
        qs.filter(pk=int(raw)).first() if str(raw).isdigit() and int(raw) > 12
        else qs.filter(period_no=int(raw)).order_by("-fiscal_year__year").first()
        if str(raw).isdigit() else None
    )
    if period is None:
        raise NotFound(f"No fiscal period matches '{raw}' for this entity.")
    return period


# --------------------------------------------------------------------------- #
# Master data + documents                                                     #
# --------------------------------------------------------------------------- #

class EntityListView(generics.ListAPIView):
    """GET /finance/entities/ — the ledger entities (sets of books) on the platform."""

    serializer_class = LedgerEntitySerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.entity.view"

    def get_queryset(self):
        qs = LedgerEntity.objects.all().order_by("code")
        if (kind := self.request.query_params.get("kind")):
            qs = qs.filter(kind=kind)
        if (active := self.request.query_params.get("is_active")) is not None:
            if active.lower() in ("true", "false"):
                qs = qs.filter(is_active=active.lower() == "true")
        return qs


class AccountListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/accounts/?entity= — the entity's chart of accounts."""

    serializer_class = AccountSerializer
    rbac_permission = "finance.account.view"

    def entity_qs(self, entity):
        qs = Account.objects.filter(entity=entity).select_related("parent").order_by("code")
        params = self.request.query_params
        if (atype := params.get("account_type")):
            qs = qs.filter(account_type=atype)
        if (postable := params.get("is_postable")) is not None:
            if postable.lower() in ("true", "false"):
                qs = qs.filter(is_postable=postable.lower() == "true")
        return qs


class FiscalPeriodListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/periods/?entity= — the entity's fiscal periods."""

    serializer_class = FiscalPeriodSerializer
    rbac_permission = "finance.period.view"

    def entity_qs(self, entity):
        qs = FiscalPeriod.objects.filter(entity=entity).select_related("fiscal_year")
        if (status_val := self.request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (year := self.request.query_params.get("year")):
            qs = qs.filter(fiscal_year__year=year)
        return qs.order_by("fiscal_year__year", "period_no")


class JournalEntryListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/journals/?entity= — posted/draft journal entries for the entity."""

    serializer_class = JournalEntryListSerializer
    rbac_permission = "finance.journal.view"

    def entity_qs(self, entity):
        qs = JournalEntry.objects.filter(entity=entity).select_related("period")
        params = self.request.query_params
        if (status_val := params.get("status")):
            qs = qs.filter(status=status_val)
        if (source := params.get("source")):
            qs = qs.filter(source=source)
        if (date_from := params.get("date_from")):
            qs = qs.filter(date__gte=date_from)
        if (date_to := params.get("date_to")):
            qs = qs.filter(date__lte=date_to)
        return qs.order_by("-date", "-id")


class JournalEntryDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """GET /finance/journals/<id>/?entity= — one journal entry with its lines."""

    serializer_class = JournalEntryDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.journal.view"
    lookup_field = "id"

    def get_queryset(self):
        entity = resolve_entity(self.request)
        return (
            JournalEntry.objects.filter(entity=entity)
            .select_related("period")
            .prefetch_related("lines__account")
        )


class InvoiceListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/invoices/?entity= — sales invoices for the entity."""

    serializer_class = InvoiceSerializer
    rbac_permission = "finance.invoice.view"

    def entity_qs(self, entity):
        qs = Invoice.objects.filter(entity=entity).select_related("customer")
        params = self.request.query_params
        if (status_val := params.get("status")):
            qs = qs.filter(status=status_val)
        if (pay := params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        return qs.order_by("-invoice_date", "-id")


# --------------------------------------------------------------------------- #
# Actions                                                                     #
# --------------------------------------------------------------------------- #

class JournalPostView(APIView):
    """POST /finance/journals/<id>/post/?entity= — post a draft journal."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.journal.post"

    def post(self, request, id):
        from .posting import post_journal

        entity = resolve_entity(request)
        entry = JournalEntry.objects.filter(entity=entity, id=id).first()
        if entry is None:
            raise NotFound("Journal entry not found for this entity.")
        post_journal(entry, actor_user=request.user)
        entry.refresh_from_db()
        return success_response(
            message=f"Journal {entry.document_number} posted.",
            data=JournalEntryDetailSerializer(entry).data,
        )


class JournalReverseView(APIView):
    """POST /finance/journals/<id>/reverse/?entity= — reverse a posted journal."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.journal.reverse"

    def post(self, request, id):
        from .posting import reverse_journal

        entity = resolve_entity(request)
        entry = JournalEntry.objects.filter(entity=entity, id=id).first()
        if entry is None:
            raise NotFound("Journal entry not found for this entity.")
        reversal = reverse_journal(entry, actor_user=request.user)
        return success_response(
            message=f"Journal {entry.document_number} reversed.",
            data=JournalEntryDetailSerializer(reversal).data,
            status=201,
        )


class PeriodCloseView(APIView):
    """POST /finance/periods/<id>/close/?entity= — run the checklist and close a period.

    Body (all optional): ``{"soft": bool, "force": bool, "run_depreciation": bool}``.
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.period.close"

    def post(self, request, id):
        from .close import close_period

        entity = resolve_entity(request)
        period = FiscalPeriod.objects.filter(entity=entity, id=id).first()
        if period is None:
            raise NotFound("Fiscal period not found for this entity.")

        body = request.data or {}
        period, checklist = close_period(
            entity, period, actor_user=request.user,
            soft=bool(body.get("soft", False)),
            force=bool(body.get("force", False)),
            run_depreciation=bool(body.get("run_depreciation", True)),
        )
        return success_response(
            message=f"Period '{period}' closed to {period.status}.",
            data={
                "period": FiscalPeriodSerializer(period).data,
                "checklist": _serialize_checklist(checklist),
            },
        )


# --------------------------------------------------------------------------- #
# Reports / financial statements                                              #
# --------------------------------------------------------------------------- #

def _money(amount):
    return {"kobo": amount, "naira": format_naira(amount)}


def _serialize_checklist(checklist):
    return {
        "passed": checklist.passed,
        "items": [
            {"name": i.name, "passed": i.passed, "blocking": i.blocking, "detail": i.detail}
            for i in checklist.items
        ],
    }


def _line(row):
    return {
        "account_id": row.account_id, "code": row.code, "name": row.name,
        "account_type": row.account_type, "amount": _money(row.amount),
    }


def _maybe_export(request, table, *, filename):
    """If ``?export=csv|xlsx|pdf`` is set, render ``table`` to a file download.

    Returns an :class:`HttpResponse` attachment, or ``None`` when no export was asked
    for (the caller then returns its normal JSON envelope). An unknown format becomes a
    DRF :class:`ValidationError` (rendered as a 400 by the custom exception handler).

    Note: the parameter is ``export`` (not ``format``) because DRF reserves ``?format=``
    for renderer content negotiation.
    """
    fmt = request.query_params.get("export")
    if not fmt:
        return None
    from .exports import render

    try:
        body, content_type, ext = render(table, fmt)
    except ValueError as exc:
        raise ValidationError({"export": str(exc)})
    resp = HttpResponse(body, content_type=content_type)
    resp["Content-Disposition"] = f'attachment; filename="{filename}.{ext}"'
    return resp


class TrialBalanceView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import trial_balance

        from .exports import ReportTable

        entity = resolve_entity(request)
        period = _resolve_period(entity, request)
        tb = trial_balance(entity, period=period)

        export = _maybe_export(request, ReportTable(
            title="Trial Balance",
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'All periods'}",
            columns=["Code", "Account", "Type", "Debit", "Credit"],
            rows=[[r.code, r.name, r.account_type, r.debit_naira, r.credit_naira] for r in tb.rows],
            summary_rows=[["", "TOTAL", "", format_naira(tb.total_debit), format_naira(tb.total_credit)]],
        ), filename=f"trial_balance_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="Trial balance retrieved.",
            data={
                "entity": entity.code,
                "period": getattr(period, "name", None),
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type,
                        "debit": _money(r.debit), "credit": _money(r.credit),
                    }
                    for r in tb.rows
                ],
                "total_debit": _money(tb.total_debit),
                "total_credit": _money(tb.total_credit),
                "is_balanced": tb.is_balanced,
            },
        )


class IncomeStatementView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import income_statement

        from .exports import ReportTable

        entity = resolve_entity(request)
        period = _resolve_period(entity, request)
        pnl = income_statement(entity, period=period)

        rows = [["Income", r.code, r.name, r.amount_naira] for r in pnl.income_rows]
        rows += [["Expense", r.code, r.name, r.amount_naira] for r in pnl.expense_rows]
        export = _maybe_export(request, ReportTable(
            title="Income Statement",
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'Year to date'}",
            columns=["Section", "Code", "Account", "Amount"],
            rows=rows,
            summary_rows=[
                ["", "", "Total income", format_naira(pnl.total_income)],
                ["", "", "Total expense", format_naira(pnl.total_expense)],
                ["", "", "Net income", format_naira(pnl.net_income)],
            ],
        ), filename=f"income_statement_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="Income statement retrieved.",
            data={
                "entity": entity.code,
                "period": getattr(period, "name", None),
                "income": [_line(r) for r in pnl.income_rows],
                "expense": [_line(r) for r in pnl.expense_rows],
                "total_income": _money(pnl.total_income),
                "total_expense": _money(pnl.total_expense),
                "net_income": _money(pnl.net_income),
            },
        )


class BalanceSheetView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import balance_sheet

        from .exports import ReportTable

        entity = resolve_entity(request)
        as_of = request.query_params.get("as_of") or None
        bs = balance_sheet(entity, as_of=as_of)

        rows = [["Asset", r.code, r.name, r.amount_naira] for r in bs.asset_rows]
        rows += [["Liability", r.code, r.name, r.amount_naira] for r in bs.liability_rows]
        rows += [["Equity", r.code, r.name, r.amount_naira] for r in bs.equity_rows]
        rows += [["Equity", "", "Retained earnings (unclosed)", format_naira(bs.retained_earnings)]]
        export = _maybe_export(request, ReportTable(
            title="Balance Sheet",
            subtitle=f"{entity.code} · as at {bs.as_of}",
            columns=["Section", "Code", "Account", "Amount"],
            rows=rows,
            summary_rows=[
                ["", "", "Total assets", format_naira(bs.total_assets)],
                ["", "", "Total liabilities", format_naira(bs.total_liabilities)],
                ["", "", "Total equity", format_naira(bs.total_equity)],
            ],
        ), filename=f"balance_sheet_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="Balance sheet retrieved.",
            data={
                "entity": entity.code,
                "as_of": str(bs.as_of),
                "assets": [_line(r) for r in bs.asset_rows],
                "liabilities": [_line(r) for r in bs.liability_rows],
                "equity": [_line(r) for r in bs.equity_rows],
                "total_assets": _money(bs.total_assets),
                "total_liabilities": _money(bs.total_liabilities),
                "retained_earnings": _money(bs.retained_earnings),
                "total_equity": _money(bs.total_equity),
                "is_balanced": bs.is_balanced,
            },
        )


class CashFlowView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import cash_flow_statement

        from .exports import ReportTable

        entity = resolve_entity(request)
        period = _resolve_period(entity, request)
        cf = cash_flow_statement(entity, period=period)

        export = _maybe_export(request, ReportTable(
            title="Cash Flow Statement",
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'Year to date'}",
            columns=["Activity", "Amount"],
            rows=[
                ["Operating activities", format_naira(cf.by_activity["operating"])],
                ["Investing activities", format_naira(cf.by_activity["investing"])],
                ["Financing activities", format_naira(cf.by_activity["financing"])],
            ],
            summary_rows=[
                ["Net change in cash", format_naira(cf.net_change)],
                ["Opening cash", format_naira(cf.opening_cash)],
                ["Closing cash", format_naira(cf.closing_cash)],
            ],
        ), filename=f"cash_flow_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="Cash flow statement retrieved.",
            data={
                "entity": entity.code,
                "period": getattr(period, "name", None),
                "opening_cash": _money(cf.opening_cash),
                "closing_cash": _money(cf.closing_cash),
                "by_activity": {k: _money(v) for k, v in cf.by_activity.items()},
                "net_change": _money(cf.net_change),
                "is_reconciled": cf.is_reconciled,
            },
        )


class ARAgingView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import ar_aging

        from .reports import AGING_BUCKETS
        from .exports import ReportTable

        entity = resolve_entity(request)
        as_of = request.query_params.get("as_of") or None
        report = ar_aging(entity, as_of=as_of)

        columns = ["Code", "Customer"] + list(AGING_BUCKETS) + ["Net"]
        rows = [
            [r.code, r.name] + [format_naira(r.buckets[b]) for b in AGING_BUCKETS]
            + [format_naira(r.net)]
            for r in report.rows
        ]
        summary = ["", "TOTAL"] + [format_naira(report.bucket_totals[b]) for b in AGING_BUCKETS]
        summary += [format_naira(report.total_net)]
        export = _maybe_export(request, ReportTable(
            title="Accounts Receivable Aging",
            subtitle=f"{entity.code} · as at {report.as_of}",
            columns=columns,
            rows=rows,
            summary_rows=[summary],
        ), filename=f"ar_aging_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="AR aging retrieved.",
            data={
                "entity": entity.code,
                "as_of": str(report.as_of),
                "rows": [
                    {
                        "customer_id": r.customer_id, "code": r.code, "name": r.name,
                        "buckets": {b: _money(v) for b, v in r.buckets.items()},
                        "outstanding": _money(r.outstanding),
                        "unallocated_credit": _money(r.unallocated_credit),
                        "net": _money(r.net),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _money(v) for b, v in report.bucket_totals.items()},
                "total_net": _money(report.total_net),
            },
        )


class ARReconciliationView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import reconcile_ar

        entity = resolve_entity(request)
        as_of = request.query_params.get("as_of") or None
        rec = reconcile_ar(entity, as_of=as_of)
        return success_response(
            message="AR reconciliation retrieved.",
            data={
                "entity": entity.code,
                "subledger_total": _money(rec.subledger_total),
                "control_total": _money(rec.control_total),
                "difference": _money(rec.difference),
                "is_reconciled": rec.is_reconciled,
            },
        )
