"""The daily check that the platform's books agree with its provider balance.

The platform's provider balance holds two kinds of money: what it holds for
held-mode client branches, and its own online takings not yet settled to its
bank. Its books say what that should come to:

    the provider balance account (PROVIDER_BALANCE), which mirrors the held-funds
    sub-ledger, plus its own payments still in transit (each less its fee)

and the provider says what it actually holds (``available_balance``, Paystack's
``GET /balance``). :func:`reconcile_held_ledger` compares the two every day and
records the check (:class:`~vs_payments.models.HeldReconciliation`).

When they differ by more than the platform's tolerance (the configuration value
``payments.held_reconciliation_tolerance_kobo``, default 0), or the account
disagrees with the sub-ledger it mirrors, the check opens one system-health
incident (:func:`vs_health.faults.report_configuration_fault`, as the worker
watchdog does) and tells the platform's operators once, when it opens. The next
check that agrees resolves it, so the next disagreement opens a fresh one.

Bright Star Lekki's N178,000 and Greenfield's N40,000 are held, and the
platform's own N5,000 subscription payment is in transit: the books say
N223,000. Paystack reports N222,950 because a N50 transfer fee was charged and
never booked: the check opens "Held-ledger mismatch: N50" and the operators are
told. Once the fee is booked and Paystack and the books agree, it resolves.

A check that could not ask the provider is recorded with its error and changes
no incident: nothing was measured. The provider must keep the platform's
balance rather than sweep it to the platform's bank each day *(confirm the
Paystack setting)*, or the balance falls with every sweep and never agrees.
"""
from __future__ import annotations

import logging

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger("vs_payments.held_reconciliation")

#: The one open incident a disagreement is filed under.
FAULT_KEY = "payments.held-ledger-mismatch"

#: The platform setting holding the tolerance, in kobo.
TOLERANCE_KEY = "payments.held_reconciliation_tolerance_kobo"

#: Who an incident and its events are attributed to.
WHO = "Held-ledger reconciliation"


