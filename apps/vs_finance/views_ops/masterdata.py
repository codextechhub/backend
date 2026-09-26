"""GL master data: currencies, FX rates, tax codes, cost centers, dimensions.
"""
from __future__ import annotations


from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_rbac.permissions import (
    HasAnyModuleAccess,
    HasRBACPermission,
    IsAuthenticatedAndActive,
)

from ..views import resolve_entity
from ..models import (
    CostCenter,
    Currency,
    Dimension,
    FxRate,
    TaxCode,
)
from ..serializers import (
    CostCenterSerializer,
    CurrencySerializer,
    DimensionSerializer,
    FxRateSerializer,
    TaxCodeSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _dec,
    _int,
    _resolve_account,
    _resolve_cost_center,
    _resolve_currency,
    _str_list,
)

# --------------------------------------------------------------------------- #
# Setup / reference data                                                      #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Currency List Create View.
class CurrencyListCreateView(_FinanceBase):
    """GET (list) / POST (create) currencies - **global** reference data (no entity).

    Reading is open to anyone holding a finance key: a currency is a code, a
    name and a symbol, and the currency picker on invoices and bills reads this
    list. Creating one still needs ``finance.currency.create``.

    docstring-name: Currencies
    """

    rbac_modules = ["finance"]
    rbac_permission = "finance.currency.create"

    def get_permissions(self):
        if self.request.method == "POST":
            return super().get_permissions()
        return [(IsAuthenticatedAndActive & HasAnyModuleAccess)()]

    # Handle GET requests for this endpoint.
    def get(self, request):
        qs = Currency.objects.all().order_by("code")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Currencies retrieved.", data=CurrencySerializer(qs, many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        body = request.data or {}
        code = str(body.get("code", "")).upper().strip()
        if not code:
            raise ValidationError({"code": "A 3-letter ISO currency code is required."})
        currency, created = Currency.objects.update_or_create(
            code=code,
            defaults={
                "name": body.get("name", code),
                "symbol": body.get("symbol", ""),
                "minor_unit": _int(body.get("minor_unit", 2), "minor_unit", minimum=0),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Currency {code} {'created' if created else 'updated'}.",
            data=CurrencySerializer(currency).data, status=201 if created else 200,
        )


# Group endpoint behavior for Fx Rate List Create View.
class FxRateListCreateView(_FinanceBase):
    """GET (list) / POST (create) FX rates - **global** reference data (no entity).

    docstring-name: FX rates
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.fxrate.create" if self.request.method == "POST" \
            else "finance.fxrate.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        qs = FxRate.objects.select_related("base", "quote").all()
        if (base := request.query_params.get("base")):
            qs = qs.filter(base_id=base.upper())
        if (quote := request.query_params.get("quote")):
            qs = qs.filter(quote_id=quote.upper())
        return self.paginate(request, qs, FxRateSerializer)

    # Handle POST requests for this endpoint.
    def post(self, request):
        body = request.data or {}
        base = _resolve_currency(body.get("base"), "base")
        quote = _resolve_currency(body.get("quote"), "quote")
        if base is None or quote is None:
            raise ValidationError({"base": "Both base and quote currencies are required."})
        rate = _dec(body.get("rate"), "rate")
        if rate <= 0:
            raise ValidationError({"rate": "Rate must be positive."})
        fx, created = FxRate.objects.update_or_create(
            base=base, quote=quote,
            as_of=_date(body.get("as_of"), "as_of", required=True),
            source=body.get("source", ""),
            defaults={"rate": rate},
        )
        return success_response(
            f"FX rate {base.code}/{quote.code} recorded.",
            data=FxRateSerializer(fx).data, status=201 if created else 200,
        )


# Group endpoint behavior for Tax Code List Create View.
class TaxCodeListCreateView(_FinanceBase):
    """GET (list) / POST (create) tax codes for an entity.

    Reading is open to anyone holding a finance key: a tax code is a name, a
    rate and the accounts it posts to, and the tax picker on every invoice and
    bill line reads this list. Gating it on ``finance.taxcode.view`` left a
    bursar who may raise an invoice unable to choose its VAT. Creating one still
    needs ``finance.taxcode.create``.

    docstring-name: Tax codes
    """

    rbac_modules = ["finance"]
    rbac_permission = "finance.taxcode.create"

    def get_permissions(self):
        if self.request.method == "POST":
            return super().get_permissions()
        return [(IsAuthenticatedAndActive & HasAnyModuleAccess)()]

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxCode.objects.filter(entity=entity).select_related(
            "collected_account", "paid_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Tax codes retrieved.", data=TaxCodeSerializer(qs, many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "A tax code is required."})
        tax, created = TaxCode.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "rate_bps": _int(body.get("rate_bps", 0), "rate_bps", minimum=0),
                "is_recoverable": _bool(body.get("is_recoverable", True), default=True),
                "collected_account": _resolve_account(
                    entity, body.get("collected_account"), "collected_account"),
                "paid_account": _resolve_account(
                    entity, body.get("paid_account"), "paid_account"),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Tax code {code} {'created' if created else 'updated'}.",
            data=TaxCodeSerializer(tax).data, status=201 if created else 200,
        )


# Group endpoint behavior for Cost Center List Create View.
class CostCenterListCreateView(_FinanceBase):
    """GET (list) / POST (create) cost centres for an entity.

    Reading is open to anyone working in finance, and to the procurement staff
    whose forms pick a cost centre: requisition writers, and storekeepers
    issuing stock to a department. A cost centre is a name and a code
    for books the caller is already entitled to, with no amounts, and every
    finance report that can be narrowed by one needs this list for its filter.
    Gating the read on ``finance.costcenter.view`` refused a bursar the filter
    on reports they were allowed to run. Creating one still needs
    ``finance.costcenter.create``.

    docstring-name: Cost centers
    """

    rbac_modules = ["finance"]

    @property
    def rbac_permission(self):
        """The create key for a write; the requisition grants for a read."""
        if self.request.method == "POST":
            return "finance.costcenter.create"
        return [
            "procurement.requisition.create",
            "procurement.requisition.update",
            "procurement.stock.issue",
        ]

    def get_permissions(self):
        """A read passes on any finance key or on a procurement grant that picks one."""
        if self.request.method == "POST":
            return super().get_permissions()
        return [(IsAuthenticatedAndActive & (HasAnyModuleAccess | HasRBACPermission))()]

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = CostCenter.objects.filter(entity=entity).select_related("parent")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Cost centres retrieved.", data=CostCenterSerializer(qs, many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        # TODO: code should be automated when a user didn't provide it
        if not code:
            raise ValidationError({"code": "A cost centre code is required."})
        parent = None
        if body.get("parent"):
            parent = _resolve_cost_center(entity, body.get("parent"), "parent")
        cc, created = CostCenter.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "parent": parent,
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Cost centre {code} {'created' if created else 'updated'}.",
            data=CostCenterSerializer(cc).data, status=201 if created else 200,
        )


# Group endpoint behavior for Dimension List Create View.
class DimensionListCreateView(_FinanceBase):
    """GET (list) / POST (create) analytical dimensions for an entity.

    Reading is open to anyone working in finance: a dimension is a name and
    its allowed values, with no amounts, and the journal screen and the
    dimension analysis report both need the list for their filters. Creating
    one still needs ``finance.dimension.create``.

    docstring-name: Dimensions
    """

    rbac_modules = ["finance"]

    @property
    def rbac_permission(self):
        """The create key; reads are gated by module membership instead."""
        return "finance.dimension.create"

    def get_permissions(self):
        """A read passes on any finance key; a write needs the create key."""
        if self.request.method == "POST":
            return super().get_permissions()
        return [(IsAuthenticatedAndActive & HasAnyModuleAccess)()]

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = Dimension.objects.filter(entity=entity)
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Dimensions retrieved.", data=DimensionSerializer(qs, many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        # TODO: code should be automated when a user didn't provide it
        if not code:
            raise ValidationError({"code": "A dimension code is required."})
        dim, created = Dimension.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "allowed_values": _str_list(body.get("allowed_values"), "allowed_values"),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Dimension {code} {'created' if created else 'updated'}.",
            data=DimensionSerializer(dim).data, status=201 if created else 200,
        )

