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
``payments.held_reconciliation_tolerance``, in naira, default 0), or the account
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
no incident: nothing was measured.

**A provider that sweeps the balance.** The platform setting
``payments.provider_balance_swept`` (default off) says the provider moves the
platform's balance to the platform's bank by automatic settlement. Off, the
check is the comparison above and never asks for settlements. On, it reads the
provider's settlement records first and counts each successful settlement of
the main balance once (:class:`~vs_payments.models.ProviderSweep`, unique on
the provider's settlement id), then takes from the books' figure every sweep
counted so far, since that money has left the balance for the platform's bank.
Sweeps counted while the setting was on still count after it is turned off:
their money left the balance all the same.

A sweep is taken into the comparison, not booked as a journal. It mixes held
money with the platform's own takings, whose arrival in the bank is booked when
finance matches the bank line to them (:mod:`vs_payments.settlement`), so a
journal for the whole sweep would book that part twice; and the provider
balance account is the mirror of the held-funds sub-ledger, which a sweep does
not change. When finance does match the platform's own takings to a bank line
after sweeping began, they leave transit, and the check adds them back
(``own_swept_settled``) because the sweep that carried them was already taken
off once.

Paystack sweeps Bright Star Lekki's N178,000 and the platform's N5,000 to the
platform's Zenith account overnight: one settlement of N183,000. The next
morning Paystack reports N40,000 (Greenfield's, which arrived after the cut).
The books say N178,000 + N40,000 + N5,000 in transit, less N183,000 swept:
N40,000, and the check agrees. Run again at noon, the same settlement is not
counted a second time.

The settlement read is guarded:

* only the main balance's settlements count (a branch subaccount's settlement
  never touched the balance), and only successful ones;
* a settlement in another currency than NGN, or one with no amount, refuses the
  read: the check is recorded with the reason, no comparison is made, and a
  separate incident (``payments.provider-sweep-refused``) stays open until a
  later read is clean;
* the window runs from a week before the latest settlement counted (31 days
  back on the first read) to the platform's tomorrow, so a settlement dated on
  the provider's UTC day, or recorded late, is still read; the unique key makes
  the overlap harmless;
* the settlements are read before and after the balance; a settlement that
  appears between the two may or may not be in the balance read, so the
  balance is read again (three reads at most, then the check is recorded as not
  measured);
* a settlement the provider is still paying is not counted, but a difference
  it alone explains is recorded as not measured rather than raised.
"""
from __future__ import annotations

import datetime
import logging

from django.db.models import Min, Sum
from django.utils import timezone

logger = logging.getLogger("vs_payments.held_reconciliation")

#: The one open incident a disagreement is filed under.
FAULT_KEY = "payments.held-ledger-mismatch"

#: The incident a settlement record the check refuses to count is filed under.
SWEEP_FAULT_KEY = "payments.provider-sweep-refused"

#: The platform setting holding the tolerance, in whole naira as an operator types it.
TOLERANCE_KEY = "payments.held_reconciliation_tolerance"

#: The platform setting saying the provider sweeps the platform's balance to its bank.
SWEPT_KEY = "payments.provider_balance_swept"

#: The only currency whose settlements are counted.
SWEEP_CURRENCY = "NGN"

#: Days read back on the first read of settlements, before any is counted.
FIRST_LOOKBACK_DAYS = 31

#: Days re-read before the latest settlement counted.
OVERLAP_DAYS = 7

#: Reads of the balance before a check that keeps finding new settlements gives up.
MAX_BALANCE_READS = 3

#: Who an incident and its events are attributed to.
WHO = "Held-ledger reconciliation"


class SweepRefused(Exception):
    """A settlement record the check will not count: another currency, no amount or no id."""


class SweepUnsettled(Exception):
    """Settlements kept appearing while the balance was read, so nothing was measured."""


def tolerance() -> int:
    """Kobo the provider's balance may differ from the books by before it is an incident.

    The setting is typed in naira, as every amount a person sees is
    (:data:`TOLERANCE_KEY`); the check compares kobo, so it is converted here.
    """
    from vs_config.conf import get_config

    try:
        return max(0, int(get_config(TOLERANCE_KEY, 0) or 0)) * 100
    except (TypeError, ValueError):
        return 0


def balance_swept() -> bool:
    """Whether the provider sweeps the platform's balance to its bank (platform setting, default off)."""
    from vs_config.conf import get_config

    value = get_config(SWEPT_KEY, False)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


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


def swept_total(provider_name: str, currency: str) -> int:
    """Kobo of every settlement counted for ``provider_name`` in ``currency``."""
    from .models import ProviderSweep

    total = (ProviderSweep.objects.filter(provider=provider_name, currency=currency.upper())
             .aggregate(total=Sum("amount"))["total"])
    return int(total or 0)


def first_sweep_day(provider_name: str, currency: str, zone):
    """The platform day of the earliest settlement counted, or None before any is."""
    from .models import ProviderSweep

    first = (ProviderSweep.objects.filter(provider=provider_name, currency=currency.upper())
             .aggregate(settled=Min("settled_at"), recorded=Min("recorded_on")))
    days = [day for day in (
        first["settled"].astimezone(zone).date() if first["settled"] else None,
        first["recorded"],
    ) if day is not None]
    return min(days) if days else None


def own_swept_settled(platform, since) -> int:
    """The platform's own takings matched to a bank line on or after ``since``, less their fees.

    Once the provider sweeps the balance, the platform's own takings reach its
    bank only inside a sweep, so one finance matches to a bank line from the
    first sweep on was carried by a sweep the check already took off.
    """
    if platform is None or since is None:
        return 0
    from vs_finance.constants import DocumentStatus

    from .constants import CollectionStatus
    from .models import CollectionIntent

    rows = (CollectionIntent.objects.filter(
        entity=platform, held_by_platform=False, status=CollectionStatus.SUCCEEDED,
        settlement_entry__status=DocumentStatus.POSTED, settlement_entry__date__gte=since,
    ).values_list("amount", "fee"))
    return sum(int(amount) - int(fee or 0) for amount, fee in rows)


def read_sweeps(gateway, provider_name: str, *, today, zone) -> tuple[int, int]:
    """Count every new successful settlement of the main balance; answer (new, in flight).

    ``new`` is how many settlements this read counted for the first time and
    ``in flight`` the kobo of settlements the provider is still paying. Raises
    :class:`SweepRefused` for a settlement it will not count, before counting
    any of the read.
    """
    from .models import ProviderSweep

    latest = (ProviderSweep.objects.filter(provider=provider_name)
              .exclude(settled_at=None).order_by("-settled_at").first())
    if latest is not None:
        start = min(latest.settled_at.astimezone(zone).date(), today) - datetime.timedelta(
            days=OVERLAP_DAYS)
    else:
        start = today - datetime.timedelta(days=FIRST_LOOKBACK_DAYS)
    records = gateway.list_settlements(start=start, end=today + datetime.timedelta(days=1))

    known = set(ProviderSweep.objects.filter(provider=provider_name)
                .values_list("settlement_id", flat=True))
    settled, in_flight = [], 0
    for record in records:
        if record.subaccount or record.status not in ("SETTLED", "PENDING"):
            if record.status == "UNKNOWN":
                logger.warning("%s settlement %s has a status the check does not know: %s",
                               provider_name, record.settlement_id, record.raw.get("status"))
            continue
        if record.currency != SWEEP_CURRENCY:
            raise SweepRefused(
                f"{provider_name} settlement {record.settlement_id or '(no id)'} is in "
                f"{record.currency or 'no currency'}; only {SWEEP_CURRENCY} settlements are "
                f"counted.")
        if not record.settlement_id:
            raise SweepRefused(f"{provider_name} listed a settlement with no id.")
        if record.amount is None or record.amount < 0:
            raise SweepRefused(
                f"{provider_name} settlement {record.settlement_id} has no readable amount.")
        if record.settlement_id in known:
            continue
        if record.status == "PENDING":
            in_flight += record.amount
        else:
            settled.append(record)

    new = 0
    for record in settled:
        _, created = ProviderSweep.objects.get_or_create(
            provider=provider_name, settlement_id=record.settlement_id,
            defaults={
                "currency": record.currency, "amount": record.amount,
                "settled_at": record.settled_at, "provider_status": str(
                    record.raw.get("status", "") or "")[:32],
                "recorded_on": today, "raw": record.raw,
            },
        )
        new += int(created)
    return new, in_flight


def read_balance_with_sweeps(gateway, provider_name: str, currency: str, *, today, zone):
    """The provider's balance, read between two reads of its settlements; answers (balance, in flight).

    A settlement that appears in the second read may have left the balance
    before or after it was read, so the balance is read again, up to
    :data:`MAX_BALANCE_READS` times, then :class:`SweepUnsettled` is raised.
    """
    if currency.upper() != SWEEP_CURRENCY:
        raise SweepRefused(
            f"Swept balances are checked in {SWEEP_CURRENCY} only, not {currency.upper()}.")
    read_sweeps(gateway, provider_name, today=today, zone=zone)
    for _ in range(MAX_BALANCE_READS):
        reported = int(gateway.available_balance(currency))
        new, in_flight = read_sweeps(gateway, provider_name, today=today, zone=zone)
        if not new:
            return reported, in_flight
    raise SweepUnsettled(
        f"{provider_name} kept settling to the bank while its balance was read; nothing was "
        f"measured.")


def reconcile_held_ledger(*, provider=None, currency="NGN", today=None):
    """Check the platform's books against its provider balance today, and record it.

    Returns the day's :class:`~vs_payments.models.HeldReconciliation`; a second
    check the same day replaces the first's figures.
    """
    from vs_config.clock import tenant_today, tenant_zone

    from .held import platform_books
    from .models import HeldBalance, HeldReconciliation
    from .providers.registry import get_provider
    from .services import resolve_provider_name

    platform = platform_books()
    provider_name = resolve_provider_name(provider)
    today = today or (tenant_today(platform.tenant) if platform is not None else timezone.localdate())
    zone = tenant_zone(platform.tenant) if platform is not None else timezone.get_current_timezone()
    swept = balance_swept()
    reported, error, in_flight, refused = None, "", 0, ""
    try:
        gateway = get_provider(provider_name)
        if swept:
            reported, in_flight = read_balance_with_sweeps(
                gateway, provider_name, currency, today=today, zone=zone)
        else:
            reported = int(gateway.available_balance(currency))
    except SweepRefused as exc:
        refused = error = str(exc)[:255]
        logger.error("Held-ledger check refused %s's settlements: %s", provider_name, exc)
    except Exception as exc:  # noqa: BLE001 - a check that cannot ask is recorded, not raised.
        error = str(getattr(exc, "message", exc))[:255] or exc.__class__.__name__
    if refused:
        _raise_refused(provider_name, refused)
    elif reported is not None:
        _resolve_open_incidents(SWEEP_FAULT_KEY, (
            "The provider's settlements are read cleanly again." if swept
            else "The provider's settlements are no longer read: the balance is not swept."))

    account = provider_account_balance(platform) if platform is not None else 0
    held_total = int(HeldBalance.objects.aggregate(total=Sum("balance"))["total"] or 0)
    transit = own_in_transit(platform) if platform is not None else 0
    swept_out = swept_total(provider_name, currency)
    own_back = own_swept_settled(platform, first_sweep_day(provider_name, currency, zone))
    books = account + transit - swept_out + own_back
    limit = tolerance()
    difference = None if reported is None else reported - books
    agrees = difference is not None and abs(difference) <= limit and account == held_total
    if (difference is not None and not agrees and in_flight and account == held_total
            and abs(difference + in_flight) <= limit):
        from vs_finance.money import format_naira

        error = (f"A settlement of {format_naira(in_flight)} is still being paid by the "
                 f"provider and explains the difference; nothing was measured.")[:255]
        difference = None
    row, _ = HeldReconciliation.objects.update_or_create(
        provider=provider_name, currency=currency.upper(), checked_on=today,
        defaults={
            "provider_balance": reported, "provider_account": account, "held_total": held_total,
            "own_in_transit": transit, "balance_swept": swept, "swept_total": swept_out,
            "own_swept_settled": own_back, "books_balance": books, "difference": difference,
            "tolerance": limit, "agrees": agrees, "error": error,
        },
    )
    if difference is None:
        return row
    if agrees:
        _resolve_open_incidents(FAULT_KEY, "The provider's balance and the platform's books agree again.")
        row.incident_code = ""
    else:
        row.incident_code = _raise(row)
    row.save(update_fields=["incident_code", "updated_at"])
    return row


def _raise_refused(provider_name: str, reason: str) -> None:
    """Open the refused-settlement incident, once, while the provider's settlements cannot be counted."""
    from vs_health.faults import report_configuration_fault
    from vs_health.models import Severity

    report_configuration_fault(
        fault_key=SWEEP_FAULT_KEY,
        title=f"{provider_name} settlements could not be counted",
        summary=(f"{reason} The held-ledger check made no comparison. Check the settlement on "
                 f"the provider's dashboard; the check reads it again tomorrow."),
        severity=Severity.SEV2, who=WHO,
    )


def _raise(row) -> str:
    """Open the mismatch incident (or find the open one) and tell operators when it opens."""
    from vs_finance.money import format_naira
    from vs_health.faults import report_configuration_fault
    from vs_health.models import Incident, Severity

    parts = [f"{row.provider} reports {format_naira(row.provider_balance)} for {row.currency}; "
             f"the platform's books say {format_naira(row.books_balance)} "
             f"(provider balance account {format_naira(row.provider_account)}, own payments in "
             f"transit {format_naira(row.own_in_transit)}"
             + (f", less {format_naira(row.swept_total)} swept to the platform's bank"
                f" and plus {format_naira(row.own_swept_settled)} of its own takings settled"
                f" from those sweeps" if row.swept_total or row.own_swept_settled else "")
             + f"), a difference of "
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


def _resolve_open_incidents(fault_key: str, text: str) -> None:
    """Resolve the open incident under ``fault_key`` once its cause has cleared."""
    from vs_health.models import Incident

    now = timezone.now()
    for incident in (Incident.objects.filter(fault_key=fault_key)
                     .exclude(status=Incident.Status.RESOLVED)):
        incident.status = Incident.Status.RESOLVED
        incident.resolved_at = now
        incident.save(update_fields=["status", "resolved_at", "updated_at"])
        incident.add_event(kind="resolved", who=WHO, text=text)


def _notify_operators(incident, row) -> int:
    """Tell the platform's health and settlement operators. Never raises.

    The ``health.alert_fired`` event, so the message arrives where every other
    platform health alarm does: the observed difference against the tolerance,
    both written in naira.
    """
    try:
        from vs_finance.money import format_naira
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
                "rule_name": "Provider balance against the platform's books",
                "severity_label": incident.get_severity_display(),
                "service_name": f"{row.provider} held balance",
                "observed_value": format_naira(abs(int(row.difference or 0))),
                "comparator": ">",
                "threshold": format_naira(int(row.tolerance)),
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
