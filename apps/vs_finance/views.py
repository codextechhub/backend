"""REST API for vs_finance — entity-scoped reads, documents and key actions.

Every endpoint is scoped to a :class:`~vs_finance.models.LedgerEntity` (the ledger's
tenant — *never* a School): pass ``?entity=<id or code>``. Master-data and document
lists use the platform's paginated envelope (:class:`core.pagination.XVSPagination`)
and RBAC gate (``finance.<resource>.<action>``); the financial statements are returned
as plain JSON by dedicated report endpoints. Domain errors raised by the services are
rendered by ``core.exceptions.custom_exception_handler`` (the typed-exception path), so
the views stay thin.
"""
from __future__ import annotations  # Import dependency used by this finance module.

from django.http import HttpResponse  # Import dependency used by this finance module.
from rest_framework import generics  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.
from rest_framework.response import Response  # Import dependency used by this finance module.
from rest_framework.views import APIView  # Import dependency used by this finance module.

from core.mixins import RetrieveModelMixin  # Import dependency used by this finance module.
from core.response import success_response  # Import dependency used by this finance module.
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive  # Import dependency used by this finance module.

from .models import (  # Import dependency used by this finance module.
    Account,  # Finance processing step.
    FiscalPeriod,  # Finance processing step.
    Invoice,  # Finance processing step.
    JournalEntry,  # Finance processing step.
    LedgerEntity,  # Finance processing step.
)  # Continue structured finance payload.
from .money import format_naira  # Import dependency used by this finance module.
from .serializers import (  # Import dependency used by this finance module.
    AccountSerializer,  # Finance processing step.
    FiscalPeriodSerializer,  # Finance processing step.
    FiscalYearSerializer,  # Finance processing step.
    InvoiceSerializer,  # Finance processing step.
    JournalEntryDetailSerializer,  # Finance processing step.
    JournalEntryListSerializer,  # Finance processing step.
    DirectEntryCreateSerializer,  # Finance processing step.
    LedgerEntityCreateSerializer,  # Finance processing step.
    LedgerEntitySerializer,  # Finance processing step.
)  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Entity scoping                                                              #
# --------------------------------------------------------------------------- #

