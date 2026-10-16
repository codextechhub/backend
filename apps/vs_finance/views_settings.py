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


def _mapping_serializer():
    """A ``finance.settings`` serializer with one field per account role key."""
    from .account_mappings import ACCOUNT_MAPPING_SPECS

    return type("AccountMappingsSerializer", (FieldAccessMixin, serializers.Serializer), {
        "__module__": __name__,
        "__doc__": (
            "The account mappings keyed by role (``CASH_BANK``), under ``finance.settings``.\n\n"
            "Each role is its own field; the view lists the rows the caller may read."
        ),
        "field_resource": "finance.settings",
        "field_access_detail": True,
        **{str(key): serializers.ReadOnlyField() for key in ACCOUNT_MAPPING_SPECS},
    })


AccountMappingsSerializer = _mapping_serializer()


def _readable_mappings(request, rows) -> tuple[list, list]:
    """The mapping rows the caller may read, and the keys of those they may not change."""
    by_key = AccountMappingsSerializer(
        {row["key"]: row for row in rows}, context={"request": request},
    ).data
    read_only = list(by_key.get("_read_only_fields") or [])
    shown = [row for row in rows if row["key"] in by_key]
    return shown, [key for key in read_only if key in by_key]


class FinanceAccountSettingsView(_FinanceSettingsView):
    """Read or update typed account-role mappings for one ledger entity.

    Every role is a ``finance.settings`` field travelling under its key
    (``CASH_BANK``). A role the caller may not read is absent from
    ``mappings``, ``consumers`` and the history; ``read_only_keys`` names the
    roles they may read and not change, and a PATCH changing one of those, or
    one they may not read, is refused before anything is saved.

    docstring-name: Account mappings
    """

    settings_subject = "the finance account mappings"

    def _payload(self, request, entity, rows):
        shown, read_only = _readable_mappings(request, rows)
        return {
            "mappings": shown,
            "read_only_keys": read_only,
            "consumers": visible(request, "finance.settings", ACCOUNT_MAPPING_CONSUMERS),
            "account_options": account_mapping_options(entity),
            "history": _settings_history(
                entity, FinanceAuditAction.FINANCE_SETTINGS_UPDATED, request=request,
            ),
        }

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance account settings retrieved.",
            data=self._payload(request, entity, account_mapping_snapshot(entity)),
        )

    def patch(self, request):
        from .account_mappings import mapping_submission

        entity = resolve_entity(request)
        values = (request.data or {}).get("mappings")
        if isinstance(values, dict):
            submitted, current = mapping_submission(request, entity, values)
            assert_writable(request, "finance.settings", submitted, current=current)
        rows = update_account_mappings(
            request=request,
            entity=entity,
            values=values,
            actor_user=request.user,
        )
        return success_response(
            "Finance account settings saved.",
            data=self._payload(request, entity, rows),
        )


class DocumentSettingsSerializer(FieldAccessMixin, serializers.Serializer):
    """The document defaults as the settings screen shows them, under ``finance.settings``.

    The collection account and its options follow the bank account switches
    instead (:func:`_hide_bank_copies`), and are not ``finance.settings`` fields.
    """

    field_resource = "finance.settings"
    field_access_detail = True

    default_invoice_due_days = serializers.ReadOnlyField()
    default_invoice_narration = serializers.ReadOnlyField()
    auto_post_manual_invoices = serializers.ReadOnlyField()
    allow_customer_opening_balances = serializers.ReadOnlyField()
    term_collection_target_pct = serializers.ReadOnlyField()
    auto_apply_customer_credit = serializers.ReadOnlyField()
    concession_second_person_threshold = serializers.ReadOnlyField()
    primary_collection_bank_account = serializers.ReadOnlyField()
    primary_collection_bank_accounts = serializers.ReadOnlyField()
    bank_account_options = serializers.ReadOnlyField()
    updated_at = serializers.ReadOnlyField()
    updated_by = serializers.ReadOnlyField()


