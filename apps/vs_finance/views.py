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
    DirectEntryCreateSerializer,
    LedgerEntityCreateSerializer,
    LedgerEntitySerializer,
)


# --------------------------------------------------------------------------- #
# Entity scoping                                                              #
# --------------------------------------------------------------------------- #

def resolve_entity(request):
    """Resolve the ``?entity=`` query param (id or code) to a :class:`LedgerEntity`.

    Authorization: holding a finance permission key is NOT enough — the caller
    must also be entitled to this specific entity's books. CX staff may access
    every entity; school-scoped users only entities sourced from their school.
    Unknown and forbidden entities both return NotFound so an outsider can't
    probe which entity codes exist.

    Raises DRF :class:`ValidationError` when missing, :class:`NotFound` when
    unknown/forbidden — both rendered into the standard error envelope by the
    custom exception handler.
    """
    raw = request.query_params.get("entity")
    if not raw:
        raise ValidationError({"entity": "An 'entity' query parameter (id or code) is required."})
    qs = LedgerEntity.objects.all()

    user = getattr(request, "user", None)
    if getattr(user, "user_type", None) != "CX_STAFF":
        school = getattr(request, "school", None) or getattr(user, "school", None)
        if school is None:
            raise NotFound(f"No ledger entity matches '{raw}'.")
        qs = qs.filter(source_school=school)

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

class EntityListCreateView(generics.ListCreateAPIView):
    """GET /finance/entities/ — the ledger entities (sets of books) on the platform.

    POST /finance/entities/ — provision a **new** set of books. Entity creation is a
    structural, platform-level operation (a new entity becomes the tenant of its own
    documents and numbering), so it is gated on the dedicated ``finance.entity.create``
    key, which is granted only to the platform admin roles.

    docstring-name: Ledger entities
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return ("finance.entity.create" if self.request.method == "POST"
                else "finance.entity.view")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return LedgerEntityCreateSerializer
        return LedgerEntitySerializer

    def get_queryset(self):
        qs = LedgerEntity.objects.all().order_by("code")
        if (kind := self.request.query_params.get("kind")):
            qs = qs.filter(kind=kind)
        if (active := self.request.query_params.get("is_active")) is not None:
            if active.lower() in ("true", "false"):
                qs = qs.filter(is_active=active.lower() == "true")
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entity = serializer.save()
        return success_response(
            f"Ledger entity {entity.code} created.",
            data=serializer.data, status=201,
        )


class AccountListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/accounts/?entity= — the entity's chart of accounts.

    ``?with_balance=true`` returns the **whole tree** (un-paginated) with each
    account's net GL ``balance`` and sub-ledger ``tag`` (CONTROL / CASH) — for the
    Chart-of-Accounts screen. Without it, the plain paginated list is served (used
    by the account pickers).

    docstring-name: Chart of accounts
    """

    serializer_class = AccountSerializer

    @property
    def rbac_permission(self):
        return "finance.account.create" if self.request.method == "POST" else "finance.account.view"

    def _with_balance(self):
        return self.request.query_params.get("with_balance") == "true"

    def post(self, request):
        """Create a new chart-of-accounts node for the entity."""
        from .constants import AccountType
        from .models import Account

        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "An account code is required."})
        if Account.objects.filter(entity=entity, code=code).exists():
            raise ValidationError({"code": f"Account '{code}' already exists in this entity."})
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "An account name is required."})
        atype = body.get("account_type")
        if atype not in AccountType.values:
            raise ValidationError({"account_type": "Choose a valid account type."})
        parent = None
        if body.get("parent"):
            parent = Account.objects.filter(entity=entity, pk=body.get("parent")).first()
            if parent is None:
                raise ValidationError({"parent": "No such parent account in this entity."})
        # normal_balance is derived from type/contra by Account.save() when left blank.
        account = Account.objects.create(
            entity=entity, code=code, name=name, account_type=atype, parent=parent,
            is_contra=bool(body.get("is_contra", False)),
            is_postable=bool(body.get("is_postable", True)),
            subtype=str(body.get("subtype", "")).strip(),
            description=str(body.get("description", "")).strip(),
        )
        return success_response(
            f"Account {account.code} created.", data=AccountSerializer(account).data, status=201,
        )

    def entity_qs(self, entity):
        qs = Account.objects.filter(entity=entity).select_related("parent").order_by("code")
        params = self.request.query_params
        if self._with_balance():
            from django.db.models import F, Sum
            from django.db.models.functions import Coalesce
            qs = qs.annotate(
                _bal_dr=Coalesce(Sum(F("balances__opening_debit") + F("balances__debit_total")), 0),
                _bal_cr=Coalesce(Sum(F("balances__opening_credit") + F("balances__credit_total")), 0),
            )
        if (atype := params.get("account_type")):
            qs = qs.filter(account_type=atype)
        if (postable := params.get("is_postable")) is not None:
            if postable.lower() in ("true", "false"):
                qs = qs.filter(is_postable=postable.lower() == "true")
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self._with_balance():
            entity = getattr(self, "entity", None) or resolve_entity(self.request)
            from .constants import CASH_BANK_CODE
            from .models import Customer
            control = set(
                Customer.objects.filter(entity=entity).exclude(receivable_account=None)
                .values_list("receivable_account_id", flat=True)
            )
            try:
                from vs_procurement.models import Vendor
                control |= set(
                    Vendor.objects.filter(entity=entity).exclude(payable_account=None)
                    .values_list("payable_account_id", flat=True)
                )
            except Exception:  # pragma: no cover - procurement optional
                pass
            ctx["control_ids"] = control
            ctx["cash_ids"] = set(
                Account.objects.filter(entity=entity, code=CASH_BANK_CODE).values_list("id", flat=True)
            )
        return ctx

    def list(self, request, *args, **kwargs):
        # Chart mode returns the full tree in one envelope (the tree needs every
        # node); the picker mode keeps the standard paginated response.
        if self._with_balance():
            qs = self.filter_queryset(self.get_queryset())
            data = self.get_serializer(qs, many=True).data
            return success_response("Chart of accounts retrieved.", data=data)
        return super().list(request, *args, **kwargs)