def resolve_entity(request):  # Function handles this finance operation.
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
    raw = request.query_params.get("entity")  # Store intermediate finance value.
    if not raw:  # Branch when this finance condition is true.
        raise ValidationError({"entity": "An 'entity' query parameter (id or code) is required."})  # Surface validation or finance error.
    qs = LedgerEntity.objects.all()  # Query finance data from the database.

    user = getattr(request, "user", None)  # Store intermediate finance value.
    if getattr(user, "user_type", None) != "CX_STAFF":  # Branch when this finance condition is true.
        school = getattr(request, "school", None) or getattr(user, "school", None)  # Store intermediate finance value.
        if school is None:  # Branch when this finance condition is true.
            raise NotFound(f"No ledger entity matches '{raw}'.")  # Surface validation or finance error.
        qs = qs.filter(source_school=school)  # Store intermediate finance value.

    entity = (  # Store intermediate finance value.
        qs.filter(pk=int(raw)).first() if str(raw).isdigit()  # Store intermediate finance value.
        else qs.filter(code=str(raw).upper()).first()  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if entity is None:  # Branch when this finance condition is true.
        raise NotFound(f"No ledger entity matches '{raw}'.")  # Surface validation or finance error.
    return entity  # Return the computed finance response.


class EntityScopedListMixin:  # Class groups related finance API or service behavior.
    """A ListAPIView whose queryset is filtered to the resolved entity via ``entity_qs``."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.

    def get_queryset(self):  # Function handles this finance operation.
        self.entity = resolve_entity(self.request)  # Store intermediate finance value.
        return self.entity_qs(self.entity)  # Return the computed finance response.

    def entity_qs(self, entity):  # pragma: no cover - overridden
        raise NotImplementedError  # Surface validation or finance error.


def _resolve_period(entity, request, *, param="period"):  # Function handles this finance operation.
    """Resolve an optional ``?period=<id or period_no>`` for this entity, or ``None``."""
    raw = request.query_params.get(param)  # Store intermediate finance value.
    if not raw:  # Branch when this finance condition is true.
        return None  # Return the computed finance response.
    qs = FiscalPeriod.objects.filter(entity=entity)  # Query finance data from the database.
    period = (  # Store intermediate finance value.
        qs.filter(pk=int(raw)).first() if str(raw).isdigit() and int(raw) > 12  # Store intermediate finance value.
        else qs.filter(period_no=int(raw)).order_by("-fiscal_year__year").first()  # Store intermediate finance value.
        if str(raw).isdigit() else None  # Branch when this finance condition is true.
    )  # Continue structured finance payload.
    if period is None:  # Branch when this finance condition is true.
        raise NotFound(f"No fiscal period matches '{raw}' for this entity.")  # Surface validation or finance error.
    return period  # Return the computed finance response.


# --------------------------------------------------------------------------- #
# Master data + documents                                                     #
# --------------------------------------------------------------------------- #

class EntityListCreateView(generics.ListCreateAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/entities/ — the ledger entities (sets of books) on the platform.

    POST /finance/entities/ — provision a **new** set of books. Entity creation is a
    structural, platform-level operation (a new entity becomes the tenant of its own
    documents and numbering), so it is gated on the dedicated ``finance.entity.create``
    key, which is granted only to the platform admin roles.

    docstring-name: Ledger entities
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return ("finance.entity.create" if self.request.method == "POST"  # Return the computed finance response.
                else "finance.entity.view")  # Finance processing step.

    def get_serializer_class(self):  # Function handles this finance operation.
        if self.request.method == "POST":  # Branch when this finance condition is true.
            return LedgerEntityCreateSerializer  # Return the computed finance response.
        return LedgerEntitySerializer  # Return the computed finance response.

    def get_queryset(self):  # Function handles this finance operation.
        qs = LedgerEntity.objects.all().order_by("code")  # Query finance data from the database.
        # Tenancy (defence-in-depth, matching resolve_entity): CX staff see every set
        # of books; a school-scoped user sees only entities sourced from their school,
        # and a user with no school sees none.
        user = getattr(self.request, "user", None)  # Store intermediate finance value.
        if getattr(user, "user_type", None) != "CX_STAFF":  # Branch when this finance condition is true.
            school = getattr(self.request, "school", None) or getattr(user, "school", None)  # Store intermediate finance value.
            if school is None:  # Branch when this finance condition is true.
                return qs.none()  # Return the computed finance response.
            qs = qs.filter(source_school=school)  # Store intermediate finance value.
        if (kind := self.request.query_params.get("kind")):  # Branch when this finance condition is true.
            qs = qs.filter(kind=kind)  # Store intermediate finance value.
        if (active := self.request.query_params.get("is_active")) is not None:  # Branch when this finance condition is true.
            if active.lower() in ("true", "false"):  # Branch when this finance condition is true.
                qs = qs.filter(is_active=active.lower() == "true")  # Store intermediate finance value.
        return qs  # Return the computed finance response.

    def create(self, request, *args, **kwargs):  # Function handles this finance operation.
        serializer = self.get_serializer(data=request.data)  # Store intermediate finance value.
        serializer.is_valid(raise_exception=True)  # Store intermediate finance value.
        entity = serializer.save()  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Ledger entity {entity.code} created.",  # Finance processing step.
            data=serializer.data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class AccountListCreateView(EntityScopedListMixin, generics.ListAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/accounts/?entity= — the entity's chart of accounts.

    ``?with_balance=true`` returns the **whole tree** (un-paginated) with each
    account's net GL ``balance`` and sub-ledger ``tag`` (CONTROL / CASH) — for the
    Chart-of-Accounts screen. Without it, the plain paginated list is served (used
    by the account pickers).

    docstring-name: Chart of accounts
    """

    serializer_class = AccountSerializer  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.account.create" if self.request.method == "POST" else "finance.account.view"  # Return the computed finance response.

    def _with_balance(self):  # Function handles this finance operation.
        return self.request.query_params.get("with_balance") == "true"  # Return the computed finance response.

    # POST added manually (not using CreateAPIView or ListCreateAPIView)
    def post(self, request):  # Function handles this finance operation.
        """Create a new chart-of-accounts node for the entity."""
        from .constants import AccountType  # Import dependency used by this finance module.
        from .models import Account  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip()  # Store intermediate finance value.
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "An account code is required."})  # Surface validation or finance error.
        if Account.objects.filter(entity=entity, code=code).exists():  # Branch when this finance condition is true.
            raise ValidationError({"code": f"Account '{code}' already exists in this entity."})  # Surface validation or finance error.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "An account name is required."})  # Surface validation or finance error.
        atype = body.get("account_type")  # Store intermediate finance value.
        if atype not in AccountType.values:  # Branch when this finance condition is true.
            raise ValidationError({"account_type": "Choose a valid account type."})  # Surface validation or finance error.
        parent = None  # Store intermediate finance value.
        if (parent_ref := body.get("parent")) not in (None, ""):  # Branch when this finance condition is true.
            # Resolve by code first, then numeric pk (mirrors _resolve_cost_center),
            # scoped to the entity.
            pqs = Account.objects.filter(entity=entity)  # Query finance data from the database.
            parent = pqs.filter(code=str(parent_ref)).first()  # Store intermediate finance value.
            if parent is None and str(parent_ref).isdigit():  # Branch when this finance condition is true.
                parent = pqs.filter(pk=int(parent_ref)).first()  # Store intermediate finance value.
            if parent is None:  # Branch when this finance condition is true.
                raise ValidationError({"parent": "No such parent account in this entity."})  # Surface validation or finance error.
        # normal_balance is derived from type/contra by Account.save() when left blank.
        account = Account.objects.create(  # Query finance data from the database.
            entity=entity, code=code, name=name, account_type=atype, parent=parent,  # Store intermediate finance value.
            is_contra=bool(body.get("is_contra", False)),  # Store intermediate finance value.
            is_postable=bool(body.get("is_postable", True)),  # Store intermediate finance value.
            subtype=str(body.get("subtype", "")).strip(),  # Store intermediate finance value.
            description=str(body.get("description", "")).strip(),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Account {account.code} created.", data=AccountSerializer(account).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def entity_qs(self, entity):  # Function handles this finance operation.
        qs = Account.objects.filter(entity=entity).select_related("parent").order_by("code")  # Query finance data from the database.
        params = self.request.query_params  # Store intermediate finance value.
        if self._with_balance():  # Branch when this finance condition is true.
            from django.db.models import F, Sum  # Import dependency used by this finance module.
            from django.db.models.functions import Coalesce  # Import dependency used by this finance module.
            qs = qs.annotate(  # Store intermediate finance value.
                _bal_dr=Coalesce(Sum(F("balances__opening_debit") + F("balances__debit_total")), 0),  # Store intermediate finance value.
                _bal_cr=Coalesce(Sum(F("balances__opening_credit") + F("balances__credit_total")), 0),  # Store intermediate finance value.
            )  # Continue structured finance payload.
        if (atype := params.get("account_type")):  # Branch when this finance condition is true.
            # accepts a single type or a comma list (e.g. INCOME,EXPENSE for budgets)
            types = [t.strip() for t in atype.split(",") if t.strip()]  # Store intermediate finance value.
            qs = qs.filter(account_type__in=types) if len(types) > 1 else qs.filter(account_type=types[0])  # Store intermediate finance value.
        if (postable := params.get("is_postable")) is not None:  # Branch when this finance condition is true.
            if postable.lower() in ("true", "false"):  # Branch when this finance condition is true.
                qs = qs.filter(is_postable=postable.lower() == "true")  # Store intermediate finance value.
        return qs  # Return the computed finance response.

    def get_serializer_context(self):  # Function handles this finance operation.
        ctx = super().get_serializer_context()  # Store intermediate finance value.
        if self._with_balance():  # Branch when this finance condition is true.
            entity = getattr(self, "entity", None) or resolve_entity(self.request)  # Store intermediate finance value.
            from .constants import CASH_BANK_CODE  # Import dependency used by this finance module.
            from .models import Customer  # Import dependency used by this finance module.
            control = set(  # Store intermediate finance value.
                Customer.objects.filter(entity=entity).exclude(receivable_account=None)  # Query finance data from the database.
                .values_list("receivable_account_id", flat=True)  # Store intermediate finance value.
            )  # Continue structured finance payload.
            try:  # Start protected finance operation.
                from vs_procurement.models import Vendor  # Import dependency used by this finance module.
                control |= set(  # Store intermediate finance value.
                    Vendor.objects.filter(entity=entity).exclude(payable_account=None)  # Query finance data from the database.
                    .values_list("payable_account_id", flat=True)  # Store intermediate finance value.
                )  # Continue structured finance payload.
            except Exception:  # pragma: no cover - procurement optional
                pass  # No operation required for this branch.
            ctx["control_ids"] = control  # Store intermediate finance value.
            ctx["cash_ids"] = set(  # Store intermediate finance value.
                Account.objects.filter(entity=entity, code=CASH_BANK_CODE).values_list("id", flat=True)  # Query finance data from the database.
            )  # Continue structured finance payload.
        return ctx  # Return the computed finance response.

    def list(self, request, *args, **kwargs):  # Function handles this finance operation.
        # Chart mode returns the full tree in one envelope (the tree needs every
        # node); the picker mode keeps the standard paginated response.
        if self._with_balance():  # Branch when this finance condition is true.
            qs = self.filter_queryset(self.get_queryset())  # Store intermediate finance value.
            data = self.get_serializer(qs, many=True).data  # Store intermediate finance value.
            return success_response("Chart of accounts retrieved.", data=data)  # Return the computed finance response.
        return super().list(request, *args, **kwargs)  # Return the computed finance response.


class AccountDetailView(APIView):  # Class groups related finance API or service behavior.
    """GET an account's detail + ledger activity; PATCH to edit it.

    GET returns the account, a balance summary (current, fiscal-year opening,
    line/journal counts) and its posted journal-line activity (newest first, with
    a running balance) — feeds the Chart-of-Accounts detail drawer.

    docstring-name: Account detail & ledger
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.account.update" if self.request.method == "PATCH" else "finance.account.view"  # Return the computed finance response.

    def _get(self, entity, pk):  # Function handles this finance operation.
        from .models import Account  # Import dependency used by this finance module.
        acc = Account.objects.filter(entity=entity, pk=pk).select_related("parent").first()  # Query finance data from the database.
        if acc is None:  # Branch when this finance condition is true.
            raise NotFound("No such account in this entity.")  # Surface validation or finance error.
        return acc  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.
        from .constants import DocumentStatus, NormalBalance, AccountType  # Import dependency used by this finance module.
        from .models import FiscalYear, JournalLine  # Import dependency used by this finance module.
        from .reports import _account_gl_net  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        acc = self._get(entity, pk)  # Store intermediate finance value.
        sign = 1 if acc.normal_balance == NormalBalance.DEBIT else -1  # Store intermediate finance value.

        # Posted lines hitting this account, oldest-first to accumulate a running balance.
        lines = list(  # Store intermediate finance value.
            JournalLine.objects.filter(account=acc, entry__status__in=[DocumentStatus.POSTED, DocumentStatus.REVERSED])  # Query finance data from the database.
            .select_related("entry", "cost_center").order_by("entry__date", "entry__id", "line_no")  # Finance processing step.
        )  # Continue structured finance payload.
        # Fiscal-year opening = net of everything posted before the current FY starts.
        today = datetime.date.today()  # Store intermediate finance value.
        fy = (  # Store intermediate finance value.
            FiscalYear.objects.filter(entity=entity, start_date__lte=today, end_date__gte=today).first()  # Query finance data from the database.
            or FiscalYear.objects.filter(entity=entity).order_by("-year").first()  # Query finance data from the database.
        )  # Continue structured finance payload.
        fy_start = fy.start_date if fy else None  # Store intermediate finance value.

        opening = 0  # Store intermediate finance value.
        running = 0  # Store intermediate finance value.
        activity = []  # Store intermediate finance value.
        journals = set()  # Store intermediate finance value.
        for ln in lines:  # Iterate through finance records.
            net = sign * (ln.debit - ln.credit)  # Store intermediate finance value.
            if fy_start and ln.entry.date < fy_start:  # Branch when this finance condition is true.
                opening += net  # Store intermediate finance value.
            running += net  # Store intermediate finance value.
            journals.add(ln.entry_id)  # Finance processing step.
            activity.append({  # Finance processing step.
                "date": ln.entry.date.isoformat(),  # Finance processing step.
                "journal_no": ln.entry.document_number,  # Finance processing step.
                "source": getattr(ln.entry, "source", "") or "Manual",  # Finance processing step.
                "status": ln.entry.status,  # Finance processing step.
                "description": ln.description or ln.entry.narration or "",  # Finance processing step.
                "cost_center": ln.cost_center.code if ln.cost_center_id else "",  # Finance processing step.
                "dimensions": ln.dimensions or {},  # Finance processing step.
                "debit": _money(ln.debit),  # Finance processing step.
                "credit": _money(ln.credit),  # Finance processing step.
                "running_balance": _money(running),  # Finance processing step.
            })  # Continue structured finance payload.
        activity.reverse()  # newest first for display

        # Headline balance uses the canonical denormalised GL net (same source as
        # the chart's Balance column) so the two always agree; the activity list's
        # running balance reflects the actual posted lines.
        return success_response(  # Return the computed finance response.
            "Account detail retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "account": AccountSerializer(acc).data,  # Finance processing step.
                "type_label": AccountType(acc.account_type).label if acc.account_type else "",  # Finance processing step.
                "summary": {  # Finance processing step.
                    "current_balance": _money(_account_gl_net(acc)),  # Finance processing step.
                    "opening_balance": _money(opening),  # Finance processing step.
                    "line_count": len(lines),  # Finance processing step.
                    "journal_count": len(journals),  # Finance processing step.
                },  # Continue structured finance payload.
                "activity": activity,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.

    def patch(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        acc = self._get(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        # Only safe, non-structural fields are editable (type/normal/parent are not,
        # since changing them would rewrite how posted history is classified).
        if "name" in body:  # Branch when this finance condition is true.
            name = str(body["name"]).strip()  # Store intermediate finance value.
            if not name:  # Branch when this finance condition is true.
                raise ValidationError({"name": "A name is required."})  # Surface validation or finance error.
            acc.name = name  # Store intermediate finance value.
        for field in ("subtype", "description"):  # Iterate through finance records.
            if field in body:  # Branch when this finance condition is true.
                setattr(acc, field, str(body[field]).strip())  # Finance processing step.
        if "is_active" in body:  # Branch when this finance condition is true.
            acc.is_active = bool(body["is_active"])  # Store intermediate finance value.
        if "is_postable" in body:  # Branch when this finance condition is true.
            acc.is_postable = bool(body["is_postable"])  # Store intermediate finance value.
        acc.save()  # Finance processing step.
        return success_response(f"Account {acc.code} updated.", data=AccountSerializer(acc).data)  # Return the computed finance response.


class FiscalPeriodListView(EntityScopedListMixin, generics.ListAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/periods/?entity= — the entity's fiscal periods.

    docstring-name: Fiscal periods
    """

    serializer_class = FiscalPeriodSerializer  # Store intermediate finance value.
    rbac_permission = "finance.period.view"  # Store intermediate finance value.

    def entity_qs(self, entity):  # Function handles this finance operation.
        qs = FiscalPeriod.objects.filter(entity=entity).select_related("fiscal_year")  # Query finance data from the database.
        if (status_val := self.request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (year := self.request.query_params.get("year")):  # Branch when this finance condition is true.
            qs = qs.filter(fiscal_year__year=year)  # Store intermediate finance value.
        return qs.order_by("fiscal_year__year", "period_no")  # Return the computed finance response.


class FiscalYearListView(EntityScopedListMixin, generics.ListAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/fiscal-years/?entity= — the entity's fiscal years.

    ``?status=OPEN`` narrows to open years (the ones a new budget can target).

    docstring-name: Fiscal years
    """

    serializer_class = FiscalYearSerializer  # Store intermediate finance value.
    rbac_permission = "finance.period.view"  # Store intermediate finance value.

    def entity_qs(self, entity):  # Function handles this finance operation.
        from .models import FiscalYear  # Import dependency used by this finance module.

        qs = FiscalYear.objects.filter(entity=entity)  # Query finance data from the database.
        if (status_val := self.request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        return qs.order_by("-year")  # Return the computed finance response.


class JournalEntryListView(EntityScopedListMixin, generics.ListAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/journals/?entity= — posted/draft journal entries for the entity.

    docstring-name: Journal entries
    """

    serializer_class = JournalEntryListSerializer  # Store intermediate finance value.
    rbac_permission = "finance.journal.view"  # Store intermediate finance value.

    def entity_qs(self, entity):  # Function handles this finance operation.
        from django.db.models import Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

        qs = (  # Store intermediate finance value.
            JournalEntry.objects.filter(entity=entity)  # Query finance data from the database.
            .select_related("period", "created_by")  # Finance processing step.
            .annotate(_total_debit=Coalesce(Sum("lines__debit"), 0))  # Store intermediate finance value.
        )  # Continue structured finance payload.
        params = self.request.query_params  # Store intermediate finance value.
        if (status_val := params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (source := params.get("source")):  # Branch when this finance condition is true.
            qs = qs.filter(source=source)  # Store intermediate finance value.
        if (date_from := params.get("date_from")):  # Branch when this finance condition is true.
            qs = qs.filter(date__gte=date_from)  # Store intermediate finance value.
        if (date_to := params.get("date_to")):  # Branch when this finance condition is true.
            qs = qs.filter(date__lte=date_to)  # Store intermediate finance value.
        if (search := params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search)  # Store intermediate finance value.
                | Q(narration__icontains=search)  # Store intermediate finance value.
                | Q(reference__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        return qs.order_by("-date", "-id")  # Return the computed finance response.


class JournalSummaryView(APIView):  # Class groups related finance API or service behavior.
    """Status counts + posted total.

    Powers the Journal Entries status tabs and footer (one cheap aggregate, honours
    the same source/date/search filters as the list).

    docstring-name: Journal summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.journal.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Count, Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.
        from .constants import DocumentStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = JournalEntry.objects.filter(entity=entity)  # Query finance data from the database.
        params = request.query_params  # Store intermediate finance value.
        if (source := params.get("source")):  # Branch when this finance condition is true.
            qs = qs.filter(source=source)  # Store intermediate finance value.
        if (date_from := params.get("date_from")):  # Branch when this finance condition is true.
            qs = qs.filter(date__gte=date_from)  # Store intermediate finance value.
        if (date_to := params.get("date_to")):  # Branch when this finance condition is true.
            qs = qs.filter(date__lte=date_to)  # Store intermediate finance value.
        if (search := params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search)  # Store intermediate finance value.
                | Q(narration__icontains=search)  # Store intermediate finance value.
                | Q(reference__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        by_status = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}  # Store intermediate finance value.
        posted_total = (  # Store intermediate finance value.
            qs.filter(status=DocumentStatus.POSTED)  # Store intermediate finance value.
            .aggregate(t=Coalesce(Sum("lines__debit"), 0))["t"]  # Store intermediate finance value.
        )  # Continue structured finance payload.
        reversed_total = (  # Store intermediate finance value.
            qs.filter(status=DocumentStatus.REVERSED)  # Store intermediate finance value.
            .aggregate(t=Coalesce(Sum("lines__debit"), 0))["t"]  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            "Journal summary retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "total": sum(by_status.values()),  # Finance processing step.
                "by_status": by_status,  # Finance processing step.
                "posted_total": _money(posted_total),  # Finance processing step.
                "reversed_total": _money(reversed_total),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class JournalEntryDetailView(RetrieveModelMixin, generics.RetrieveAPIView):  # Class groups related finance API or service behavior.
    """GET /finance/journals/<id>/?entity= — one journal entry with its lines.

    docstring-name: Journal entries
    """

    serializer_class = JournalEntryDetailSerializer  # Store intermediate finance value.
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.journal.view"  # Store intermediate finance value.
    lookup_field = "id"  # Store intermediate finance value.

    def get_queryset(self):  # Function handles this finance operation.
        entity = resolve_entity(self.request)  # Store intermediate finance value.
        return (  # Return the computed finance response.
            JournalEntry.objects.filter(entity=entity)  # Query finance data from the database.
            .select_related("period")  # Finance processing step.
            .prefetch_related("lines__account")  # Finance processing step.
        )  # Continue structured finance payload.


class InvoiceListCreateView(EntityScopedListMixin, generics.ListAPIView):  # Class groups related finance API or service behavior.
    """Sales invoices for the entity. Also, raise a manual invoice (and post it).

    docstring-name: Customer invoices
    """

    serializer_class = InvoiceSerializer  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.invoice.create" if self.request.method == "POST" \
            else "finance.invoice.view"  # Finance processing step.

    def post(self, request, *args, **kwargs):  # Function handles this finance operation.
        """Create a manual invoice from ``{customer, invoice_date, lines:[...]}``.

        Each line: ``{revenue_account, description?, quantity?, unit_price, tax_code?,
        cost_center?}`` (unit_price in kobo). Posts the AR journal unless
        ``post=false`` (saved as a priced draft). Mirrors the fee-run path.
        """
        from django.db import transaction  # Import dependency used by this finance module.
        from .models import InvoiceLine  # Import dependency used by this finance module.
        from .receivables import post_invoice, price_invoice  # Import dependency used by this finance module.
        from .views_ar import _resolve_customer  # Import dependency used by this finance module.
        from .views_ops import (  # Import dependency used by this finance module.
            _date, _dec, _money, _require_lines, _resolve_account,  # Finance processing step.
            _resolve_cost_center, _resolve_currency, _resolve_tax,  # Finance processing step.
        )  # Continue structured finance payload.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        lines = _require_lines(body)  # Store intermediate finance value.
        should_post = body.get("post", True)  # Store intermediate finance value.
        if isinstance(should_post, str):  # Branch when this finance condition is true.
            should_post = should_post.lower() not in ("false", "0", "no")  # Store intermediate finance value.

        with transaction.atomic():  # Enter scoped finance context.
            invoice = Invoice.objects.create(  # Query finance data from the database.
                entity=entity,  # Store intermediate finance value.
                customer=_resolve_customer(entity, body.get("customer")),  # Store intermediate finance value.
                invoice_date=_date(body.get("invoice_date"), "invoice_date", required=True),  # Store intermediate finance value.
                due_date=_date(body.get("due_date"), "due_date"),  # Store intermediate finance value.
                currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
                source="MANUAL",  # Store intermediate finance value.
                reference=body.get("reference", ""),  # Store intermediate finance value.
                narration=body.get("narration", ""),  # Store intermediate finance value.
                created_by=request.user,  # Store intermediate finance value.
            )  # Continue structured finance payload.
            for i, ln in enumerate(lines, start=1):  # Iterate through finance records.
                InvoiceLine.objects.create(  # Query finance data from the database.
                    invoice=invoice, line_no=i,  # Store intermediate finance value.
                    description=ln.get("description", ""),  # Store intermediate finance value.
                    revenue_account=_resolve_account(  # Store intermediate finance value.
                        entity, ln.get("revenue_account"),  # Finance processing step.
                        f"lines[{i}].revenue_account", required=True),  # Store intermediate finance value.
                    quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),  # Store intermediate finance value.
                    unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),  # Store intermediate finance value.
                    tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),  # Store intermediate finance value.
                    cost_center=_resolve_cost_center(  # Store intermediate finance value.
                        entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
                )  # Continue structured finance payload.
            if should_post:  # Branch when this finance condition is true.
                post_invoice(invoice, actor_user=request.user)  # Store intermediate finance value.
            else:  # Fallback finance branch.
                price_invoice(invoice)  # Finance processing step.

        invoice.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Invoice {invoice.document_number} {'posted' if should_post else 'saved as draft'}.",  # Finance processing step.
            data=InvoiceSerializer(invoice).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def entity_qs(self, entity):  # Function handles this finance operation.
        from django.db.models import Q  # Import dependency used by this finance module.

        qs = Invoice.objects.filter(entity=entity).select_related("customer")  # Query finance data from the database.
        params = self.request.query_params  # Store intermediate finance value.
        if (status_val := params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (pay := params.get("payment_status")):  # Branch when this finance condition is true.
            qs = qs.filter(payment_status=pay)  # Store intermediate finance value.
        if (bucket := params.get("bucket")):  # Branch when this finance condition is true.
            qs = _invoice_bucket(qs, bucket)  # Store intermediate finance value.
        if (search := params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search)  # Store intermediate finance value.
                | Q(customer__name__icontains=search)  # Store intermediate finance value.
                | Q(customer__code__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        if (customer := params.get("customer")):  # Branch when this finance condition is true.
            # Filter by customer code or id (feeds the receipts & allocation screen).
            qs = (qs.filter(customer__code=str(customer).upper()) if not str(customer).isdigit()  # Store intermediate finance value.
                  else qs.filter(customer_id=int(customer)))  # Store intermediate finance value.
        return qs.order_by("-invoice_date", "-id")  # Return the computed finance response.


def _invoice_bucket(qs, bucket):  # Function handles this finance operation.
    """Filter invoices to a derived status bucket (the design's status tabs)."""
    import datetime  # Import dependency used by this finance module.
    from django.db.models import Q  # Import dependency used by this finance module.
    from .constants import DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.

    today = datetime.date.today()  # Store intermediate finance value.
    not_overdue = Q(due_date__gte=today) | Q(due_date__isnull=True)  # Store intermediate finance value.
    posted = qs.filter(status=DocumentStatus.POSTED)  # Store intermediate finance value.
    if bucket == "draft":  # Branch when this finance condition is true.
        return qs.filter(status=DocumentStatus.DRAFT)  # Return the computed finance response.
    if bucket == "paid":  # Branch when this finance condition is true.
        return posted.filter(payment_status=InvoicePaymentStatus.PAID)  # Return the computed finance response.
    if bucket == "overdue":  # Branch when this finance condition is true.
        return posted.exclude(payment_status=InvoicePaymentStatus.PAID).filter(due_date__lt=today)  # Return the computed finance response.
    if bucket == "partial":  # Branch when this finance condition is true.
        return posted.filter(payment_status=InvoicePaymentStatus.PARTIAL).filter(not_overdue)  # Return the computed finance response.
    if bucket == "issued":  # Branch when this finance condition is true.
        return posted.filter(payment_status=InvoicePaymentStatus.UNPAID).filter(not_overdue)  # Return the computed finance response.
    return qs  # Return the computed finance response.


class InvoiceSummaryView(APIView):  # Class groups related finance API or service behavior.
    """AR KPIs, status counts, totals.

    Powers the Student-Invoices KPI cards (total invoiced/collected, collection
    rate, overdue balance + a 12-month series for the sparklines), the status tabs
    and the footer totals. Honours the same ``?search=`` as the list.

    docstring-name: Invoice summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.invoice.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.
        from django.db.models import F, Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce, TruncMonth  # Import dependency used by this finance module.
        from .constants import DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.
        from .models import Invoice, Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        today = datetime.date.today()  # Store intermediate finance value.
        base = Invoice.objects.filter(entity=entity)  # Query finance data from the database.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            base = base.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search)  # Store intermediate finance value.
                | Q(customer__name__icontains=search)  # Store intermediate finance value.
                | Q(customer__code__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        posted = base.filter(status=DocumentStatus.POSTED)  # Store intermediate finance value.
        unpaid_posted = posted.exclude(payment_status=InvoicePaymentStatus.PAID)  # Store intermediate finance value.
        bal = F("total") - F("amount_paid") - F("amount_credited")  # Store intermediate finance value.

        invoiced = posted.aggregate(t=Coalesce(Sum("total"), 0))["t"]  # Store intermediate finance value.
        collected = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED).aggregate(  # Query finance data from the database.
            t=Coalesce(Sum("amount"), 0))["t"]  # Store intermediate finance value.
        overdue_balance = unpaid_posted.filter(due_date__lt=today).aggregate(t=Coalesce(Sum(bal), 0))["t"]  # Store intermediate finance value.
        outstanding = unpaid_posted.aggregate(t=Coalesce(Sum(bal), 0))["t"]  # Store intermediate finance value.
        total_all = base.aggregate(t=Coalesce(Sum("total"), 0))["t"]  # Store intermediate finance value.
        rate = round(collected * 100 / invoiced, 1) if invoiced else 0.0  # Store intermediate finance value.

        by_status = {  # Store intermediate finance value.
            b: _invoice_bucket(base, b).count()  # Finance processing step.
            for b in ("draft", "issued", "partial", "paid", "overdue")  # Iterate through finance records.
        }  # Continue structured finance payload.
        total_count = base.count()  # Store intermediate finance value.

        first = today.replace(day=1)  # Store intermediate finance value.
        y, mo = first.year, first.month - 11  # Store intermediate finance value.
        while mo <= 0:  # Loop while this finance condition holds.
            mo += 12  # Store intermediate finance value.
            y -= 1  # Store intermediate finance value.
        start = datetime.date(y, mo, 1)  # Store intermediate finance value.
        inv_m = {r["m"]: int(r["s"] or 0) for r in posted.filter(invoice_date__gte=start)  # Store intermediate finance value.
                 .annotate(m=TruncMonth("invoice_date")).values("m").annotate(s=Sum("total"))}  # Store intermediate finance value.
        col_m = {r["m"]: int(r["s"] or 0) for r in Payment.objects  # Query finance data from the database.
                 .filter(entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start)  # Store intermediate finance value.
                 .annotate(m=TruncMonth("payment_date")).values("m").annotate(s=Sum("amount"))}  # Store intermediate finance value.
        monthly, cur = [], start  # Store intermediate finance value.
        for _ in range(12):  # Iterate through finance records.
            key = datetime.date(cur.year, cur.month, 1)  # Store intermediate finance value.
            monthly.append({"label": cur.strftime("%b %y"), "invoiced": inv_m.get(key, 0), "collected": col_m.get(key, 0)})  # Finance processing step.
            cur = datetime.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)  # Store intermediate finance value.

        return success_response(  # Return the computed finance response.
            "Invoice summary retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "kpis": {  # Finance processing step.
                    "total_invoiced": _money(invoiced),  # Finance processing step.
                    "total_collected": _money(collected),  # Finance processing step.
                    "collection_rate": rate,  # Finance processing step.
                    "overdue_balance": _money(overdue_balance),  # Finance processing step.
                },  # Continue structured finance payload.
                "by_status": {**by_status, "total": total_count},  # Finance processing step.
                "totals": {"count": total_count, "total": _money(total_all), "outstanding": _money(outstanding)},  # Finance processing step.
                "monthly": monthly,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class InvoiceDetailView(APIView):  # Class groups related finance API or service behavior.
    """GET /finance/invoices/<id>/ — the full invoice for the detail drawer:
    lines, allocated payments, GL postings (from its journal), reminders, and a
    derived activity timeline.

    docstring-name: Invoice detail
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.invoice.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        from .constants import FinanceAuditAction, FinanceAuditStatus  # Import dependency used by this finance module.
        from .models import FinanceAuditLog, Invoice, JournalEntry  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        inv = (  # Store intermediate finance value.
            Invoice.objects.filter(entity=entity, pk=pk)  # Query finance data from the database.
            .select_related("customer", "journal")  # Finance processing step.
            .prefetch_related(  # Finance processing step.
                "lines__revenue_account", "lines__tax_code",  # Finance processing step.
                "allocations__payment__journal__lines__account",  # Finance processing step.
                "credit_allocations__note__journal__lines__account",  # Finance processing step.
                "concessions__journal__lines__account",  # Finance processing step.
                "dunning_notices", "journal__lines__account",  # Finance processing step.
            )  # Continue structured finance payload.
            .first()  # Finance processing step.
        )  # Continue structured finance payload.
        if inv is None:  # Branch when this finance condition is true.
            raise NotFound("No such invoice in this entity.")  # Surface validation or finance error.

        # Write-offs leave no allocation row and their journal has no invoice FK — the
        # only structured link back to the invoice is the audit trail. Pull the
        # successful write-off events, then fetch their journals for the GL history.
        writeoff_logs = list(  # Store intermediate finance value.
            FinanceAuditLog.objects.filter(  # Query finance data from the database.
                entity=entity, target_type="Invoice", target_id=str(inv.pk),  # Store intermediate finance value.
                action=FinanceAuditAction.INVOICE_WRITTEN_OFF,  # Store intermediate finance value.
                status=FinanceAuditStatus.SUCCESS,  # Store intermediate finance value.
            ).order_by("created_at")  # Continue structured finance payload.
        )  # Continue structured finance payload.
        writeoff_journal_ids = [  # Store intermediate finance value.
            int(log.metadata["journal_id"])  # Finance processing step.
            for log in writeoff_logs if log.metadata.get("journal_id")  # Iterate through finance records.
        ]  # Continue structured finance payload.
        writeoff_journals = {  # Store intermediate finance value.
            j.id: j for j in JournalEntry.objects  # Query finance data from the database.
            .filter(id__in=writeoff_journal_ids)  # Store intermediate finance value.
            .prefetch_related("lines__account")  # Finance processing step.
        }  # Continue structured finance payload.

        lines = [  # Store intermediate finance value.
            {  # Continue structured finance payload.
                "description": ln.description or "—",  # Finance processing step.
                "account_code": ln.revenue_account.code,  # Finance processing step.
                "account_name": ln.revenue_account.name,  # Finance processing step.
                "quantity": str(ln.quantity),  # Finance processing step.
                "unit_price": _money(ln.unit_price),  # Finance processing step.
                "tax_code": ln.tax_code.code if ln.tax_code_id else None,  # Finance processing step.
                "tax_amount": _money(ln.tax_amount),  # Finance processing step.
                "line_total": _money(ln.net_amount + ln.tax_amount),  # Finance processing step.
            }  # Continue structured finance payload.
            for ln in inv.lines.all()  # Iterate through finance records.
        ]  # Continue structured finance payload.

        # Cash receipts allocated to this invoice — kept as `payments` for existing
        # consumers; also fed into the unified `settlements` list below.
        payments = [  # Store intermediate finance value.
            {  # Continue structured finance payload.
                "date": a.payment.payment_date.isoformat(),  # Finance processing step.
                "reference": a.payment.document_number,  # Finance processing step.
                "method": a.payment.method,  # Finance processing step.
                "amount": _money(a.amount),  # Finance processing step.
            }  # Continue structured finance payload.
            for a in inv.allocations.all()  # Iterate through finance records.
        ]  # Continue structured finance payload.

        # Posted concessions (discounts/waivers/scholarships) on this invoice.
        concessions = [c for c in inv.concessions.all() if c.status == "POSTED"]  # Store intermediate finance value.

        # Every way this invoice was settled down: cash, credit notes, concessions,
        # write-offs.
        settlements = [dict(row, type="PAYMENT") for row in payments]  # Store intermediate finance value.
        for a in inv.credit_allocations.all():  # Iterate through finance records.
            settlements.append({  # Finance processing step.
                "type": "CREDIT_NOTE",  # Finance processing step.
                "date": a.note.note_date.isoformat(),  # Finance processing step.
                "reference": a.note.document_number,  # Finance processing step.
                "method": None,  # Finance processing step.
                "amount": _money(a.amount),  # Finance processing step.
            })  # Continue structured finance payload.
        for c in concessions:  # Iterate through finance records.
            settlements.append({  # Finance processing step.
                "type": "CONCESSION",  # Finance processing step.
                "date": c.concession_date.isoformat(),  # Finance processing step.
                "reference": c.document_number,  # Finance processing step.
                "method": None,  # Finance processing step.
                "amount": _money(c.amount),  # Finance processing step.
            })  # Continue structured finance payload.
        for log in writeoff_logs:  # Iterate through finance records.
            j = writeoff_journals.get(int(log.metadata.get("journal_id") or 0))  # Store intermediate finance value.
            settlements.append({  # Finance processing step.
                "type": "WRITE_OFF",  # Finance processing step.
                "date": (j.date.isoformat() if j else log.created_at.date().isoformat()),  # Finance processing step.
                "reference": inv.document_number,  # Finance processing step.
                "method": None,  # Finance processing step.
                "amount": _money(int(log.metadata.get("amount") or 0)),  # Finance processing step.
            })  # Continue structured finance payload.
        settlements.sort(key=lambda x: x["date"])  # Store intermediate finance value.

        # Flat lines of the invoice's own AR journal — kept as `gl_postings` for
        # existing consumers. `gl_journals` is the full GL history: the invoice posting
        # plus every settlement's journal, grouped per source document.
        gl_postings = []  # Store intermediate finance value.
        if inv.journal_id:  # Branch when this finance condition is true.
            for gl in inv.journal.lines.all():  # Iterate through finance records.
                gl_postings.append({  # Finance processing step.
                    "account_code": gl.account.code, "account_name": gl.account.name,  # Finance processing step.
                    "debit": _money(gl.debit), "credit": _money(gl.credit),  # Finance processing step.
                })  # Continue structured finance payload.

        gl_journals = []  # Store intermediate finance value.
        _seen_journals: set[int] = set()  # Store intermediate finance value.

        def _add_journal(j, doc_type, reference, date):  # Function handles this finance operation.
            if j is None or j.id in _seen_journals:  # Branch when this finance condition is true.
                return  # Return control to caller.
            _seen_journals.add(j.id)  # Finance processing step.
            gl_journals.append({  # Finance processing step.
                "document_type": doc_type,  # Finance processing step.
                "reference": reference,  # Finance processing step.
                "date": date,  # Finance processing step.
                "source": j.source,  # Finance processing step.
                "lines": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "account_code": gl.account.code, "account_name": gl.account.name,  # Finance processing step.
                        "debit": _money(gl.debit), "credit": _money(gl.credit),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for gl in j.lines.all()  # Iterate through finance records.
                ],  # Continue structured finance payload.
            })  # Continue structured finance payload.

        _add_journal(inv.journal, "INVOICE", inv.document_number, inv.invoice_date.isoformat())  # Finance processing step.
        for a in inv.allocations.all():  # Iterate through finance records.
            _add_journal(a.payment.journal, "PAYMENT", a.payment.document_number,  # Finance processing step.
                         a.payment.payment_date.isoformat())  # Finance processing step.
        for a in inv.credit_allocations.all():  # Iterate through finance records.
            _add_journal(a.note.journal, "CREDIT_NOTE", a.note.document_number,  # Finance processing step.
                         a.note.note_date.isoformat())  # Finance processing step.
        for c in concessions:  # Iterate through finance records.
            _add_journal(c.journal, "CONCESSION", c.document_number,  # Finance processing step.
                         c.concession_date.isoformat())  # Finance processing step.
        for log in writeoff_logs:  # Iterate through finance records.
            j = writeoff_journals.get(int(log.metadata.get("journal_id") or 0))  # Store intermediate finance value.
            if j is not None:  # Branch when this finance condition is true.
                _add_journal(j, "WRITE_OFF", inv.document_number, j.date.isoformat())  # Finance processing step.
        gl_journals.sort(key=lambda x: x["date"])  # Store intermediate finance value.

        reminders = [  # Store intermediate finance value.
            {  # Continue structured finance payload.
                "date": (d.notice_date or d.created_at.date()).isoformat(),  # Finance processing step.
                "level": d.level,  # Finance processing step.
                "channel": d.channel or "",  # Finance processing step.
                "status": d.notice_status,  # Finance processing step.
            }  # Continue structured finance payload.
            for d in inv.dunning_notices.all()  # Iterate through finance records.
        ]  # Continue structured finance payload.

        activity = [{"date": inv.invoice_date.isoformat(), "label": "Invoice created"}]  # Store intermediate finance value.
        for a in inv.allocations.all():  # Iterate through finance records.
            activity.append({  # Finance processing step.
                "date": a.payment.payment_date.isoformat(),  # Finance processing step.
                "label": f"Payment {a.payment.document_number} ({format_naira(a.amount)})",  # Finance processing step.
            })  # Continue structured finance payload.
        for a in inv.credit_allocations.all():  # Iterate through finance records.
            activity.append({  # Finance processing step.
                "date": a.note.note_date.isoformat(),  # Finance processing step.
                "label": f"Credit note {a.note.document_number} ({format_naira(a.amount)})",  # Finance processing step.
            })  # Continue structured finance payload.
        for c in concessions:  # Iterate through finance records.
            activity.append({  # Finance processing step.
                "date": c.concession_date.isoformat(),  # Finance processing step.
                "label": f"{c.get_kind_display()} {c.document_number} ({format_naira(c.amount)})",  # Finance processing step.
            })  # Continue structured finance payload.
        for log in writeoff_logs:  # Iterate through finance records.
            j = writeoff_journals.get(int(log.metadata.get("journal_id") or 0))  # Store intermediate finance value.
            amount = int(log.metadata.get("amount") or 0)  # Store intermediate finance value.
            activity.append({  # Finance processing step.
                "date": (j.date.isoformat() if j else log.created_at.date().isoformat()),  # Finance processing step.
                "label": f"Write-off ({format_naira(amount)})",  # Finance processing step.
            })  # Continue structured finance payload.
        for r in reminders:  # Iterate through finance records.
            activity.append({"date": r["date"], "label": f"Reminder level {r['level']} — {r['status']}"})  # Finance processing step.
        activity.sort(key=lambda x: x["date"])  # Store intermediate finance value.

        return success_response(  # Return the computed finance response.
            "Invoice retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "invoice": InvoiceSerializer(inv).data,  # Finance processing step.
                "summary": {  # Finance processing step.
                    "subtotal": _money(inv.subtotal), "tax": _money(inv.tax_total),  # Finance processing step.
                    "total": _money(inv.total),  # Finance processing step.
                    "paid": _money(inv.amount_paid),  # Finance processing step.
                    "credited": _money(inv.amount_credited),  # Finance processing step.
                    "settled": _money(inv.settled_amount),  # Finance processing step.
                    "balance": _money(inv.balance_due),  # Finance processing step.
                    "due_date": inv.due_date.isoformat() if inv.due_date else None,  # Finance processing step.
                },  # Continue structured finance payload.
                "lines": lines,  # Finance processing step.
                "payments": payments,  # Finance processing step.
                "settlements": settlements,  # Finance processing step.
                "gl_postings": gl_postings,  # Finance processing step.
                "gl_journals": gl_journals,  # Finance processing step.
                "reminders": reminders,  # Finance processing step.
                "activity": activity,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class InvoiceDocumentView(APIView):  # Class groups related finance API or service behavior.
    """GET /finance/invoices/<id>/document/ — printable HTML invoice."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.invoice.view"  # Store intermediate finance value.

    def _invoice(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        inv = (  # Store intermediate finance value.
            Invoice.objects.filter(entity=entity, pk=pk)  # Query finance data from the database.
            .select_related("entity__source_school", "branch", "customer")  # Finance processing step.
            .prefetch_related("lines__revenue_account", "lines__tax_code", "lines__cost_center")  # Finance processing step.
            .first()  # Finance processing step.
        )  # Continue structured finance payload.
        if inv is None:  # Branch when this finance condition is true.
            raise NotFound("No such invoice in this entity.")  # Surface validation or finance error.
        return inv  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        from .documents import render_invoice_document_html  # Import dependency used by this finance module.

        html = render_invoice_document_html(self._invoice(request, pk), request=request)  # Store intermediate finance value.
        return HttpResponse(html, content_type="text/html; charset=utf-8")  # Return the computed finance response.


class InvoiceDocumentPDFView(InvoiceDocumentView):  # Class groups related finance API or service behavior.
    """GET /finance/invoices/<id>/document.pdf — printable PDF invoice."""

    def get(self, request, pk):  # Function handles this finance operation.
        from .documents import DocumentRenderUnavailable, render_invoice_document_pdf  # Import dependency used by this finance module.

        invoice = self._invoice(request, pk)  # Store intermediate finance value.
        try:  # Start protected finance operation.
            pdf = render_invoice_document_pdf(invoice, request=request)  # Store intermediate finance value.
        except DocumentRenderUnavailable:  # Handle finance operation failure.
            return Response(  # Return the computed finance response.
                {"detail": "PDF rendering is unavailable on this server."},  # Continue structured finance payload.
                status=503,  # Store intermediate finance value.
            )  # Continue structured finance payload.
        response = HttpResponse(pdf, content_type="application/pdf")  # Store intermediate finance value.
        filename = f"invoice-{invoice.document_number or invoice.pk}.pdf"  # Store intermediate finance value.
        response["Content-Disposition"] = f'inline; filename="{filename}"'  # Store intermediate finance value.
        return response  # Return the computed finance response.


# --------------------------------------------------------------------------- #
# Actions                                                                     #
# --------------------------------------------------------------------------- #

class JournalSubmitView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/journals/<id>/submit/?entity= — submit a draft journal for approval.

    Hands the journal to the ``vs_workflow`` engine via
    :func:`vs_workflow.services.submission.submit_for_approval`. The handler's
    ``validate_document`` runs the posting preflight now (so a doomed journal is
    refused before it enters the queue) and moves the journal to
    ``PENDING_APPROVAL``; the GL is not touched until final approval fires the
    handler's ``on_approved`` posting. Only meaningful when a template exists for
    ``finance.journal`` at this journal's scope (see :func:`approvals.approval_required`).

    docstring-name: Submit a journal for approval
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.journal.submit"  # Store intermediate finance value.

    def post(self, request, id):  # Function handles this finance operation.
        from vs_workflow.services.submission import submit_for_approval  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        entry = JournalEntry.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if entry is None:  # Branch when this finance condition is true.
            raise NotFound("Journal entry not found for this entity.")  # Surface validation or finance error.
        submit_for_approval(entry, requested_by=request.user)  # Store intermediate finance value.
        entry.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            message=f"Journal {entry.document_number} submitted for approval.",  # Store intermediate finance value.
            data=JournalEntryDetailSerializer(entry).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class JournalPostView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/journals/<id>/post/?entity= — post a draft journal.

    When a workflow template is published for this journal's ``finance.journal``
    document type (opt-in gate), direct posting is refused: the journal must go
    through ``/submit/`` and posts only on approval. With no template, this behaves
    exactly as it always has — a direct draft → POSTED post.

    docstring-name: Post a journal entry
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.journal.post"  # Store intermediate finance value.

    def post(self, request, id):  # Function handles this finance operation.
        from .approvals import approval_required  # Import dependency used by this finance module.
        from .posting import post_journal  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        entry = JournalEntry.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if entry is None:  # Branch when this finance condition is true.
            raise NotFound("Journal entry not found for this entity.")  # Surface validation or finance error.
        if approval_required(entry):  # Branch when this finance condition is true.
            raise ValidationError({  # Surface validation or finance error.
                "detail": "This journal is approval-gated; submit it for approval "  # Finance processing step.
                          "instead of posting directly.",  # Finance processing step.
            })  # Continue structured finance payload.
        post_journal(entry, actor_user=request.user)  # Store intermediate finance value.
        entry.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            message=f"Journal {entry.document_number} posted.",  # Store intermediate finance value.
            data=JournalEntryDetailSerializer(entry).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class JournalReverseView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/journals/<id>/reverse/?entity= — reverse a posted journal.

    docstring-name: Reverse a journal entry
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.journal.reverse"  # Store intermediate finance value.

    def post(self, request, id):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.
        from .posting import reverse_journal  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        entry = JournalEntry.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if entry is None:  # Branch when this finance condition is true.
            raise NotFound("Journal entry not found for this entity.")  # Surface validation or finance error.
        # Optional reversal date; when omitted the service reverses into the original
        # period, or into the current open period if that period has since closed.
        body = request.data or {}  # Store intermediate finance value.
        raw_date = body.get("date") or body.get("reversal_date")  # Store intermediate finance value.
        rdate = None  # Store intermediate finance value.
        if raw_date:  # Branch when this finance condition is true.
            try:  # Start protected finance operation.
                rdate = datetime.date.fromisoformat(str(raw_date))  # Store intermediate finance value.
            except ValueError:  # Handle finance operation failure.
                raise ValidationError({"date": "Expected an ISO date (YYYY-MM-DD)."})  # Surface validation or finance error.
        reversal = reverse_journal(entry, actor_user=request.user, date=rdate)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message=f"Journal {entry.document_number} reversed.",  # Store intermediate finance value.
            data=JournalEntryDetailSerializer(reversal).data,  # Store intermediate finance value.
            status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DirectEntryCreateView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/direct-entries/?entity= — post a direct journal entry.

    Body: ``{"date"?, "narration"?, "reference"?, "lines": [{"account", "debit"|"credit"}]}``
    with amounts in kobo. The one sanctioned way to book money/balances that have no sub-ledger
    document behind them — capital injections, equity contributions, loan drawdowns, grants,
    opening balances and manual adjustments. Every other journal is a side-effect of an action.

    docstring-name: Post a direct entry
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.directentry.post"  # Store intermediate finance value.

    def post(self, request):  # Function handles this finance operation.
        from .posting import post_direct_entry  # Import dependency used by this finance module.
        from .views_ops import _resolve_cost_center, _resolve_dimensions  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        serializer = DirectEntryCreateSerializer(data=request.data)  # Store intermediate finance value.
        serializer.is_valid(raise_exception=True)  # Store intermediate finance value.
        data = serializer.validated_data  # Store intermediate finance value.
        # Resolve each line's optional cost centre + analytical dimensions against this
        # entity (raises a ValidationError on an unknown code/value) and carry both
        # through to the GL line.
        lines = [  # Store intermediate finance value.
            (  # Continue structured finance payload.
                ln["account"], ln["debit"], ln["credit"],  # Finance processing step.
                _resolve_cost_center(entity, ln.get("cost_center"), "lines.cost_center"),  # Finance processing step.
                _resolve_dimensions(entity, ln.get("dimensions"), "lines.dimensions"),  # Finance processing step.
            )  # Continue structured finance payload.
            for ln in data["lines"]  # Iterate through finance records.
        ]  # Continue structured finance payload.
        entry = post_direct_entry(  # Store intermediate finance value.
            entity, lines=lines,  # Store intermediate finance value.
            date=data.get("date"), narration=data.get("narration", ""),  # Store intermediate finance value.
            reference=data.get("reference", ""), actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            message=f"Direct entry posted as {entry.document_number}.",  # Store intermediate finance value.
            data=JournalEntryDetailSerializer(entry).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PeriodCloseView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/periods/<id>/close/?entity= — run the checklist and close a period.

    Body (all optional): ``{"soft": bool, "force": bool, "run_depreciation": bool}``.

    docstring-name: Close a fiscal period
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.period.close" if self.request.method == "POST" else "finance.period.view"  # Return the computed finance response.

    def _period(self, request, id):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        period = FiscalPeriod.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if period is None:  # Branch when this finance condition is true.
            raise NotFound("Fiscal period not found for this entity.")  # Surface validation or finance error.
        return entity, period  # Return the computed finance response.

    def get(self, request, id):  # Function handles this finance operation.
        """Preview the close checklist for a period (no side effects)."""
        from .close import close_checklist  # Import dependency used by this finance module.

        entity, period = self._period(request, id)  # Store intermediate finance value.
        checklist = close_checklist(entity, period)  # Store intermediate finance value.
        items = _serialize_checklist(checklist)["items"]  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message=f"Close checklist for '{period}'.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "period": FiscalPeriodSerializer(period).data,  # Finance processing step.
                "passed": checklist.passed,  # Finance processing step.
                "done": sum(1 for i in items if i["passed"]),  # Finance processing step.
                "total": len(items),  # Finance processing step.
                "items": items,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.

    def post(self, request, id):  # Function handles this finance operation.
        from .close import close_period  # Import dependency used by this finance module.

        entity, period = self._period(request, id)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        period, checklist = close_period(  # Store intermediate finance value.
            entity, period, actor_user=request.user,  # Store intermediate finance value.
            soft=bool(body.get("soft", False)),  # Store intermediate finance value.
            force=bool(body.get("force", False)),  # Store intermediate finance value.
            run_depreciation=bool(body.get("run_depreciation", True)),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            message=f"Period '{period}' closed to {period.status}.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "period": FiscalPeriodSerializer(period).data,  # Finance processing step.
                "checklist": _serialize_checklist(checklist),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class PeriodReopenView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/periods/<id>/reopen/?entity= — re-open a CLOSED/SOFT_CLOSED period.

    A LOCKED period cannot be re-opened; an already-OPEN period is refused.

    docstring-name: Re-open a fiscal period
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.period.reopen"  # Store intermediate finance value.

    def _period(self, request, id):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        period = FiscalPeriod.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if period is None:  # Branch when this finance condition is true.
            raise NotFound("Fiscal period not found for this entity.")  # Surface validation or finance error.
        return entity, period  # Return the computed finance response.

    def post(self, request, id):  # Function handles this finance operation.
        from .close import reopen_period  # Import dependency used by this finance module.

        entity, period = self._period(request, id)  # Store intermediate finance value.
        period = reopen_period(entity, period, actor_user=request.user)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message=f"Period '{period}' re-opened to {period.status}.",  # Store intermediate finance value.
            data=FiscalPeriodSerializer(period).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PeriodLockView(APIView):  # Class groups related finance API or service behavior.
    """POST /finance/periods/<id>/lock/?entity= — permanently seal a CLOSED period.

    Only a CLOSED period can be locked; the lock is irreversible.

    docstring-name: Lock a fiscal period
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.period.lock"  # Store intermediate finance value.

    def _period(self, request, id):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        period = FiscalPeriod.objects.filter(entity=entity, id=id).first()  # Query finance data from the database.
        if period is None:  # Branch when this finance condition is true.
            raise NotFound("Fiscal period not found for this entity.")  # Surface validation or finance error.
        return entity, period  # Return the computed finance response.

    def post(self, request, id):  # Function handles this finance operation.
        from .close import lock_period  # Import dependency used by this finance module.

        entity, period = self._period(request, id)  # Store intermediate finance value.
        period = lock_period(entity, period, actor_user=request.user)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message=f"Period '{period}' locked to {period.status}.",  # Store intermediate finance value.
            data=FiscalPeriodSerializer(period).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Reports / financial statements                                              #
# --------------------------------------------------------------------------- #

def _money(amount):  # Function handles this finance operation.
    return {"kobo": amount, "naira": format_naira(amount)}  # Return the computed finance response.


def _serialize_checklist(checklist):  # Function handles this finance operation.
    return {  # Return the computed finance response.
        "passed": checklist.passed,  # Finance processing step.
        "items": [  # Finance processing step.
            {"name": i.name, "passed": i.passed, "blocking": i.blocking, "detail": i.detail}  # Continue structured finance payload.
            for i in checklist.items  # Iterate through finance records.
        ],  # Continue structured finance payload.
    }  # Continue structured finance payload.


def _line(row):  # Function handles this finance operation.
    return {  # Return the computed finance response.
        "account_id": row.account_id, "code": row.code, "name": row.name,  # Finance processing step.
        "account_type": row.account_type, "amount": _money(row.amount),  # Finance processing step.
    }  # Continue structured finance payload.


def _maybe_export(request, table, *, filename):  # Function handles this finance operation.
    """If ``?export=csv|xlsx|pdf`` is set, render ``table`` to a file download.

    Returns an :class:`HttpResponse` attachment, or ``None`` when no export was asked
    for (the caller then returns its normal JSON envelope). An unknown format becomes a
    DRF :class:`ValidationError` (rendered as a 400 by the custom exception handler).

    Note: the parameter is ``export`` (not ``format``) because DRF reserves ``?format=``
    for renderer content negotiation.
    """
    fmt = request.query_params.get("export")  # Store intermediate finance value.
    if not fmt:  # Branch when this finance condition is true.
        return None  # Return the computed finance response.
    from .exports import render  # Import dependency used by this finance module.

    try:  # Start protected finance operation.
        body, content_type, ext = render(table, fmt)  # Store intermediate finance value.
    except ValueError as exc:  # Handle finance operation failure.
        raise ValidationError({"export": str(exc)})  # Surface validation or finance error.
    resp = HttpResponse(body, content_type=content_type)  # Store intermediate finance value.
    resp["Content-Disposition"] = f'attachment; filename="{filename}.{ext}"'  # Store intermediate finance value.
    return resp  # Return the computed finance response.


class TrialBalanceView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Trial balance"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import trial_balance  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        tb = trial_balance(entity, period=period)  # Store intermediate finance value.

        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Trial Balance",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'All periods'}",  # Store intermediate finance value.
            columns=["Code", "Account", "Type", "Debit", "Credit"],  # Store intermediate finance value.
            rows=[[r.code, r.name, r.account_type, r.debit_naira, r.credit_naira] for r in tb.rows],  # Store intermediate finance value.
            summary_rows=[["", "TOTAL", "", format_naira(tb.total_debit), format_naira(tb.total_credit)]],  # Store intermediate finance value.
        ), filename=f"trial_balance_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="Trial balance retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "period": getattr(period, "name", None),  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "account_id": r.account_id, "code": r.code, "name": r.name,  # Finance processing step.
                        "account_type": r.account_type,  # Finance processing step.
                        "debit": _money(r.debit), "credit": _money(r.credit),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in tb.rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "total_debit": _money(tb.total_debit),  # Finance processing step.
                "total_credit": _money(tb.total_credit),  # Finance processing step.
                "is_balanced": tb.is_balanced,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class IncomeStatementView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Income statement"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import income_statement_compare  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        rep = income_statement_compare(entity, period=period)  # Store intermediate finance value.

        def _mon(v):  # Function handles this finance operation.
            return _money(v) if v is not None else None  # Return the computed finance response.

        def _isline(line):  # Function handles this finance operation.
            return {  # Return the computed finance response.
                "account_id": line.account_id, "code": line.code, "name": line.name,  # Finance processing step.
                "account_type": line.account_type, "amount": _money(line.amount),  # Finance processing step.
                "budget": _mon(line.budget), "variance": _mon(line.variance),  # Finance processing step.
                "prior_year": _mon(line.prior_year),  # Finance processing step.
            }  # Continue structured finance payload.

        def _istot(t):  # Function handles this finance operation.
            return {  # Return the computed finance response.
                "amount": _money(t.amount), "budget": _mon(t.budget),  # Finance processing step.
                "variance": _mon(t.variance), "prior_year": _mon(t.prior_year),  # Finance processing step.
            }  # Continue structured finance payload.

        # Export columns mirror the comparison the data supports.
        cols = ["Section", "Code", "Account", "This period"]  # Store intermediate finance value.
        if rep.has_budget:  # Branch when this finance condition is true.
            cols += ["Budget", "Variance"]  # Store intermediate finance value.
        if rep.has_prior_year:  # Branch when this finance condition is true.
            cols += ["Prior year"]  # Store intermediate finance value.

        def _xrow(section, line):  # Function handles this finance operation.
            row = [section, line.code, line.name, format_naira(line.amount)]  # Store intermediate finance value.
            if rep.has_budget:  # Branch when this finance condition is true.
                row += [format_naira(line.budget or 0), format_naira(line.variance or 0)]  # Store intermediate finance value.
            if rep.has_prior_year:  # Branch when this finance condition is true.
                row += [format_naira(line.prior_year or 0)]  # Store intermediate finance value.
            return row  # Return the computed finance response.

        def _xtot(label, t):  # Function handles this finance operation.
            row = ["", "", label, format_naira(t.amount)]  # Store intermediate finance value.
            if rep.has_budget:  # Branch when this finance condition is true.
                row += [format_naira(t.budget or 0), format_naira(t.variance or 0)]  # Store intermediate finance value.
            if rep.has_prior_year:  # Branch when this finance condition is true.
                row += [format_naira(t.prior_year or 0)]  # Store intermediate finance value.
            return row  # Return the computed finance response.

        rows = [_xrow("Revenue", r) for r in rep.income_rows]  # Store intermediate finance value.
        rows += [_xrow("Expense", r) for r in rep.expense_rows]  # Store intermediate finance value.
        scope = rep.period_name or (f"FY{rep.fiscal_year}" if rep.fiscal_year else "Year to date")  # Store intermediate finance value.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Income Statement",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {scope}",  # Store intermediate finance value.
            columns=cols,  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[  # Store intermediate finance value.
                _xtot("Total revenue", rep.income_totals),  # Finance processing step.
                _xtot("Total expenses", rep.expense_totals),  # Finance processing step.
                _xtot("Net income", rep.net_totals),  # Finance processing step.
            ],  # Continue structured finance payload.
        ), filename=f"income_statement_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="Income statement retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "period": rep.period_name,  # Finance processing step.
                "fiscal_year": rep.fiscal_year,  # Finance processing step.
                "prior_fiscal_year": rep.prior_fiscal_year,  # Finance processing step.
                "has_budget": rep.has_budget,  # Finance processing step.
                "has_prior_year": rep.has_prior_year,  # Finance processing step.
                "income": [_isline(r) for r in rep.income_rows],  # Finance processing step.
                "expense": [_isline(r) for r in rep.expense_rows],  # Finance processing step.
                "totals": {  # Finance processing step.
                    "income": _istot(rep.income_totals),  # Finance processing step.
                    "expense": _istot(rep.expense_totals),  # Finance processing step.
                    "net": _istot(rep.net_totals),  # Finance processing step.
                },  # Continue structured finance payload.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class BalanceSheetView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Balance sheet"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import balance_sheet_sections  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        as_of = request.query_params.get("as_of") or None  # Store intermediate finance value.
        bs = balance_sheet_sections(entity, as_of=as_of)  # Store intermediate finance value.

        def _group(g):  # Function handles this finance operation.
            return {  # Return the computed finance response.
                "line": g.line, "label": g.label, "amount": _money(g.amount),  # Finance processing step.
                "accounts": [  # Finance processing step.
                    {"account_id": a["account_id"], "code": a["code"],  # Continue structured finance payload.
                     "name": a["name"], "amount": _money(a["amount"])}  # Finance processing step.
                    for a in g.accounts  # Iterate through finance records.
                ],  # Continue structured finance payload.
            }  # Continue structured finance payload.

        def _section(s):  # Function handles this finance operation.
            return {  # Return the computed finance response.
                "key": s.key, "label": s.label, "total": _money(s.total),  # Finance processing step.
                "groups": [_group(g) for g in s.groups],  # Finance processing step.
            }  # Continue structured finance payload.

        rows = []  # Store intermediate finance value.
        for s in bs.sections:  # Iterate through finance records.
            for g in s.groups:  # Iterate through finance records.
                rows.append([s.label, g.label, format_naira(g.amount)])  # Finance processing step.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Balance Sheet",  # Store intermediate finance value.
            subtitle=f"{entity.code} · as at {bs.as_of}",  # Store intermediate finance value.
            columns=["Section", "Line", "Amount"],  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[  # Store intermediate finance value.
                ["", "Total assets", format_naira(bs.total_assets)],  # Continue structured finance payload.
                ["", "Total liabilities", format_naira(bs.total_liabilities)],  # Continue structured finance payload.
                ["", "Total equity", format_naira(bs.total_equity)],  # Continue structured finance payload.
            ],  # Continue structured finance payload.
        ), filename=f"balance_sheet_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="Balance sheet retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "as_of": str(bs.as_of),  # Finance processing step.
                "sections": [_section(s) for s in bs.sections],  # Finance processing step.
                "total_assets": _money(bs.total_assets),  # Finance processing step.
                "total_liabilities": _money(bs.total_liabilities),  # Finance processing step.
                "total_equity": _money(bs.total_equity),  # Finance processing step.
                "retained_earnings": _money(bs.current_year_earnings),  # Finance processing step.
                "is_balanced": bs.is_balanced,  # Finance processing step.
                "difference": _money(bs.difference),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class CashFlowView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Cash flow statement"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import cash_flow_statement  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        cf = cash_flow_statement(entity, period=period)  # Store intermediate finance value.

        _ACT_LABEL = {  # Store intermediate finance value.
            "operating": "Operating activities",  # Finance processing step.
            "investing": "Investing activities",  # Finance processing step.
            "financing": "Financing activities",  # Finance processing step.
        }  # Continue structured finance payload.
        rows = []  # Store intermediate finance value.
        for act in ("operating", "investing", "financing"):  # Iterate through finance records.
            for ln in cf.activity_lines[act]:  # Iterate through finance records.
                rows.append([_ACT_LABEL[act], ln.name, format_naira(ln.amount)])  # Finance processing step.
            rows.append([_ACT_LABEL[act], f"Net cash from {act}",  # Finance processing step.
                         format_naira(cf.by_activity[act])])  # Finance processing step.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Cash Flow Statement",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'Year to date'}",  # Store intermediate finance value.
            columns=["Activity", "Line", "Amount"],  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[  # Store intermediate finance value.
                ["", "Net change in cash", format_naira(cf.net_change)],  # Continue structured finance payload.
                ["", "Cash at start of period", format_naira(cf.opening_cash)],  # Continue structured finance payload.
                ["", "Cash at end of period", format_naira(cf.closing_cash)],  # Continue structured finance payload.
            ],  # Continue structured finance payload.
        ), filename=f"cash_flow_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        def _cfline(ln):  # Function handles this finance operation.
            return {"account_id": ln.account_id, "code": ln.code,  # Return the computed finance response.
                    "name": ln.name, "amount": _money(ln.amount)}  # Finance processing step.

        return success_response(  # Return the computed finance response.
            message="Cash flow statement retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "period": getattr(period, "name", None),  # Finance processing step.
                "opening_cash": _money(cf.opening_cash),  # Finance processing step.
                "closing_cash": _money(cf.closing_cash),  # Finance processing step.
                "by_activity": {k: _money(v) for k, v in cf.by_activity.items()},  # Finance processing step.
                "activity_lines": {  # Finance processing step.
                    k: [_cfline(ln) for ln in v] for k, v in cf.activity_lines.items()  # Finance processing step.
                },  # Continue structured finance payload.
                "net_change": _money(cf.net_change),  # Finance processing step.
                "is_reconciled": cf.is_reconciled,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class AnalyticsSliceView(APIView):  # Class groups related finance API or service behavior.
    """GET /finance/reports/analytics-slice/?entity=&axis= — net activity per account,
    bucketed by one analytical axis (a cost centre or a dimension).

    ``axis`` is required: either ``cost_center`` or a registered Dimension code (e.g.
    ``FUND``). Optional ``period`` and ``account_type`` narrow the slice.

    docstring-name: Analytics slice
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .models import Dimension  # Import dependency used by this finance module.
        from .reports import analytics_slice  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        axis = (request.query_params.get("axis") or "").strip()  # Store intermediate finance value.
        if not axis:  # Branch when this finance condition is true.
            raise ValidationError({"axis": "An 'axis' query parameter is required "  # Surface validation or finance error.
                                           "('cost_center' or a dimension code)."})  # Finance processing step.
        if axis != "cost_center" and not Dimension.objects.filter(  # Branch when this finance condition is true.
            entity=entity, code=axis, is_active=True  # Store intermediate finance value.
        ).exists():  # Continue structured finance payload.
            raise ValidationError(  # Surface validation or finance error.
                {"axis": f"'{axis}' is not 'cost_center' or an active dimension in this entity."})  # Continue structured finance payload.

        period = _resolve_period(entity, request)  # Store intermediate finance value.
        account_type = request.query_params.get("account_type") or None  # Store intermediate finance value.
        sl = analytics_slice(entity, axis=axis, period=period, account_type=account_type)  # Store intermediate finance value.

        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title=f"Analytics Slice · {axis}",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'All periods'}",  # Store intermediate finance value.
            columns=["Bucket", "Code", "Account", "Type", "Net"],  # Store intermediate finance value.
            rows=[[r.bucket, r.code, r.name, r.account_type, r.net_naira] for r in sl.rows],  # Store intermediate finance value.
            summary_rows=[["", "", "TOTAL", "", format_naira(sl.total_net)]],  # Store intermediate finance value.
        ), filename=f"analytics_slice_{axis}_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="Analytics slice retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "period": getattr(period, "name", None),  # Finance processing step.
                "axis": sl.axis,  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "bucket": r.bucket, "account_id": r.account_id,  # Finance processing step.
                        "code": r.code, "name": r.name, "account_type": r.account_type,  # Finance processing step.
                        "debit": _money(r.debit), "credit": _money(r.credit),  # Finance processing step.
                        "net": _money(r.net),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in sl.rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "bucket_totals": {k: _money(v) for k, v in sl.bucket_totals.items()},  # Finance processing step.
                "total_net": _money(sl.total_net),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class ChangesInEquityView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Statement of changes in equity"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import statement_of_changes_in_equity  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        soce = statement_of_changes_in_equity(entity, period=period)  # Store intermediate finance value.

        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Statement of Changes in Equity",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {getattr(period, 'name', None) or 'Inception to date'}",  # Store intermediate finance value.
            columns=["Component", "Opening", "Profit", "Contributions/(Distributions)", "Closing"],  # Store intermediate finance value.
            rows=[  # Store intermediate finance value.
                [c.label, c.opening_naira, c.profit_naira, c.contributions_naira, c.closing_naira]  # Continue structured finance payload.
                for c in soce.columns  # Iterate through finance records.
            ],  # Continue structured finance payload.
            summary_rows=[[  # Store intermediate finance value.
                "TOTAL",  # Finance processing step.
                format_naira(soce.total_opening), format_naira(soce.total_profit),  # Finance processing step.
                format_naira(soce.total_contributions), format_naira(soce.total_closing),  # Finance processing step.
            ]],  # Continue structured finance payload.
        ), filename=f"changes_in_equity_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="Statement of changes in equity retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "period": getattr(period, "name", None),  # Finance processing step.
                "as_of": str(soce.as_of),  # Finance processing step.
                "columns": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "key": c.key, "label": c.label, "code": c.code,  # Finance processing step.
                        "account_id": c.account_id,  # Finance processing step.
                        "opening": _money(c.opening), "profit": _money(c.profit),  # Finance processing step.
                        "contributions": _money(c.contributions), "closing": _money(c.closing),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for c in soce.columns  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "total_opening": _money(soce.total_opening),  # Finance processing step.
                "total_profit": _money(soce.total_profit),  # Finance processing step.
                "total_contributions": _money(soce.total_contributions),  # Finance processing step.
                "total_closing": _money(soce.total_closing),  # Finance processing step.
                "balance_sheet_equity": _money(soce.balance_sheet_equity),  # Finance processing step.
                "is_reconciled": soce.is_reconciled,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class StatutoryPackView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: Statutory reporting pack"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import statutory_pack  # Import dependency used by this finance module.

        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        as_of = request.query_params.get("as_of") or None  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        pack = statutory_pack(entity, as_of=as_of, period=period)  # Store intermediate finance value.

        # Export face: the IFRS-mapped Statement of Financial Position + Income
        # Statement as one flat table (the companion statements have their own exports).
        rows: list = []  # Store intermediate finance value.
        for section in pack.sofp_sections:  # Iterate through finance records.
            rows.append([section.label.upper(), "", ""])  # Finance processing step.
            for g in section.groups:  # Iterate through finance records.
                rows.append(["", g.label, g.amount_naira])  # Finance processing step.
            rows.append(["", f"  Total {section.label.lower()}", section.total_naira])  # Finance processing step.
        rows.append(["INCOME STATEMENT", "", ""])  # Finance processing step.
        for g in pack.income_lines:  # Iterate through finance records.
            rows.append(["", g.label, g.amount_naira])  # Finance processing step.
        rows.append(["", "  Net income", format_naira(pack.net_income)])  # Finance processing step.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Statutory Pack (IFRS for SMEs)",  # Store intermediate finance value.
            subtitle=f"{entity.code} · as at {pack.as_of}",  # Store intermediate finance value.
            columns=["Section", "Line", "Amount"],  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[  # Store intermediate finance value.
                ["", "Total assets", format_naira(pack.total_assets)],  # Continue structured finance payload.
                ["", "Total equity", format_naira(pack.total_equity)],  # Continue structured finance payload.
                ["", "Total liabilities", format_naira(pack.total_liabilities)],  # Continue structured finance payload.
            ],  # Continue structured finance payload.
        ), filename=f"statutory_pack_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        def _group(g):  # Function handles this finance operation.
            return {  # Return the computed finance response.
                "line": g.line, "label": g.label, "amount": _money(g.amount),  # Finance processing step.
                "accounts": [  # Finance processing step.
                    {"account_id": a["account_id"], "code": a["code"],  # Continue structured finance payload.
                     "name": a["name"], "amount": _money(a["amount"])}  # Finance processing step.
                    for a in g.accounts  # Iterate through finance records.
                ],  # Continue structured finance payload.
            }  # Continue structured finance payload.

        cf = pack.cash_flow  # Store intermediate finance value.
        soce = pack.changes_in_equity  # Store intermediate finance value.
        tb = pack.trial_balance  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message="Statutory pack retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "as_of": str(pack.as_of),  # Finance processing step.
                "period": getattr(period, "name", None),  # Finance processing step.
                "statement_of_financial_position": {  # Finance processing step.
                    "sections": [  # Finance processing step.
                        {  # Continue structured finance payload.
                            "key": s.key, "label": s.label,  # Finance processing step.
                            "groups": [_group(g) for g in s.groups],  # Finance processing step.
                            "total": _money(s.total),  # Finance processing step.
                        }  # Continue structured finance payload.
                        for s in pack.sofp_sections  # Iterate through finance records.
                    ],  # Continue structured finance payload.
                    "total_assets": _money(pack.total_assets),  # Finance processing step.
                    "total_equity": _money(pack.total_equity),  # Finance processing step.
                    "total_liabilities": _money(pack.total_liabilities),  # Finance processing step.
                    "is_balanced": pack.is_balanced,  # Finance processing step.
                },  # Continue structured finance payload.
                "income_statement": {  # Finance processing step.
                    "lines": [_group(g) for g in pack.income_lines],  # Finance processing step.
                    "total_income": _money(pack.total_income),  # Finance processing step.
                    "total_expense": _money(pack.total_expense),  # Finance processing step.
                    "net_income": _money(pack.net_income),  # Finance processing step.
                },  # Continue structured finance payload.
                "cash_flow": {  # Finance processing step.
                    "opening_cash": _money(cf.opening_cash),  # Finance processing step.
                    "closing_cash": _money(cf.closing_cash),  # Finance processing step.
                    "by_activity": {k: _money(v) for k, v in cf.by_activity.items()},  # Finance processing step.
                    "net_change": _money(cf.net_change),  # Finance processing step.
                    "is_reconciled": cf.is_reconciled,  # Finance processing step.
                },  # Continue structured finance payload.
                "changes_in_equity": {  # Finance processing step.
                    "total_opening": _money(soce.total_opening),  # Finance processing step.
                    "total_profit": _money(soce.total_profit),  # Finance processing step.
                    "total_contributions": _money(soce.total_contributions),  # Finance processing step.
                    "total_closing": _money(soce.total_closing),  # Finance processing step.
                    "is_reconciled": soce.is_reconciled,  # Finance processing step.
                },  # Continue structured finance payload.
                "trial_balance": {  # Finance processing step.
                    "total_debit": _money(tb.total_debit),  # Finance processing step.
                    "total_credit": _money(tb.total_credit),  # Finance processing step.
                    "is_balanced": tb.is_balanced,  # Finance processing step.
                },  # Continue structured finance payload.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class FinanceDashboardView(APIView):  # Class groups related finance API or service behavior.
    """Aggregated **Finance overview** — every dashboard block in one payload.

    Computed live from the GL and entity-scoped. Optional ``?period=<period_no>``
    pins the "as of" period; otherwise the latest open period is used.

    docstring-name: Finance dashboard
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .dashboard import finance_dashboard  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        period = _resolve_period(entity, request)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message="Finance dashboard retrieved.",  # Store intermediate finance value.
            data=finance_dashboard(entity, period=period),  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ARAgingView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: AR aging report"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import ar_aging  # Import dependency used by this finance module.

        from .reports import AGING_BUCKETS  # Import dependency used by this finance module.
        from .exports import ReportTable  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        as_of = request.query_params.get("as_of") or None  # Store intermediate finance value.
        report = ar_aging(entity, as_of=as_of)  # Store intermediate finance value.

        columns = ["Code", "Customer"] + list(AGING_BUCKETS) + ["Net"]  # Store intermediate finance value.
        rows = [  # Store intermediate finance value.
            [r.code, r.name] + [format_naira(r.buckets[b]) for b in AGING_BUCKETS]  # Continue structured finance payload.
            + [format_naira(r.net)]  # Finance processing step.
            for r in report.rows  # Iterate through finance records.
        ]  # Continue structured finance payload.
        summary = ["", "TOTAL"] + [format_naira(report.bucket_totals[b]) for b in AGING_BUCKETS]  # Store intermediate finance value.
        summary += [format_naira(report.total_net)]  # Store intermediate finance value.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title="Accounts Receivable Aging",  # Store intermediate finance value.
            subtitle=f"{entity.code} · as at {report.as_of}",  # Store intermediate finance value.
            columns=columns,  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[summary],  # Store intermediate finance value.
        ), filename=f"ar_aging_{entity.code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            message="AR aging retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "as_of": str(report.as_of),  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "customer_id": r.customer_id, "code": r.code, "name": r.name,  # Finance processing step.
                        "buckets": {b: _money(v) for b, v in r.buckets.items()},  # Finance processing step.
                        "outstanding": _money(r.outstanding),  # Finance processing step.
                        "unallocated_credit": _money(r.unallocated_credit),  # Finance processing step.
                        "net": _money(r.net),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in report.rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "bucket_totals": {b: _money(v) for b, v in report.bucket_totals.items()},  # Finance processing step.
                "total_net": _money(report.total_net),  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class ARReconciliationView(APIView):  # Class groups related finance API or service behavior.
    """docstring-name: AR reconciliation report"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Store intermediate finance value.
    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .reports import reconcile_ar  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        as_of = request.query_params.get("as_of") or None  # Store intermediate finance value.
        rec = reconcile_ar(entity, as_of=as_of)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message="AR reconciliation retrieved.",  # Store intermediate finance value.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "subledger_total": _money(rec.subledger_total),  # Finance processing step.
                "control_total": _money(rec.control_total),  # Finance processing step.
                "difference": _money(rec.difference),  # Finance processing step.
                "is_reconciled": rec.is_reconciled,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
