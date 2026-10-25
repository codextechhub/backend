"""Entity-scoped Procurement settings API."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS

from core.response import success_response
from vs_finance.constants import FinanceAuditAction
from vs_finance.settings_history import settings_history, settings_history_for_actions
from vs_finance.views import resolve_entity
from vs_finance.views_settings import WholeTenantSettingsMixin
from vs_rbac.field_enforcement import assert_writable, can_read, visible

from ..settings import (
    HISTORY_FIELDS,
    resolve_procurement_settings,
    serialize_procurement_settings,
    update_procurement_settings,
)
from ..settings_ownership import PROCUREMENT_SETTING_CONSUMERS
from .base import _ProcBase


def _history(entity, request):
    """The last ten changes to the entity's procurement settings, labelled.

    See :func:`vs_finance.settings_history.settings_history`; the labels are
    :data:`vs_procurement.settings.HISTORY_FIELDS`.
    """
    if not can_read(request, "procurement.settings.history"):
        return None
    return settings_history(
        entity, FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED, HISTORY_FIELDS,
        request=request,
    )


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
        history = _history(entity, request)
        return success_response(
            "Procurement settings retrieved.",
            data={
                "settings": visible(request, "procurement.settings", serialize_procurement_settings(
                    resolve_procurement_settings(entity),
                )),
                "consumers": PROCUREMENT_SETTING_CONSUMERS,
                **({"history": history} if history is not None else {}),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        assert_writable(request, "procurement.settings", request.data or {})
        settings = update_procurement_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        history = _history(entity, request)
        return success_response(
            "Procurement settings saved.",
            data={
                "settings": visible(request, "procurement.settings", serialize_procurement_settings(settings)),
                "consumers": PROCUREMENT_SETTING_CONSUMERS,
                **({"history": history} if history is not None else {}),
            },
        )


class ProcurementSettingsHistoryView(WholeTenantSettingsMixin, _ProcBase):
    """The newest changes from every Procurement settings section."""

    rbac_permission = "procurement.settings.view"

    def get(self, request):
        entity = resolve_entity(request)
        if not can_read(request, "procurement.settings.history"):
            return success_response("Procurement settings changes retrieved.", data={})
        return success_response(
            "Procurement settings changes retrieved.",
            data={
                "history": settings_history_for_actions(
                    entity,
                    {FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED: HISTORY_FIELDS},
                    request=request,
                ),
            },
        )
