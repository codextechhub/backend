"""Entity-scoped Procurement settings API."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS

from core.response import success_response
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import FinanceAuditLog
from vs_finance.serializers import FinanceAuditLogSerializer
from vs_finance.views import resolve_entity
from vs_finance.views_settings import WholeTenantSettingsMixin

from ..settings import (
    resolve_procurement_settings,
    serialize_procurement_settings,
    update_procurement_settings,
)
from ..settings_ownership import PROCUREMENT_SETTING_CONSUMERS
from .base import _ProcBase


def _history(entity):
    rows = (
        FinanceAuditLog.objects.filter(
            entity=entity,
            action=FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED,
        ).select_related("actor").order_by("-created_at", "-id")[:10]
    )
    return FinanceAuditLogSerializer(rows, many=True).data


class ProcurementSettingsView(WholeTenantSettingsMixin, _ProcBase):
    """Read or update purchasing defaults and invoice-matching tolerances.

    The settings bind every branch buying against the entity's books, so a
    write needs whole-tenant reach as well as the update key; see
    :class:`~vs_finance.views_settings.WholeTenantSettingsMixin`.
    """

    settings_subject = "the procurement settings"

    @property
    def rbac_permission(self):
        return (
            "procurement.settings.view"
            if self.request.method in SAFE_METHODS
            else "procurement.settings.update"
        )

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Procurement settings retrieved.",
            data={
                "settings": serialize_procurement_settings(
                    resolve_procurement_settings(entity),
                ),
                "consumers": PROCUREMENT_SETTING_CONSUMERS,
                "history": _history(entity),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        settings = update_procurement_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        return success_response(
            "Procurement settings saved.",
            data={
                "settings": serialize_procurement_settings(settings),
                "consumers": PROCUREMENT_SETTING_CONSUMERS,
                "history": _history(entity),
            },
        )