class AccountDetailView(APIView):
    """GET an account's detail + ledger activity; PATCH to edit it.

    GET returns the account, a balance summary (current, fiscal-year opening,
    line/journal counts) and its posted journal-line activity (newest first, with
    a running balance) — feeds the Chart-of-Accounts detail drawer.

    docstring-name: Account detail & ledger
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "finance.account.update" if self.request.method == "PATCH" else "finance.account.view"

    def _get(self, entity, pk):
        from .models import Account
        acc = Account.objects.filter(entity=entity, pk=pk).select_related("parent").first()
        if acc is None:
            raise NotFound("No such account in this entity.")
        return acc

    def get(self, request, pk):
        import datetime
        from .constants import DocumentStatus, NormalBalance, AccountType
        from .models import FiscalYear, JournalLine
        from .reports import _account_gl_net

        entity = resolve_entity(request)
        acc = self._get(entity, pk)
        sign = 1 if acc.normal_balance == NormalBalance.DEBIT else -1

        # Posted lines hitting this account, oldest-first to accumulate a running balance.
        lines = list(
            JournalLine.objects.filter(account=acc, entry__status=DocumentStatus.POSTED)
            .select_related("entry", "cost_center").order_by("entry__date", "entry__id", "line_no")
        )
        # Fiscal-year opening = net of everything posted before the current FY starts.
        today = datetime.date.today()
        fy = (
            FiscalYear.objects.filter(entity=entity, start_date__lte=today, end_date__gte=today).first()
            or FiscalYear.objects.filter(entity=entity).order_by("-year").first()
        )
        fy_start = fy.start_date if fy else None

        opening = 0
        running = 0
        activity = []
        journals = set()
        for ln in lines:
            net = sign * (ln.debit - ln.credit)
            if fy_start and ln.entry.date < fy_start:
                opening += net
            running += net
            journals.add(ln.entry_id)
            activity.append({
                "date": ln.entry.date.isoformat(),
                "journal_no": ln.entry.document_number,
                "source": getattr(ln.entry, "source", "") or "Manual",
                "description": ln.description or ln.entry.narration or "",
                "cost_center": ln.cost_center.code if ln.cost_center_id else "",
                "debit": _money(ln.debit),
                "credit": _money(ln.credit),
                "running_balance": _money(running),
            })
        activity.reverse()  # newest first for display

        # Headline balance uses the canonical denormalised GL net (same source as
        # the chart's Balance column) so the two always agree; the activity list's
        # running balance reflects the actual posted lines.
        return success_response(
            "Account detail retrieved.",
            data={
                "account": AccountSerializer(acc).data,
                "type_label": AccountType(acc.account_type).label if acc.account_type else "",
                "summary": {
                    "current_balance": _money(_account_gl_net(acc)),
                    "opening_balance": _money(opening),
                    "line_count": len(lines),
                    "journal_count": len(journals),
                },
                "activity": activity,
            },
        )

    def patch(self, request, pk):
        entity = resolve_entity(request)
        acc = self._get(entity, pk)
        body = request.data or {}
        # Only safe, non-structural fields are editable (type/normal/parent are not,
        # since changing them would rewrite how posted history is classified).
        if "name" in body:
            name = str(body["name"]).strip()
            if not name:
                raise ValidationError({"name": "A name is required."})
            acc.name = name
        for field in ("subtype", "description"):
            if field in body:
                setattr(acc, field, str(body[field]).strip())
        if "is_active" in body:
            acc.is_active = bool(body["is_active"])
        if "is_postable" in body:
            acc.is_postable = bool(body["is_postable"])
        acc.save()
        return success_response(f"Account {acc.code} updated.", data=AccountSerializer(acc).data)


class FiscalPeriodListView(EntityScopedListMixin, generics.ListAPIView):
    """GET /finance/periods/?entity= — the entity's fiscal periods.

    docstring-name: Fiscal periods
    """

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
    """GET /finance/journals/?entity= — posted/draft journal entries for the entity.

    docstring-name: Journal entries
    """

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
    """GET /finance/journals/<id>/?entity= — one journal entry with its lines.

    docstring-name: Journal entries
    """

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
    """GET /finance/invoices/?entity= — sales invoices for the entity.

    docstring-name: Customer invoices
    """

    serializer_class = InvoiceSerializer
    rbac_permission = "finance.invoice.view"

    def entity_qs(self, entity):
        qs = Invoice.objects.filter(entity=entity).select_related("customer")
        params = self.request.query_params
        if (status_val := params.get("status")):
            qs = qs.filter(status=status_val)
        if (pay := params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        if (customer := params.get("customer")):
            # Filter by customer code or id (feeds the receipts & allocation screen).
            qs = (qs.filter(customer__code=str(customer).upper()) if not str(customer).isdigit()
                  else qs.filter(customer_id=int(customer)))
        return qs.order_by("-invoice_date", "-id")


# --------------------------------------------------------------------------- #
# Actions                                                                     #
# --------------------------------------------------------------------------- #

class JournalPostView(APIView):
    """POST /finance/journals/<id>/post/?entity= — post a draft journal.

    docstring-name: Post a journal entry
    """

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
    """POST /finance/journals/<id>/reverse/?entity= — reverse a posted journal.

    docstring-name: Reverse a journal entry
    """

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


class DirectEntryCreateView(APIView):
    """POST /finance/direct-entries/?entity= — post a direct journal entry.

    Body: ``{"date"?, "narration"?, "reference"?, "lines": [{"account", "debit"|"credit"}]}``
    with amounts in kobo. The one sanctioned way to book money/balances that have no sub-ledger
    document behind them — capital injections, equity contributions, loan drawdowns, grants,
    opening balances and manual adjustments. Every other journal is a side-effect of an action.

    docstring-name: Post a direct entry
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.directentry.post"

    def post(self, request):
        from .posting import post_direct_entry

        entity = resolve_entity(request)
        serializer = DirectEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        entry = post_direct_entry(
            entity,
            lines=[(ln["account"], ln["debit"], ln["credit"]) for ln in data["lines"]],
            date=data.get("date"), narration=data.get("narration", ""),
            reference=data.get("reference", ""), actor_user=request.user,
        )
        return success_response(
            message=f"Direct entry posted as {entry.document_number}.",
            data=JournalEntryDetailSerializer(entry).data, status=201,
        )


