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
from vs_rbac.scoping import WholeTenantWriteMixin

from ..constants import TaxTreatment
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
#
# Currencies, FX rates, tax codes, cost centres and dimensions carry no branch,
# so a write to any of them binds every branch at once and needs whole-tenant reach
# (:class:`vs_rbac.scoping.WholeTenantWriteMixin`). Reads stay open as each
# view's docstring describes.


def _upsert_by_code(model, lookup, body, fields, *, check=None):
    """Create the ``model`` row at ``lookup``, or update the one already there, from ``body``.

    ``fields`` maps each model field to ``(body key, parse, default)``. A create
    fills every field, from the body where it names one and from the default
    where it does not. An update changes only the fields the body names and
    keeps every other one as it is: a bursar renaming the VAT code sends the new
    name, and its treatment, rate and accounts stay what they were rather than
    falling back to the create defaults. A field is cleared by naming it with an
    empty value.

    ``check(values, row)`` sees the parsed values and the row being updated
    (``None`` on a create) before anything is written, to refuse a combination
    of the new values with the kept ones. Returns ``(row, created)``.
    """
    from django.db import transaction

    with transaction.atomic():
        row = model.objects.select_for_update().filter(**lookup).first()
        values = {
            name: parse(body[key] if key in body else default)
            for name, (key, parse, default) in fields.items()
            if row is None or key in body
        }
        if check is not None:
            check(values, row)
        if row is None:
            return model.objects.create(**lookup, **values), True
        for name, value in values.items():
            setattr(row, name, value)
        row.save()
        return row, False

