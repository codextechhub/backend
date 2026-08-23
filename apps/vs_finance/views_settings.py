"""Entity-scoped Finance settings endpoints."""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

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
    DOCUMENT_SETTING_CONSUMERS,
)
from .views import resolve_entity


def _settings_history(entity, action):
    rows = (
        FinanceAuditLog.objects.filter(entity=entity, action=action)
        .select_related("actor").order_by("-created_at", "-id")[:10]
    )
    return FinanceAuditLogSerializer(rows, many=True).data


class FinanceAccountSettingsView(APIView):
    """Read or update typed account-role mappings for one ledger entity."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return (
            "finance.settings.update"
            if self.request.method == "PATCH"
            else "finance.settings.view"
        )

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


class FinanceDocumentSettingsView(APIView):
    """Read or update customer-document defaults for one ledger entity."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return (
            "finance.settings.update"
            if self.request.method == "PATCH"
            else "finance.settings.view"
        )

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


class FinanceBankingSettingsView(APIView):
    """Read or update reconciliation and receipt-allocation defaults."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return (
            "finance.settings.update"
            if self.request.method == "PATCH"
            else "finance.settings.view"
        )

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
