"""GL master data: currencies, FX rates, tax codes, cost centers, dimensions.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from rest_framework.exceptions import ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    CostCenter,  # Finance processing step.
    Currency,  # Finance processing step.
    Dimension,  # Finance processing step.
    FxRate,  # Finance processing step.
    TaxCode,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    CostCenterSerializer,  # Finance processing step.
    CurrencySerializer,  # Finance processing step.
    DimensionSerializer,  # Finance processing step.
    FxRateSerializer,  # Finance processing step.
    TaxCodeSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _bool,  # Finance processing step.
    _date,  # Finance processing step.
    _dec,  # Finance processing step.
    _int,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
    _str_list,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Setup / reference data                                                      #
# --------------------------------------------------------------------------- #

class CurrencyListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) currencies — **global** reference data (no entity).

    docstring-name: Currencies
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.currency.create" if self.request.method == "POST" \
            else "finance.currency.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        qs = Currency.objects.all().order_by("code")  # Query finance data from the database.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Currencies retrieved.", data=CurrencySerializer(qs, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).upper().strip()  # Store intermediate finance value.
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A 3-letter ISO currency code is required."})  # Surface validation or finance error.
        currency, created = Currency.objects.update_or_create(  # Query finance data from the database.
            code=code,  # Store intermediate finance value.
            defaults={  # Store intermediate finance value.
                "name": body.get("name", code),  # Finance processing step.
                "symbol": body.get("symbol", ""),  # Finance processing step.
                "minor_unit": _int(body.get("minor_unit", 2), "minor_unit", minimum=0),  # Store intermediate finance value.
                "is_active": _bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Currency {code} {'created' if created else 'updated'}.",  # Finance processing step.
            data=CurrencySerializer(currency).data, status=201 if created else 200,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FxRateListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) FX rates — **global** reference data (no entity).

    docstring-name: FX rates
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.fxrate.create" if self.request.method == "POST" \
            else "finance.fxrate.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        qs = FxRate.objects.select_related("base", "quote").all()  # Query finance data from the database.
        if (base := request.query_params.get("base")):  # Branch when this finance condition is true.
            qs = qs.filter(base_id=base.upper())  # Store intermediate finance value.
        if (quote := request.query_params.get("quote")):  # Branch when this finance condition is true.
            qs = qs.filter(quote_id=quote.upper())  # Store intermediate finance value.
        return self.paginate(request, qs, FxRateSerializer)  # Return the computed finance response.

    def post(self, request):  # Function handles this finance operation.
        body = request.data or {}  # Store intermediate finance value.
        base = _resolve_currency(body.get("base"), "base")  # Store intermediate finance value.
        quote = _resolve_currency(body.get("quote"), "quote")  # Store intermediate finance value.
        if base is None or quote is None:  # Branch when this finance condition is true.
            raise ValidationError({"base": "Both base and quote currencies are required."})  # Surface validation or finance error.
        rate = _dec(body.get("rate"), "rate")  # Store intermediate finance value.
        if rate <= 0:  # Branch when this finance condition is true.
            raise ValidationError({"rate": "Rate must be positive."})  # Surface validation or finance error.
        fx, created = FxRate.objects.update_or_create(  # Query finance data from the database.
            base=base, quote=quote,  # Store intermediate finance value.
            as_of=_date(body.get("as_of"), "as_of", required=True),  # Store intermediate finance value.
            source=body.get("source", ""),  # Store intermediate finance value.
            defaults={"rate": rate},  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"FX rate {base.code}/{quote.code} recorded.",  # Finance processing step.
            data=FxRateSerializer(fx).data, status=201 if created else 200,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxCodeListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) tax codes for an entity.

    docstring-name: Tax codes
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.taxcode.create" if self.request.method == "POST" \
            else "finance.taxcode.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = TaxCode.objects.filter(entity=entity).select_related(  # Query finance data from the database.
            "collected_account", "paid_account")  # Finance processing step.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Tax codes retrieved.", data=TaxCodeSerializer(qs, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip()  # Store intermediate finance value.
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A tax code is required."})  # Surface validation or finance error.
        tax, created = TaxCode.objects.update_or_create(  # Query finance data from the database.
            entity=entity, code=code,  # Store intermediate finance value.
            defaults={  # Store intermediate finance value.
                "name": body.get("name", code),  # Finance processing step.
                "rate_bps": _int(body.get("rate_bps", 0), "rate_bps", minimum=0),  # Store intermediate finance value.
                "is_recoverable": _bool(body.get("is_recoverable", True), default=True),  # Store intermediate finance value.
                "collected_account": _resolve_account(  # Finance processing step.
                    entity, body.get("collected_account"), "collected_account"),  # Finance processing step.
                "paid_account": _resolve_account(  # Finance processing step.
                    entity, body.get("paid_account"), "paid_account"),  # Finance processing step.
                "is_active": _bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Tax code {code} {'created' if created else 'updated'}.",  # Finance processing step.
            data=TaxCodeSerializer(tax).data, status=201 if created else 200,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class CostCenterListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) cost centres for an entity.

    docstring-name: Cost centers
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.costcenter.create" if self.request.method == "POST" \
            else "finance.costcenter.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = CostCenter.objects.filter(entity=entity).select_related("parent")  # Query finance data from the database.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Cost centres retrieved.", data=CostCenterSerializer(qs, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip()  # Store intermediate finance value.
        # TODO: code should be automated when a user didn't provide it
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A cost centre code is required."})  # Surface validation or finance error.
        parent = None  # Store intermediate finance value.
        if body.get("parent"):  # Branch when this finance condition is true.
            parent = _resolve_cost_center(entity, body.get("parent"), "parent")  # Store intermediate finance value.
        cc, created = CostCenter.objects.update_or_create(  # Query finance data from the database.
            entity=entity, code=code,  # Store intermediate finance value.
            defaults={  # Store intermediate finance value.
                "name": body.get("name", code),  # Finance processing step.
                "parent": parent,  # Finance processing step.
                "is_active": _bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Cost centre {code} {'created' if created else 'updated'}.",  # Finance processing step.
            data=CostCenterSerializer(cc).data, status=201 if created else 200,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DimensionListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) analytical dimensions for an entity.

    docstring-name: Dimensions
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.dimension.create" if self.request.method == "POST" \
            else "finance.dimension.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Dimension.objects.filter(entity=entity)  # Query finance data from the database.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Dimensions retrieved.", data=DimensionSerializer(qs, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip()  # Store intermediate finance value.
        # TODO: code should be automated when a user didn't provide it
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A dimension code is required."})  # Surface validation or finance error.
        dim, created = Dimension.objects.update_or_create(  # Query finance data from the database.
            entity=entity, code=code,  # Store intermediate finance value.
            defaults={  # Store intermediate finance value.
                "name": body.get("name", code),  # Finance processing step.
                "allowed_values": _str_list(body.get("allowed_values"), "allowed_values"),  # Finance processing step.
                "is_active": _bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Dimension {code} {'created' if created else 'updated'}.",  # Finance processing step.
            data=DimensionSerializer(dim).data, status=201 if created else 200,  # Store intermediate finance value.
        )  # Continue structured finance payload.