# Group endpoint behavior for Currency List Create View.
class CurrencyListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (create) currencies - **global** reference data (no entity).

    Reading is open to anyone holding a finance key: a currency is a code, a
    name and a symbol, and the currency picker on invoices and bills reads this
    list. Creating one still needs ``finance.currency.create``.

    docstring-name: Currencies
    """

    shared_subject = "the currencies"

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
        """Create a currency, or update the named fields of one (:func:`_upsert_by_code`)."""
        body = request.data or {}
        code = str(body.get("code", "")).upper().strip()
        if not code:
            raise ValidationError({"code": "A 3-letter ISO currency code is required."})
        currency, created = _upsert_by_code(Currency, {"code": code}, body, {
            "name": ("name", lambda value: value, code),
            "symbol": ("symbol", lambda value: value, ""),
            "minor_unit": ("minor_unit", lambda value: _int(value, "minor_unit", minimum=0), 2),
            "is_active": ("is_active", lambda value: _bool(value, default=True), True),
        })
        return success_response(
            f"Currency {code} {'created' if created else 'updated'}.",
            data=CurrencySerializer(currency).data, status=201 if created else 200,
        )


# Group endpoint behavior for Fx Rate List Create View.
class FxRateListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (create) FX rates - **global** reference data (no entity).

    docstring-name: FX rates
    """

    shared_subject = "the exchange rates"

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
class TaxCodeListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (create) tax codes for an entity.

    Reading is open to anyone holding a finance key: a tax code is a name, a
    rate and the accounts it posts to, and the tax picker on every invoice and
    bill line reads this list. Gating it on ``finance.taxcode.view`` left a
    bursar who may raise an invoice unable to choose its VAT. Creating one still
    needs ``finance.taxcode.create``.

    docstring-name: Tax codes
    """

    shared_subject = "the tax codes"

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
        """Create a tax code, or update the named fields of one (:func:`_upsert_by_code`).

        A create with no ``treatment`` is STANDARD. An update with no
        ``treatment`` (or a blank one) keeps the code's own: re-saving a zero-rated
        code's name must not turn it into a standard one. A rate above 0 needs a
        STANDARD treatment, whether the treatment is sent or kept, so moving a
        7.5% code to EXEMPT sends ``rate_bps`` 0 with it.
        """
        entity = resolve_entity(request)
        data = request.data or {}
        body = data.dict() if hasattr(data, "dict") else dict(data)
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "A tax code is required."})
        if body.get("treatment") in (None, ""):
            body.pop("treatment", None)

        def treatment_of(value):
            treatment = str(value).upper()
            if treatment not in TaxTreatment.values:
                raise ValidationError({"treatment": (
                    f"Treatment must be one of {', '.join(TaxTreatment.values)}.")})
            return treatment

        def check(values, row):
            treatment = values.get("treatment", getattr(row, "treatment", TaxTreatment.STANDARD))
            if values.get("rate_bps", getattr(row, "rate_bps", 0)) and treatment != TaxTreatment.STANDARD:
                raise ValidationError({"rate_bps": (
                    f"A {TaxTreatment(treatment).label.lower()} code charges no tax, so its rate is 0.")})

        def account(field):
            return lambda value: _resolve_account(request, entity, value, field)

        tax, created = _upsert_by_code(TaxCode, {"entity": entity, "code": code}, body, {
            "name": ("name", lambda value: value, code),
            "rate_bps": ("rate_bps", lambda value: _int(value, "rate_bps", minimum=0), 0),
            "treatment": ("treatment", treatment_of, TaxTreatment.STANDARD),
            "is_recoverable": ("is_recoverable", lambda value: _bool(value, default=True), True),
            "collected_account": ("collected_account", account("collected_account"), None),
            "paid_account": ("paid_account", account("paid_account"), None),
            "is_active": ("is_active", lambda value: _bool(value, default=True), True),
        }, check=check)
        return success_response(
            f"Tax code {code} {'created' if created else 'updated'}.",
            data=TaxCodeSerializer(tax).data, status=201 if created else 200,
        )


# Group endpoint behavior for Cost Center List Create View.
class CostCenterListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (create) cost centres for an entity.

    Reading is open to anyone working in finance, and to the procurement staff
    whose forms pick a cost centre: requisition writers, and storekeepers
    issuing stock to a department. A cost centre is a name and a code
    for books the caller is already entitled to, with no amounts, and every
    finance report that can be narrowed by one needs this list for its filter.
    Gating the read on ``finance.costcenter.view`` refused a bursar the filter
    on reports they were allowed to run. Creating one still needs
    ``finance.costcenter.create``.

    A cost centre carries no branch, so it is shared by every branch posting to
    the books, and a write needs whole-tenant reach as well as the key. The POST
    is create-or-update by code: Lekki's bursar posting ``ADMIN`` with a new name
    would rename the cost centre Ikeja's lines already carry, so a branch-bound
    caller is refused (403 ``SHARED_RECORD_READ_ONLY``) whether the code is new
    or not.

    docstring-name: Cost centers
    """

    shared_subject = "the cost centres"

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
        cc, created = _upsert_by_code(CostCenter, {"entity": entity, "code": code}, body, {
            "name": ("name", lambda value: value, code),
            "parent": ("parent", lambda value: (
                _resolve_cost_center(entity, value, "parent") if value else None), None),
            "is_active": ("is_active", lambda value: _bool(value, default=True), True),
        })
        return success_response(
            f"Cost centre {code} {'created' if created else 'updated'}.",
            data=CostCenterSerializer(cc).data, status=201 if created else 200,
        )


# Group endpoint behavior for Dimension List Create View.
class DimensionListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (create) analytical dimensions for an entity.

    Reading is open to anyone working in finance: a dimension is a name and
    its allowed values, with no amounts, and the journal screen and the
    dimension analysis report both need the list for their filters. Creating
    one still needs ``finance.dimension.create``.

    docstring-name: Dimensions
    """

    shared_subject = "the dimensions"

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
        dim, created = _upsert_by_code(Dimension, {"entity": entity, "code": code}, body, {
            "name": ("name", lambda value: value, code),
            "allowed_values": ("allowed_values", lambda value: _str_list(value, "allowed_values"), None),
            "is_active": ("is_active", lambda value: _bool(value, default=True), True),
        })
        return success_response(
            f"Dimension {code} {'created' if created else 'updated'}.",
            data=DimensionSerializer(dim).data, status=201 if created else 200,
        )

