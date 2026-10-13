"""The finance dashboards as a caller's Field Access lets them read them.

:mod:`vs_finance.dashboard`, :mod:`vs_finance.dashboard_receivables` and
:mod:`vs_finance.dashboard_spend` work out a payload of cards, blocks, trends
and exception lists. Each of those is a registered field of
``finance.dashboard``, and the serializers here are the surfaces that carry
them: a card the caller may not read is absent from the response, not ``None``.
``None`` stays what it has always meant, a card the caller's permission keys
withhold.

Every card is declared as a read-only field, so the registry test can prove
each registered name reaches a client. The keys that are not figures (the
window, the books kind, the as-of date, the reader's first name) are passed
through untouched, and so is any key a later change adds to a payload; the
registered names among those are still filtered, so a card added to one tab
cannot reach a caller who has hidden it by being forgotten here.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin, can_read, visible

RESOURCE = "finance.dashboard"

#: Dashboard rows that repeat a field of another resource, by card: the key of
#: each row and the registered field it copies. A card is the dashboard's own
#: figure, but a bank's name is a field of the bank account, and a caller who
#: may not read it there must not recover it from a card.
_COPIED_FIELDS = {
    "bank_accounts": (("bank_name", "finance.bankaccount.bank_name"),),
    "reconciliation": (("bank_name", "finance.bankaccount.bank_name"),),
}


def _card():
    """A dashboard card: whatever the payload holds under its name, read-only."""
    return serializers.JSONField(read_only=True)


class _KeepOtherKeys:
    """Puts back the payload keys a surface does not declare.

    The payload is a plain mapping, so a key with no declared field would be
    dropped by the serializer. The keys it does not declare are added after the
    declared ones are filtered, and are themselves passed through
    :func:`~vs_rbac.field_enforcement.visible`, so an undeclared registered name
    is still filtered. Not a serializer itself: each surface carries
    ``FieldAccessMixin`` on its own, so the registry sees every one of them.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        extras = {key: value for key, value in instance.items() if key not in self.fields}
        data.update(visible(request, RESOURCE, extras))
        for card, copies in _COPIED_FIELDS.items():
            rows = data.get(card)
            if not isinstance(rows, list):
                continue
            hidden = [key for key, field_key in copies if not can_read(request, field_key)]
            if hidden:
                data[card] = [
                    {k: v for k, v in row.items() if k not in hidden} if isinstance(row, dict) else row
                    for row in rows
                ]
        return data


class FinanceKpisSerializer(_KeepOtherKeys, FieldAccessMixin, serializers.Serializer):
    """The four headline cards of the overview, each with its sparkline."""

    field_resource = RESOURCE

    cash_position = _card()
    receivables = _card()
    payables = _card()
    net_income_ytd = _card()


class FinanceDashboardSerializer(_KeepOtherKeys, FieldAccessMixin, serializers.Serializer):
    """The Finance overview payload."""

    field_resource = RESOURCE

    kpis = FinanceKpisSerializer(read_only=True)
    collections = _card()
    channels = _card()
    branches = _card()
    bank_accounts = _card()
    revenue_vs_budget = _card()
    budget = _card()
    top_payers = _card()
    receivables_summary = _card()
    ar_aging = _card()
    trend = _card()
    top_overdue = _card()
    vendor_due = _card()
    payables_due = _card()
    approvals = _card()
    attention = _card()
    upcoming = _card()
    close_progress = _card()
    fiscal_runway = _card()
    recent_journals = _card()


class ReceivablesDashboardSerializer(_KeepOtherKeys, FieldAccessMixin, serializers.Serializer):
    """The Receivables and collections tab."""

    field_resource = RESOURCE

    collections = _card()
    receivables_summary = _card()
    days_to_pay = _card()
    credit = _card()
    curve = _card()
    plans = _card()
    groups = _card()
    dunning = _card()
    concessions = _card()
    adjustments = _card()
    largest = _card()


class SpendDashboardSerializer(_KeepOtherKeys, FieldAccessMixin, serializers.Serializer):
    """The Cash, spend and compliance tab."""

    field_resource = RESOURCE

    runway = _card()
    cash_movement = _card()
    spend = _card()
    spending = _card()
    reconciliation = _card()
    unmatched = _card()
    budgets = _card()
    payroll = _card()
    claims = _card()
    petty_cash = _card()
    tax_owed = _card()
    tax_calendar = _card()
    assets = _card()
