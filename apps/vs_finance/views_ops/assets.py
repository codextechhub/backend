"""Fixed assets and depreciation.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    FixedAsset,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    FixedAssetSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _date,  # Finance processing step.
    _int,  # Finance processing step.
    _money,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Fixed assets                                                                #
# --------------------------------------------------------------------------- #

class FixedAssetListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) fixed assets for an entity.

    docstring-name: Fixed assets
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.fixedasset.create" if self.request.method == "POST" \
            else "finance.fixedasset.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = FixedAsset.objects.filter(entity=entity).prefetch_related("schedule")  # Query finance data from the database.
        if (status_val := request.query_params.get("asset_status")):  # Branch when this finance condition is true.
            qs = qs.filter(asset_status=status_val)  # Store intermediate finance value.
        if (category := request.query_params.get("category")):  # Branch when this finance condition is true.
            qs = qs.filter(category=category)  # Store intermediate finance value.
        return self.paginate(  # Return the computed finance response.
            request, qs.order_by("-acquisition_date", "-id"), FixedAssetSerializer)  # Finance processing step.

    def post(self, request):  # Function handles this finance operation.
        from ..constants import AssetCategory, DepreciationMethod  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "An asset name is required."})  # Surface validation or finance error.
        category = body.get("category") or AssetCategory.OTHER  # Store intermediate finance value.
        if category not in AssetCategory.values:  # Branch when this finance condition is true.
            raise ValidationError({"category": "Choose a valid asset category."})  # Surface validation or finance error.
        method = body.get("method") or DepreciationMethod.STRAIGHT_LINE  # Store intermediate finance value.
        if method not in DepreciationMethod.values:  # Branch when this finance condition is true.
            raise ValidationError({"method": "Choose a valid depreciation method."})  # Surface validation or finance error.
        asset = FixedAsset.objects.create(  # Query finance data from the database.
            entity=entity, name=name,  # Store intermediate finance value.
            asset_code=body.get("asset_code", ""),  # Store intermediate finance value.
            category=category,  # Store intermediate finance value.
            acquisition_date=_date(body.get("acquisition_date"), "acquisition_date", required=True),  # Store intermediate finance value.
            cost=_money(body.get("cost", 0), "cost"),  # Store intermediate finance value.
            salvage_value=_money(body.get("salvage_value", 0), "salvage_value"),  # Store intermediate finance value.
            useful_life_months=_int(  # Store intermediate finance value.
                body.get("useful_life_months"), "useful_life_months", required=True, minimum=1),  # Store intermediate finance value.
            method=method,  # Store intermediate finance value.
            asset_account=_resolve_account(entity, body.get("asset_account"), "asset_account"),  # Store intermediate finance value.
            accumulated_depreciation_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("accumulated_depreciation_account"),  # Finance processing step.
                "accumulated_depreciation_account"),  # Finance processing step.
            depreciation_expense_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("depreciation_expense_account"),  # Finance processing step.
                "depreciation_expense_account"),  # Finance processing step.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Fixed asset {asset.document_number} created.",  # Finance processing step.
            data=FixedAssetSerializer(asset).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FixedAssetSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — register KPIs over **all** assets (accurate under pagination).

    docstring-name: Fixed assets
    """

    rbac_permission = "finance.fixedasset.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

        from ..constants import AssetStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        assets = FixedAsset.objects.filter(entity=entity)  # Query finance data from the database.
        live = assets.exclude(asset_status=AssetStatus.DISPOSED).aggregate(  # Store intermediate finance value.
            cost=Coalesce(Sum("cost"), 0),  # Store intermediate finance value.
            accum=Coalesce(Sum("accumulated_depreciation"), 0),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        # Straight-line monthly charge is a per-asset floor division, so sum it in
        # Python over just the (bounded) active set rather than approximating in SQL.
        monthly = 0  # Store intermediate finance value.
        for a in assets.filter(  # Iterate through finance records.
            asset_status=AssetStatus.ACTIVE, useful_life_months__gt=0,  # Store intermediate finance value.
        ).values("cost", "salvage_value", "useful_life_months"):  # Continue structured finance payload.
            base = max(a["cost"] - a["salvage_value"], 0)  # Store intermediate finance value.
            monthly += base // a["useful_life_months"]  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Fixed asset summary retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "cost": live["cost"],  # Finance processing step.
                "accum": live["accum"],  # Finance processing step.
                "nbv": live["cost"] - live["accum"],  # Finance processing step.
                "monthly": monthly,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class _FixedAssetActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _asset(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        asset = FixedAsset.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if asset is None:  # Branch when this finance condition is true.
            raise NotFound("Fixed asset not found for this entity.")  # Surface validation or finance error.
        return entity, asset  # Return the computed finance response.


class FixedAssetDetailView(_FixedAssetActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Fixed assets"""
    rbac_permission = "finance.fixedasset.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, asset = self._asset(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Fixed asset retrieved.", data=FixedAssetSerializer(asset).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FixedAssetAcquireView(_FixedAssetActionBase):  # Class groups related finance API or service behavior.
    """POST {bank_account?, credit_account?} — capitalise + build the schedule.

    docstring-name: Acquire a fixed asset
    """

    rbac_permission = "finance.fixedasset.acquire"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..assets import acquire_asset  # Import dependency used by this finance module.

        entity, asset = self._asset(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)  # Store intermediate finance value.
        credit = _resolve_account(entity, body.get("credit_account"), "credit_account")  # Store intermediate finance value.
        acquire_asset(  # Finance processing step.
            asset, bank_account=bank, credit_account=credit, actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        asset.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Fixed asset {asset.document_number} capitalised.",  # Finance processing step.
            data=FixedAssetSerializer(asset).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FixedAssetDepreciateView(_FixedAssetActionBase):  # Class groups related finance API or service behavior.
    """POST {up_to_date} — post every due depreciation charge up to a date.

    docstring-name: Run depreciation
    """

    rbac_permission = "finance.fixedasset.depreciate"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..assets import post_depreciation  # Import dependency used by this finance module.

        _, asset = self._asset(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        posted = post_depreciation(  # Store intermediate finance value.
            asset,  # Finance processing step.
            up_to_date=_date(body.get("up_to_date"), "up_to_date", required=True),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        asset.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Posted {len(posted)} depreciation charge(s) for {asset.name}.",  # Finance processing step.
            data=FixedAssetSerializer(asset).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FixedAssetRunDepreciationView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET ?up_to_date — preview the period's depreciation posting; POST to run it.

    The run posts ONE compound journal covering every due charge across active assets.

    docstring-name: Run period depreciation
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.fixedasset.depreciate" if self.request.method == "POST" \
            else "finance.fixedasset.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        from ..assets import preview_period_depreciation  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        up_to = _date(request.query_params.get("up_to_date"), "up_to_date", required=True)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Depreciation preview retrieved.",  # Finance processing step.
            data=preview_period_depreciation(entity, up_to_date=up_to),  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        from ..assets import run_period_depreciation  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        result = run_period_depreciation(  # Store intermediate finance value.
            entity,  # Finance processing step.
            up_to_date=_date(body.get("up_to_date"), "up_to_date", required=True),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Posted depreciation across {result['asset_count']} asset(s).", data=result,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FixedAssetDisposeView(_FixedAssetActionBase):  # Class groups related finance API or service behavior.
    """POST {disposal_date, proceeds?, bank_account?, gain_loss_account?} — retire/sell.

    docstring-name: Dispose a fixed asset
    """

    rbac_permission = "finance.fixedasset.dispose"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..assets import dispose_asset  # Import dependency used by this finance module.

        entity, asset = self._asset(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        dispose_asset(  # Finance processing step.
            asset,  # Finance processing step.
            disposal_date=_date(body.get("disposal_date"), "disposal_date", required=True),  # Store intermediate finance value.
            proceeds=_money(body.get("proceeds", 0), "proceeds"),  # Store intermediate finance value.
            bank_account=_resolve_bank_account(entity, body.get("bank_account"), required=False),  # Store intermediate finance value.
            gain_loss_account=_resolve_account(entity, body.get("gain_loss_account"), "gain_loss_account"),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        asset.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Fixed asset {asset.document_number} disposed.",  # Finance processing step.
            data=FixedAssetSerializer(asset).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


