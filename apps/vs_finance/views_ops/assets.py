"""Fixed assets and depreciation.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..views import resolve_entity
from ..models import (
    FixedAsset,
)
from ..serializers import (
    FixedAssetSerializer,
)


from .base import (
    _FinanceBase,
    _date,
    _int,
    _money,
    _resolve_account,
    _resolve_bank_account,
)

# --------------------------------------------------------------------------- #
# Fixed assets                                                                #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Fixed Asset List Create View.
class FixedAssetListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) fixed assets for an entity.

    docstring-name: Fixed assets
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.fixedasset.create" if self.request.method == "POST" \
            else "finance.fixedasset.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = FixedAsset.objects.filter(entity=entity).prefetch_related("schedule")
        if (status_val := request.query_params.get("asset_status")):
            qs = qs.filter(asset_status=status_val)
        if (category := request.query_params.get("category")):
            qs = qs.filter(category=category)
        return self.paginate(
            request, qs.order_by("-acquisition_date", "-id"), FixedAssetSerializer)

    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..constants import AssetCategory, DepreciationMethod

        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "An asset name is required."})
        category = body.get("category") or AssetCategory.OTHER
        if category not in AssetCategory.values:
            raise ValidationError({"category": "Choose a valid asset category."})
        method = body.get("method") or DepreciationMethod.STRAIGHT_LINE
        if method not in DepreciationMethod.values:
            raise ValidationError({"method": "Choose a valid depreciation method."})
        asset = FixedAsset.objects.create(
            entity=entity, name=name,
            asset_code=body.get("asset_code", ""),
            category=category,
            acquisition_date=_date(body.get("acquisition_date"), "acquisition_date", required=True),
            cost=_money(body.get("cost", 0), "cost"),
            salvage_value=_money(body.get("salvage_value", 0), "salvage_value"),
            useful_life_months=_int(
                body.get("useful_life_months"), "useful_life_months", required=True, minimum=1),
            method=method,
            asset_account=_resolve_account(entity, body.get("asset_account"), "asset_account"),
            accumulated_depreciation_account=_resolve_account(
                entity, body.get("accumulated_depreciation_account"),
                "accumulated_depreciation_account"),
            depreciation_expense_account=_resolve_account(
                entity, body.get("depreciation_expense_account"),
                "depreciation_expense_account"),
            created_by=request.user,
        )
        return success_response(
            f"Fixed asset {asset.document_number} created.",
            data=FixedAssetSerializer(asset).data, status=201,
        )


# Group endpoint behavior for Fixed Asset Summary View.
class FixedAssetSummaryView(_FinanceBase):
    """GET — register KPIs over **all** assets (accurate under pagination).

    docstring-name: Fixed assets
    """

    rbac_permission = "finance.fixedasset.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Q, Sum
        from django.db.models.functions import Coalesce

        from ..constants import AssetStatus

        entity = resolve_entity(request)
        assets = FixedAsset.objects.filter(entity=entity)
        live = assets.exclude(asset_status=AssetStatus.DISPOSED).aggregate(
            cost=Coalesce(Sum("cost"), 0),
            accum=Coalesce(Sum("accumulated_depreciation"), 0),
        )
        # Straight-line monthly charge is a per-asset floor division, so sum it in
        # Python over just the (bounded) active set rather than approximating in SQL.
        monthly = 0
        for a in assets.filter(
            asset_status=AssetStatus.ACTIVE, useful_life_months__gt=0,
        ).values("cost", "salvage_value", "useful_life_months"):
            base = max(a["cost"] - a["salvage_value"], 0)
            monthly += base // a["useful_life_months"]
        return success_response(
            "Fixed asset summary retrieved.",
            data={
                "cost": live["cost"],
                "accum": live["accum"],
                "nbv": live["cost"] - live["accum"],
                "monthly": monthly,
            },
        )


# Define Fixed Asset Action Base values.
class _FixedAssetActionBase(_FinanceBase):
    # Support the asset workflow.
    def _asset(self, request, pk):
        entity = resolve_entity(request)
        asset = FixedAsset.objects.filter(entity=entity, pk=pk).first()
        if asset is None:
            raise NotFound("Fixed asset not found for this entity.")
        return entity, asset


# Group endpoint behavior for Fixed Asset Detail View.
class FixedAssetDetailView(_FixedAssetActionBase):
    """docstring-name: Fixed assets"""
    rbac_permission = "finance.fixedasset.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, asset = self._asset(request, pk)
        return success_response(
            "Fixed asset retrieved.", data=FixedAssetSerializer(asset).data,
        )


# Group endpoint behavior for Fixed Asset Acquire View.
class FixedAssetAcquireView(_FixedAssetActionBase):
    """POST {bank_account?, credit_account?} — capitalise + build the schedule.

    docstring-name: Acquire a fixed asset
    """

    rbac_permission = "finance.fixedasset.acquire"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..assets import acquire_asset

        entity, asset = self._asset(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)
        credit = _resolve_account(entity, body.get("credit_account"), "credit_account")
        acquire_asset(
            asset, bank_account=bank, credit_account=credit, actor_user=request.user,
        )
        asset.refresh_from_db()
        return success_response(
            f"Fixed asset {asset.document_number} capitalised.",
            data=FixedAssetSerializer(asset).data,
        )


# Group endpoint behavior for Fixed Asset Depreciate View.
class FixedAssetDepreciateView(_FixedAssetActionBase):
    """POST {up_to_date} — post every due depreciation charge up to a date.

    docstring-name: Run depreciation
    """

    rbac_permission = "finance.fixedasset.depreciate"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..assets import post_depreciation

        _, asset = self._asset(request, pk)
        body = request.data or {}
        posted = post_depreciation(
            asset,
            up_to_date=_date(body.get("up_to_date"), "up_to_date", required=True),
            actor_user=request.user,
        )
        asset.refresh_from_db()
        return success_response(
            f"Posted {len(posted)} depreciation charge(s) for {asset.name}.",
            data=FixedAssetSerializer(asset).data,
        )


# Group endpoint behavior for Fixed Asset Run Depreciation View.
class FixedAssetRunDepreciationView(_FinanceBase):
    """GET ?up_to_date — preview the period's depreciation posting; POST to run it.

    The run posts ONE compound journal covering every due charge across active assets.

    docstring-name: Run period depreciation
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.fixedasset.depreciate" if self.request.method == "POST" \
            else "finance.fixedasset.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..assets import preview_period_depreciation

        entity = resolve_entity(request)
        up_to = _date(request.query_params.get("up_to_date"), "up_to_date", required=True)
        return success_response(
            "Depreciation preview retrieved.",
            data=preview_period_depreciation(entity, up_to_date=up_to),
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..assets import run_period_depreciation

        entity = resolve_entity(request)
        body = request.data or {}
        result = run_period_depreciation(
            entity,
            up_to_date=_date(body.get("up_to_date"), "up_to_date", required=True),
            actor_user=request.user,
        )
        return success_response(
            f"Posted depreciation across {result['asset_count']} asset(s).", data=result,
        )


# Group endpoint behavior for Fixed Asset Dispose View.
class FixedAssetDisposeView(_FixedAssetActionBase):
    """POST {disposal_date, proceeds?, bank_account?, gain_loss_account?} — retire/sell.

    docstring-name: Dispose a fixed asset
    """

    rbac_permission = "finance.fixedasset.dispose"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..assets import dispose_asset

        entity, asset = self._asset(request, pk)
        body = request.data or {}
        dispose_asset(
            asset,
            disposal_date=_date(body.get("disposal_date"), "disposal_date", required=True),
            proceeds=_money(body.get("proceeds", 0), "proceeds"),
            bank_account=_resolve_bank_account(entity, body.get("bank_account"), required=False),
            gain_loss_account=_resolve_account(entity, body.get("gain_loss_account"), "gain_loss_account"),
            actor_user=request.user,
        )
        asset.refresh_from_db()
        return success_response(
            f"Fixed asset {asset.document_number} disposed.",
            data=FixedAssetSerializer(asset).data,
        )