def tolerance() -> int:
    """Kobo the provider's balance may differ from the books by before it is an incident."""
    from vs_config.conf import get_config

    try:
        return max(0, int(get_config(TOLERANCE_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


def provider_account_balance(platform) -> int:
    """The platform's provider balance account, as its posted journals have it (debit balance)."""
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, DocumentStatus
    from vs_finance.models import JournalLine

    try:
        account = resolve_mapped_account(platform, AccountMappingKey.PROVIDER_BALANCE)
    except Exception:  # noqa: BLE001 - no account means nothing booked to it.
        return 0
    totals = JournalLine.objects.filter(
        account=account, entry__status__in=(DocumentStatus.POSTED, DocumentStatus.REVERSED),
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return int((totals["debit"] or 0) - (totals["credit"] or 0))


def own_in_transit(platform) -> int:
    """The platform's own confirmed online payments not yet settled to its bank, less their fees."""
    from .models import CollectionIntent
    from .settlement import awaiting_settlement_q

    rows = (CollectionIntent.objects.filter(entity=platform, held_by_platform=False)
            .filter(awaiting_settlement_q()).values_list("amount", "fee"))
    return sum(int(amount) - int(fee or 0) for amount, fee in rows)


def reconcile_held_ledger(*, provider=None, currency="NGN", today=None):
    """Check the platform's books against its provider balance today, and record it.

    Returns the day's :class:`~vs_payments.models.HeldReconciliation`; a second
    check the same day replaces the first's figures.
    """
    from vs_config.clock import tenant_today

    from .held import platform_books
    from .models import HeldBalance, HeldReconciliation
    from .providers.registry import get_provider
    from .services import resolve_provider_name

    platform = platform_books()
    provider_name = resolve_provider_name(provider)
    today = today or (tenant_today(platform.tenant) if platform is not None else timezone.localdate())
    reported, error = None, ""
    try:
        reported = int(get_provider(provider_name).available_balance(currency))
    except Exception as exc:  # noqa: BLE001 - a check that cannot ask is recorded, not raised.
        error = str(getattr(exc, "message", exc))[:255] or exc.__class__.__name__
    account = provider_account_balance(platform) if platform is not None else 0
    held_total = int(HeldBalance.objects.aggregate(total=Sum("balance"))["total"] or 0)
    transit = own_in_transit(platform) if platform is not None else 0
    books = account + transit
    limit = tolerance()
    difference = None if reported is None else reported - books
    agrees = difference is not None and abs(difference) <= limit and account == held_total
    row, _ = HeldReconciliation.objects.update_or_create(
        provider=provider_name, currency=currency.upper(), checked_on=today,
        defaults={
            "provider_balance": reported, "provider_account": account, "held_total": held_total,
            "own_in_transit": transit, "books_balance": books, "difference": difference,
            "tolerance": limit, "agrees": agrees, "error": error,
        },
    )
    if difference is None:
        return row
    if agrees:
        _resolve_open_incidents()
        row.incident_code = ""
    else:
        row.incident_code = _raise(row)
    row.save(update_fields=["incident_code", "updated_at"])
    return row


def _raise(row) -> str:
    """Open the mismatch incident (or find the open one) and tell operators when it opens."""
    from vs_finance.money import format_naira
    from vs_health.faults import report_configuration_fault
    from vs_health.models import Incident, Severity

    parts = [f"{row.provider} reports {format_naira(row.provider_balance)} for {row.currency}; "
             f"the platform's books say {format_naira(row.books_balance)} "
             f"(provider balance account {format_naira(row.provider_account)}, own payments in "
             f"transit {format_naira(row.own_in_transit)}), a difference of "
             f"{format_naira(row.difference)} against a tolerance of {format_naira(row.tolerance)}."]
    if row.provider_account != row.held_total:
        parts.append(f"The provider balance account also differs from the held-funds "
                     f"sub-ledger ({format_naira(row.held_total)}): a held movement is not "
                     f"yet posted to the platform's books.")
    incident = report_configuration_fault(
        fault_key=FAULT_KEY,
        title=f"Held-ledger mismatch: {format_naira(abs(row.difference or 0))}",
        summary=" ".join(parts), severity=Severity.SEV2, who=WHO,
    )
    if incident is not None:
        _notify_operators(incident, row)
        return incident.code
    open_incident = (Incident.objects.filter(fault_key=FAULT_KEY)
                     .exclude(status=Incident.Status.RESOLVED).order_by("-started_at").first())
    return open_incident.code if open_incident is not None else ""


def _resolve_open_incidents() -> None:
    """Resolve the mismatch incident once the books and the provider agree again."""
    from vs_health.models import Incident

    now = timezone.now()
    for incident in (Incident.objects.filter(fault_key=FAULT_KEY)
                     .exclude(status=Incident.Status.RESOLVED)):
        incident.status = Incident.Status.RESOLVED
        incident.resolved_at = now
        incident.save(update_fields=["status", "resolved_at", "updated_at"])
        incident.add_event(kind="resolved", who=WHO,
                           text="The provider's balance and the platform's books agree again.")


def _notify_operators(incident, row) -> int:
    """Tell the platform's health and settlement operators. Never raises.

    The ``health.alert_fired`` event, so the message arrives where every other
    platform health alarm does: the observed difference against the tolerance.
    """
    try:
        from vs_notifications.notify import send_notification
        from vs_rbac.evaluator import resolve_users_with_permission
        from vs_tenants.models import Tenant

        from vs_health.constants import PERM_UPDATE

        platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        recipients = {}
        for key in (PERM_UPDATE, "payments.platform_settlement.view"):
            for user in resolve_users_with_permission(
                    tenant=platform, branch=None, permission_key=key):
                recipients[user.pk] = user
        if not recipients:
            logger.error("Held-ledger incident %s has no recipients.", incident.code)
            return 0
        sent = send_notification(
            event_key="health.alert_fired",
            context={
                "incident_code": incident.code,
                "rule_name": "Provider balance against the platform's books (kobo)",
                "severity_label": incident.get_severity_display(),
                "service_name": f"{row.provider} held balance",
                "observed_value": abs(int(row.difference or 0)),
                "comparator": ">",
                "threshold": int(row.tolerance),
                "fired_at": incident.started_at.isoformat(),
            },
            recipients=list(recipients.values()),
            tenant=platform,
            metadata={"incident_id": str(incident.id), "incident_code": incident.code},
        )
        return len(sent)
    except Exception:  # noqa: BLE001 - the incident must survive a delivery failure
        logger.exception("Held-ledger incident %s: notification failed.", incident.code)
        return 0
