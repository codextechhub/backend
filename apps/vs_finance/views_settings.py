"""Entity-scoped Finance settings endpoints."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import assert_caller_may_configure

from .account_mappings import (
    account_mapping_options,
    account_mapping_snapshot,
    update_account_mappings,
)
from .banking_settings import (
    resolve_finance_banking_settings,
    serialize_finance_banking_settings,
    update_finance_banking_settings,
)
from .calendar_settings import (
    resolve_finance_calendar_settings,
    serialize_finance_calendar_settings,
    update_finance_calendar_settings,
)
from .constants import FinanceAuditAction
from .document_settings import (
    resolve_finance_document_settings,
    serialize_finance_document_settings,
    update_finance_document_settings,
)
from .models import FinanceAuditLog
from .serializers import FinanceAuditLogSerializer
from .settings_ownership import (
    ACCOUNT_MAPPING_CONSUMERS,
    BANKING_SETTING_CONSUMERS,
    CALENDAR_SETTING_CONSUMERS,
    DOCUMENT_SETTING_CONSUMERS,
)
from .views import resolve_entity


def _settings_history(entity, action):
    rows = (
        FinanceAuditLog.objects.filter(entity=entity, action=action)
        .select_related("actor").order_by("-created_at", "-id")[:10]
    )
    return FinanceAuditLogSerializer(rows, many=True).data


class WholeTenantSettingsMixin:
    """Refuse any write to a tenant-wide settings screen from a branch-bound caller.

    A ledger entity's settings carry no branch: an account mapping, a document
    default, a banking default or a calendar rule binds every branch that posts
    to those books. Holding the update key is therefore not enough to change
    one. Lagoon View runs Ikeja and Lekki; if the Lekki bursar, whose role
    carries ``finance.settings.update`` pinned to Lekki, could re-point the
    school fees income mapping, Ikeja's receipts would post to her choice of
    account. The caller's reach has to be the whole tenant, which is
    :func:`vs_rbac.scoping.assert_caller_may_configure` with no branch, and a
    refusal is a 403 ``SHARED_RECORD_READ_ONLY`` raised before the handler
    runs, so nothing is written.

    The check sits in :meth:`check_permissions`, after the permission classes,
    so a caller without the key still gets the ordinary permission refusal and
    every unsafe method of every view built on this is covered without the
    handler having to remember it. Reads are untouched: a branch-bound holder
    of the view key still sees the settings that govern their branch.

    ``settings_subject`` names what is being changed in the refusal message.
    """

    settings_subject = "these settings"

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method not in SAFE_METHODS:
            assert_caller_may_configure(
                request.user, request.tenant,
                message=(
                    "Only a school-wide administrator can change "
                    f"{self.settings_subject}."
                ),
            )


class _FinanceSettingsView(WholeTenantSettingsMixin, APIView):
    """A finance settings screen: ``finance.settings.view`` to read, ``.update`` to write."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return (
            "finance.settings.view"
            if self.request.method in SAFE_METHODS
            else "finance.settings.update"
        )


class FinanceAccountSettingsView(_FinanceSettingsView):
    """Read or update typed account-role mappings for one ledger entity."""

    settings_subject = "the finance account mappings"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance account settings retrieved.",
            data={
                "mappings": account_mapping_snapshot(entity),
                "consumers": ACCOUNT_MAPPING_CONSUMERS,
                "account_options": account_mapping_options(entity),
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
                ),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        mappings = update_account_mappings(
            request=request,
            entity=entity,
            values=(request.data or {}).get("mappings"),
            actor_user=request.user,
        )
        return success_response(
            "Finance account settings saved.",
            data={
                "mappings": mappings,
                "consumers": ACCOUNT_MAPPING_CONSUMERS,
                "account_options": account_mapping_options(entity),
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
                ),
            },
        )


class FinanceDocumentSettingsView(_FinanceSettingsView):
    """Read or update customer-document defaults for one ledger entity."""

    settings_subject = "the finance document settings"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance document settings retrieved.",
            data={
                "settings": serialize_finance_document_settings(
                    resolve_finance_document_settings(entity),
                ),
                "consumers": DOCUMENT_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
                ),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        settings = update_finance_document_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        return success_response(
            "Finance document settings saved.",
            data={
                "settings": serialize_finance_document_settings(settings),
                "consumers": DOCUMENT_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
                ),
            },
        )


class FinanceBankingSettingsView(_FinanceSettingsView):
    """Read or update reconciliation and receipt-allocation defaults."""

    settings_subject = "the finance banking settings"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance banking settings retrieved.",
            data={
                "settings": serialize_finance_banking_settings(
                    resolve_finance_banking_settings(entity),
                ),
                "consumers": BANKING_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
                ),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        settings = update_finance_banking_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        return success_response(
            "Finance banking settings saved.",
            data={
                "settings": serialize_finance_banking_settings(settings),
                "consumers": BANKING_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
                ),
            },
        )


class FinanceCalendarSettingsView(_FinanceSettingsView):
    """Read or update how the fiscal calendar is kept ahead of today."""

    settings_subject = "the fiscal calendar settings"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance calendar settings retrieved.",
            data={
                "settings": serialize_finance_calendar_settings(
                    resolve_finance_calendar_settings(entity),
                ),
                "consumers": CALENDAR_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
                ),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        settings = update_finance_calendar_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        return success_response(
            "Finance calendar settings saved.",
            data={
                "settings": serialize_finance_calendar_settings(settings),
                "consumers": CALENDAR_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
                ),
            },
        )