class PeriodCloseView(APIView):
    """POST /finance/periods/<id>/close/?entity= — run the checklist and close a period.

    Body (all optional): ``{"soft": bool, "force": bool, "run_depreciation": bool}``.

    docstring-name: Close a fiscal period
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
    """docstring-name: Trial balance"""
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
    """docstring-name: Income statement"""
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
    """docstring-name: Balance sheet"""
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
    """docstring-name: Cash flow statement"""
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


class ChangesInEquityView(APIView):
    """docstring-name: Statement of changes in equity"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import statement_of_changes_in_equity

        from .exports import ReportTable

        entity = resolve_entity(request)
        period = _resolve_period(entity, request)
        soce = statement_of_changes_in_equity(entity, period=period)

        export = _maybe_export(request, ReportTable(
            title="Statement of Changes in Equity",
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'Inception to date'}",
            columns=["Component", "Opening", "Profit", "Contributions/(Distributions)", "Closing"],
            rows=[
                [c.label, c.opening_naira, c.profit_naira, c.contributions_naira, c.closing_naira]
                for c in soce.columns
            ],
            summary_rows=[[
                "TOTAL",
                format_naira(soce.total_opening), format_naira(soce.total_profit),
                format_naira(soce.total_contributions), format_naira(soce.total_closing),
            ]],
        ), filename=f"changes_in_equity_{entity.code}")
        if export is not None:
            return export

        return success_response(
            message="Statement of changes in equity retrieved.",
            data={
                "entity": entity.code,
                "period": getattr(period, "name", None),
                "as_of": str(soce.as_of),
                "columns": [
                    {
                        "key": c.key, "label": c.label, "code": c.code,
                        "account_id": c.account_id,
                        "opening": _money(c.opening), "profit": _money(c.profit),
                        "contributions": _money(c.contributions), "closing": _money(c.closing),
                    }
                    for c in soce.columns
                ],
                "total_opening": _money(soce.total_opening),
                "total_profit": _money(soce.total_profit),
                "total_contributions": _money(soce.total_contributions),
                "total_closing": _money(soce.total_closing),
                "balance_sheet_equity": _money(soce.balance_sheet_equity),
                "is_reconciled": soce.is_reconciled,
            },
        )


