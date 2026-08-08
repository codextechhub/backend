"""Entity-scoped Procurement settings API."""
from __future__ import annotations

from core.response import success_response
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import FinanceAuditLog
from vs_finance.serializers import FinanceAuditLogSerializer
from vs_finance.views import resolve_entity

from ..settings import (
    resolve_procurement_settings,
    serialize_procurement_settings,
    update_procurement_settings,
)
from .base import _ProcBase


def _history(entity):
    rows = (
        FinanceAuditLog.objects.filter(
            entity=entity,
            action=FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED,
        ).select_related("actor").order_by("-created_at", "-id")[:10]
    )
    return FinanceAuditLogSerializer(rows, many=True).data


class ProcurementSettingsView(_ProcBase):
    """Read or update purchasing defaults and invoice-matching tolerances."""

    @property
    def rbac_permission(self):
        return (
            "procurement.settings.update"
            if self.request.method == "PATCH"
            else "procurement.settings.view"
        )

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Procurement settings retrieved.",
            data={
                "settings": serialize_procurement_settings(
                    resolve_procurement_settings(entity),
                ),
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
                "history": _history(entity),
            },
        )
