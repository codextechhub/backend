"""Entity-scoped Finance settings endpoints."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.response import success_response
from rest_framework import serializers
from vs_rbac.field_enforcement import UNPARSED, FieldAccessMixin, assert_writable, visible
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import WholeTenantWriteMixin

from .account_mappings import (
    account_mapping_options,
    account_mapping_snapshot,
    update_account_mappings,
)
from .banking_settings import (
    normalised_submission as banking_submission,
    stored_banking_values,
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
from .settings_history import (
    BANK_SUMMARY_FIELDS,
    finance_setting_fields,
    settings_history,
    settings_history_for_actions,
)
from .settings_ownership import (
    ACCOUNT_MAPPING_CONSUMERS,
    BANKING_SETTING_CONSUMERS,
    CALENDAR_SETTING_CONSUMERS,
    DOCUMENT_SETTING_CONSUMERS,
)
from .field_guards import readable
from .views import resolve_entity


def _hide_bank_copies(request, settings: dict) -> dict:
    """The document settings with each bank summary reduced to what the caller may read.

    The collection account and the account options are summaries of bank
    accounts, so the bank and currency they repeat follow the switches
    of ``finance.bankaccount``. The account's id and name identify it.
    """
    hidden = {key for key, field in BANK_SUMMARY_FIELDS
              if not readable(request, "finance.bankaccount", field)}
    if not hidden:
        return settings

    def strip(summary):
        return {k: v for k, v in summary.items() if k not in hidden} if summary else summary

    out = dict(settings)
    for key in ("primary_collection_bank_account",):
        out[key] = strip(out.get(key))
    for key in ("primary_collection_bank_accounts", "bank_account_options"):
        out[key] = [strip(row) for row in out.get(key) or []]
    return out


def _assert_collection_account_writable(request, entity, reference) -> None:
    """Refuse choosing a different fee collection account to a caller who may not write that flag.

    Choosing the account that already collects, or clearing a choice there is
    not, changes nothing and passes. Any other choice flips
    ``is_primary_collection`` on a bank account, so it is judged under that
    field's Write switch like every other path that sets it.
    """
    from .models import BankAccount

    if reference in (None, ""):
        unchanged = not BankAccount.objects.filter(entity=entity, is_primary_collection=True).exists()
    else:
        try:
            chosen = BankAccount.objects.filter(entity=entity, pk=int(reference)).first()
        except (TypeError, ValueError):
            chosen = None
        unchanged = chosen is not None and chosen.is_primary_collection
    if not unchanged:
        assert_writable(
            request, "finance.bankaccount", {"is_primary_collection": UNPARSED})


def _settings_history(entity, action, *, request=None):
    """The last ten changes to one of the entity's finance settings, labelled.

    See :func:`vs_finance.settings_history.settings_history`. A ``request``
    narrows each change to the settings its caller may read.
    """
    return settings_history(entity, action, finance_setting_fields(action), request=request)


class WholeTenantSettingsMixin(WholeTenantWriteMixin):
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

    The gate itself is :class:`vs_rbac.scoping.WholeTenantWriteMixin`, which
    every other shared-record screen uses too. Reads are untouched: a
    branch-bound holder of the view key still sees the settings that govern
    their branch.

    ``settings_subject`` names what is being changed in the refusal message.
    """

    settings_subject = "these settings"

    @property
    def shared_subject(self):
        return self.settings_subject


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
                "settings": _hide_bank_copies(request, serialize_finance_document_settings(
                    resolve_finance_document_settings(entity),
                )),
                "consumers": DOCUMENT_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
                    request=request,
                ),
            },
        )

    def patch(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if "primary_collection_bank_account" in body:
            _assert_collection_account_writable(
                request, entity, body["primary_collection_bank_account"])
        settings = update_finance_document_settings(
            entity=entity, data=body, actor_user=request.user,
        )
        return success_response(
            "Finance document settings saved.",
            data={
                "settings": _hide_bank_copies(
                    request, serialize_finance_document_settings(settings)),
                "consumers": DOCUMENT_SETTING_CONSUMERS,
                "history": _settings_history(
                    entity, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
                    request=request,
                ),
            },
        )


class BankingSettingsSerializer(FieldAccessMixin, serializers.Serializer):
    """The banking and cash defaults as the settings screen shows them, under ``finance.settings``.

    The names are those of :func:`vs_finance.banking_settings.serialize_finance_banking_settings`.
    Each default is a stored, submitted setting with its own Read and Write
    switch, and the label and options beside the allocation order go with it.
    """

    field_resource = "finance.settings"
    field_access_detail = True

    default_bank_reconciliation_tolerance_days = serializers.ReadOnlyField()
    default_group_reconciliation_matches = serializers.ReadOnlyField()
    default_receipt_allocation_strategy = serializers.ReadOnlyField()
    default_receipt_allocation_strategy_label = serializers.ReadOnlyField()
    default_receipt_allocation_strategy_options = serializers.ReadOnlyField()
    apply_transfers_automatically = serializers.ReadOnlyField()
    petty_cash_low_balance_threshold_bps = serializers.ReadOnlyField()
    updated_at = serializers.ReadOnlyField()
    updated_by = serializers.ReadOnlyField()


class FinanceBankingSettingsView(_FinanceSettingsView):
    """Read or update reconciliation, receipt-allocation and transfer defaults.

    Every default is a ``finance.settings`` field. A default the caller may not
    read is absent from ``settings``, from ``consumers`` and from the history;
    a PATCH that changes one the caller may not write is refused with the
    standard field refusal before anything is saved, and sending one back
    exactly as stored is not a change.

    docstring-name: Banking settings
    """

    settings_subject = "the finance banking settings"

    def _payload(self, request, entity, settings):
        return {
            "settings": BankingSettingsSerializer(
                serialize_finance_banking_settings(settings), context={"request": request},
            ).data,
            "consumers": visible(request, "finance.settings", BANKING_SETTING_CONSUMERS),
            "history": _settings_history(
                entity, FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED, request=request,
            ),
        }

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance banking settings retrieved.",
            data=self._payload(request, entity, resolve_finance_banking_settings(entity)),
        )

    def patch(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if isinstance(body, dict):
            assert_writable(
                request, "finance.settings", banking_submission(body),
                current=stored_banking_values(resolve_finance_banking_settings(entity)),
            )
        settings = update_finance_banking_settings(
            entity=entity, data=body, actor_user=request.user,
        )
        return success_response(
            "Finance banking settings saved.",
            data=self._payload(request, entity, settings),
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


class FinancePayrollSettingsView(_FinanceSettingsView):
    """Read or update the payroll policy: statutory switches and rates, payslip delivery."""

    settings_subject = "the payroll settings"

    def _payload(self, entity, settings):
        from .payroll_settings import serialize_finance_payroll_settings
        from .settings_ownership import PAYROLL_SETTING_CONSUMERS

        return {
            "settings": serialize_finance_payroll_settings(settings),
            "consumers": PAYROLL_SETTING_CONSUMERS,
            "history": _settings_history(entity, FinanceAuditAction.PAYROLL_SETTINGS_UPDATED),
        }

    def get(self, request):
        from .payroll_settings import resolve_finance_payroll_settings

        entity = resolve_entity(request)
        return success_response(
            "Finance payroll settings retrieved.",
            data=self._payload(entity, resolve_finance_payroll_settings(entity)),
        )

    def patch(self, request):
        from .payroll_settings import update_finance_payroll_settings

        entity = resolve_entity(request)
        settings = update_finance_payroll_settings(
            entity=entity, data=request.data or {}, actor_user=request.user,
        )
        return success_response(
            "Finance payroll settings saved.", data=self._payload(entity, settings),
        )


class FinanceSettingsHistoryView(_FinanceSettingsView):
    """The newest changes from every Finance settings section."""

    def get(self, request):
        entity = resolve_entity(request)
        actions = (
            FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
            FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
            FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
            FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
            FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED,
            FinanceAuditAction.PAYROLL_SETTINGS_UPDATED,
            FinanceAuditAction.RETENTION_SETTINGS_UPDATED,
        )
        fields_by_action = {
            action: finance_setting_fields(action) for action in actions
        }
        return success_response(
            "Finance settings changes retrieved.",
            data={
                "history": settings_history_for_actions(
                    entity, fields_by_action, request=request,
                ),
            },
        )