class StatutoryPackView(APIView):
    """docstring-name: Statutory reporting pack"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .reports import statutory_pack

        from .exports import ReportTable

        entity = resolve_entity(request)
        as_of = request.query_params.get("as_of") or None
        period = _resolve_period(entity, request)
        pack = statutory_pack(entity, as_of=as_of, period=period)

        # Export face: the IFRS-mapped Statement of Financial Position + Income
        # Statement as one flat table (the companion statements have their own exports).
        rows: list = []
        for section in pack.sofp_sections:
            rows.append([section.label.upper(), "", ""])
            for g in section.groups:
                rows.append(["", g.label, g.amount_naira])
            rows.append(["", f"  Total {section.label.lower()}", section.total_naira])
        rows.append(["INCOME STATEMENT", "", ""])
        for g in pack.income_lines:
            rows.append(["", g.label, g.amount_naira])
        rows.append(["", "  Net income", format_naira(pack.net_income)])
        export = _maybe_export(request, ReportTable(
            title="Statutory Pack (IFRS for SMEs)",
            subtitle=f"{entity.code} · as at {pack.as_of}",
            columns=["Section", "Line", "Amount"],
            rows=rows,
            summary_rows=[
                ["", "Total assets", format_naira(pack.total_assets)],
                ["", "Total equity", format_naira(pack.total_equity)],
                ["", "Total liabilities", format_naira(pack.total_liabilities)],
            ],
        ), filename=f"statutory_pack_{entity.code}")
        if export is not None:
            return export

        def _group(g):
            return {
                "line": g.line, "label": g.label, "amount": _money(g.amount),
                "accounts": [
                    {"account_id": a["account_id"], "code": a["code"],
                     "name": a["name"], "amount": _money(a["amount"])}
                    for a in g.accounts
                ],
            }

        cf = pack.cash_flow
        soce = pack.changes_in_equity
        tb = pack.trial_balance
        return success_response(
            message="Statutory pack retrieved.",
            data={
                "entity": entity.code,
                "as_of": str(pack.as_of),
                "period": getattr(period, "name", None),
                "statement_of_financial_position": {
                    "sections": [
                        {
                            "key": s.key, "label": s.label,
                            "groups": [_group(g) for g in s.groups],
                            "total": _money(s.total),
                        }
                        for s in pack.sofp_sections
                    ],
                    "total_assets": _money(pack.total_assets),
                    "total_equity": _money(pack.total_equity),
                    "total_liabilities": _money(pack.total_liabilities),
                    "is_balanced": pack.is_balanced,
                },
                "income_statement": {
                    "lines": [_group(g) for g in pack.income_lines],
                    "total_income": _money(pack.total_income),
                    "total_expense": _money(pack.total_expense),
                    "net_income": _money(pack.net_income),
                },
                "cash_flow": {
                    "opening_cash": _money(cf.opening_cash),
                    "closing_cash": _money(cf.closing_cash),
                    "by_activity": {k: _money(v) for k, v in cf.by_activity.items()},
                    "net_change": _money(cf.net_change),
                    "is_reconciled": cf.is_reconciled,
                },
                "changes_in_equity": {
                    "total_opening": _money(soce.total_opening),
                    "total_profit": _money(soce.total_profit),
                    "total_contributions": _money(soce.total_contributions),
                    "total_closing": _money(soce.total_closing),
                    "is_reconciled": soce.is_reconciled,
                },
                "trial_balance": {
                    "total_debit": _money(tb.total_debit),
                    "total_credit": _money(tb.total_credit),
                    "is_balanced": tb.is_balanced,
                },
            },
        )


class FinanceDashboardView(APIView):
    """Aggregated **Finance overview** — every dashboard block in one payload.

    Computed live from the GL and entity-scoped. Optional ``?period=<period_no>``
    pins the "as of" period; otherwise the latest open period is used.

    docstring-name: Finance dashboard
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.report.view"

    def get(self, request):
        from .dashboard import finance_dashboard

        entity = resolve_entity(request)
        period = _resolve_period(entity, request)
        return success_response(
            message="Finance dashboard retrieved.",
            data=finance_dashboard(entity, period=period),
        )


class ARAgingView(APIView):
    """docstring-name: AR aging report"""
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
    """docstring-name: AR reconciliation report"""
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