class FinanceDocumentSettingsView(_FinanceSettingsView):
    """Read or update customer-document defaults for one ledger entity.

    Every default is a ``finance.settings`` field with its own Read and Write
    switch; the collection account follows the bank account switches.

    docstring-name: Document settings
    """

    settings_subject = "the finance document settings"

    def _payload(self, request, entity, settings):
        return {
            "settings": DocumentSettingsSerializer(
                _hide_bank_copies(request, serialize_finance_document_settings(settings)),
                context={"request": request},
            ).data,
            "consumers": visible(request, "finance.settings", DOCUMENT_SETTING_CONSUMERS),
            "history": _settings_history(
                entity, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
                request=request,
            ),
        }

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance document settings retrieved.",
            data=self._payload(request, entity, resolve_finance_document_settings(entity)),
        )

    def patch(self, request):
        from .document_settings import SETTING_FIELDS, _validated_values
        from .settings_access import normalised_submission, stored_values

        entity = resolve_entity(request)
        body = request.data or {}
        if isinstance(body, dict):
            fields = SETTING_FIELDS[:-1]
            assert_writable(
                request, "finance.settings",
                normalised_submission(
                    {k: v for k, v in body.items() if k != "primary_collection_bank_account"},
                    fields, _validated_values,
                ),
                current=stored_values(resolve_finance_document_settings(entity), fields),
            )
        if "primary_collection_bank_account" in body:
            _assert_collection_account_writable(
                request, entity, body["primary_collection_bank_account"])
        settings = update_finance_document_settings(
            entity=entity, data=body, actor_user=request.user,
        )
        return success_response(
            "Finance document settings saved.",
            data=self._payload(request, entity, settings),
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


class CalendarSettingsSerializer(FieldAccessMixin, serializers.Serializer):
    """The fiscal calendar rules as the settings screen shows them, under ``finance.settings``."""

    field_resource = "finance.settings"
    field_access_detail = True

    next_year_mode = serializers.ReadOnlyField()
    next_year_mode_label = serializers.ReadOnlyField()
    next_year_mode_options = serializers.ReadOnlyField()
    next_year_lead_days = serializers.ReadOnlyField()
    periods_close_in_order = serializers.ReadOnlyField()
    updated_at = serializers.ReadOnlyField()
    updated_by = serializers.ReadOnlyField()


class FinanceCalendarSettingsView(_FinanceSettingsView):
    """Read or update how the fiscal calendar is kept ahead of today.

    Every rule is a ``finance.settings`` field with its own Read and Write switch.

    docstring-name: Fiscal calendar settings
    """

    settings_subject = "the fiscal calendar settings"

    def _payload(self, request, entity, settings):
        return {
            "settings": CalendarSettingsSerializer(
                serialize_finance_calendar_settings(settings), context={"request": request},
            ).data,
            "consumers": visible(request, "finance.settings", CALENDAR_SETTING_CONSUMERS),
            "history": _settings_history(
                entity, FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED, request=request,
            ),
        }

    def get(self, request):
        entity = resolve_entity(request)
        return success_response(
            "Finance calendar settings retrieved.",
            data=self._payload(request, entity, resolve_finance_calendar_settings(entity)),
        )

    def patch(self, request):
        from .calendar_settings import SETTING_FIELDS, _validated_values
        from .settings_access import normalised_submission, stored_values

        entity = resolve_entity(request)
        body = request.data or {}
        if isinstance(body, dict):
            assert_writable(
                request, "finance.settings",
                normalised_submission(body, SETTING_FIELDS, _validated_values),
                current=stored_values(resolve_finance_calendar_settings(entity), SETTING_FIELDS),
            )
        settings = update_finance_calendar_settings(
            entity=entity, data=body, actor_user=request.user,
        )
        return success_response(
            "Finance calendar settings saved.",
            data=self._payload(request, entity, settings),
        )


class PayrollSettingsSerializer(FieldAccessMixin, serializers.Serializer):
    """The payroll policy as the settings screen shows them, under ``finance.settings``.

    Whether the payment provider route is open is the books' state, not a
    setting, and carries no switch.
    """

    field_resource = "finance.settings"
    field_access_detail = True

    paye_method = serializers.ReadOnlyField()
    paye_method_label = serializers.ReadOnlyField()
    paye_method_options = serializers.ReadOnlyField()
    tax_country = serializers.ReadOnlyField()
    payment_route = serializers.ReadOnlyField()
    payment_route_label = serializers.ReadOnlyField()
    payment_route_options = serializers.ReadOnlyField()
    employee_pension_enabled = serializers.ReadOnlyField()
    employer_pension_enabled = serializers.ReadOnlyField()
    nhf_enabled = serializers.ReadOnlyField()
    nsitf_enabled = serializers.ReadOnlyField()
    itf_enabled = serializers.ReadOnlyField()
    payslip_in_app = serializers.ReadOnlyField()
    payslip_email = serializers.ReadOnlyField()
    previous_pay_required = serializers.ReadOnlyField()
    employee_pension_rate_bps = serializers.ReadOnlyField()
    employer_pension_rate_bps = serializers.ReadOnlyField()
    nhf_rate_bps = serializers.ReadOnlyField()
    nsitf_rate_bps = serializers.ReadOnlyField()
    itf_rate_bps = serializers.ReadOnlyField()
    payroll_moved_here_on = serializers.ReadOnlyField()
    paystack_route_available = serializers.ReadOnlyField()
    paystack_route_unavailable_reason = serializers.ReadOnlyField()
    updated_at = serializers.ReadOnlyField()
    updated_by = serializers.ReadOnlyField()


class FinancePayrollSettingsView(_FinanceSettingsView):
    """Read or update the payroll policy: statutory switches and rates, payslip delivery.

    Every value is a ``finance.settings`` field with its own Read and Write switch.

    docstring-name: Payroll settings
    """

    settings_subject = "the payroll settings"

    def _payload(self, request, entity, settings):
        from .payroll_settings import serialize_finance_payroll_settings
        from .settings_ownership import PAYROLL_SETTING_CONSUMERS

        return {
            "settings": PayrollSettingsSerializer(
                serialize_finance_payroll_settings(settings), context={"request": request},
            ).data,
            "consumers": visible(request, "finance.settings", PAYROLL_SETTING_CONSUMERS),
            "history": _settings_history(
                entity, FinanceAuditAction.PAYROLL_SETTINGS_UPDATED, request=request,
            ),
        }

    def get(self, request):
        from .payroll_settings import resolve_finance_payroll_settings

        entity = resolve_entity(request)
        return success_response(
            "Finance payroll settings retrieved.",
            data=self._payload(request, entity, resolve_finance_payroll_settings(entity)),
        )

    def patch(self, request):
        from .payroll_settings import (
            SETTING_FIELDS, _validated_values, resolve_finance_payroll_settings,
            update_finance_payroll_settings,
        )
        from .settings_access import normalised_submission, stored_values

        entity = resolve_entity(request)
        body = request.data or {}
        if isinstance(body, dict):
            assert_writable(
                request, "finance.settings",
                normalised_submission(
                    body, SETTING_FIELDS, lambda data: _validated_values(data, entity),
                ),
                current=stored_values(resolve_finance_payroll_settings(entity), SETTING_FIELDS),
            )
        settings = update_finance_payroll_settings(
            entity=entity, data=body, actor_user=request.user,
        )
        return success_response(
            "Finance payroll settings saved.", data=self._payload(request, entity, settings),
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
