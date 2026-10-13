"""A provider payment terminal for every branch, kept open and routed to the right money.

A terminal is a payment page and a set of transfer details a branch puts on its
desk or wall. Parents pay on it without naming a pupil, so the money is booked
to the branch and matched to a customer afterwards
(:mod:`vs_payments.terminal_payments`).

**One terminal per branch.** Creating a branch opens a :class:`VirtualTerminal`
row in the same transaction (:func:`on_branch_saved`) and, once that commits,
queues the provider call (``vs_payments.provision_terminal``). The row is what
makes the work recoverable: a call that failed, or a task that was lost, leaves
a ``PENDING`` or ``FAILED`` row the sweep (:func:`sweep`) opens again with a
growing wait. The platform's own tenant keeps none, and neither does any
deployment that has switched ``PAYMENTS_TERMINALS_AUTO_CREATE`` off. A branch
taken out of service for good (inactive or closed) has its terminal switched
off; the provider offers no way back, so a branch that returns gets a new one.
A suspended branch keeps its terminal, because suspension is meant to end.

**Where the money goes.** A terminal's payments land in the platform's provider
balance, which holds them for the branch like a checkout's
(:mod:`vs_payments.held`), unless the tenant's payments settle directly. Then
the terminal is routed through a provider split that sends every payment to the
branch's subaccount, which bears the fee. :func:`sync_terminal_routing` brings
the split in line with the tenant's custody mode and the branch's collection
account, and runs when the terminal is opened, when a subaccount is saved, when
the mode switches and from the sweep. Until the split is attached a payment
simply lands in the platform's balance, where it is held and settled to the
branch, so no payment is ever left unrecorded by a route that has not caught up.

Provider calls never run inside a database transaction, and one row is leased
to a single worker for the call (``claimed_at``), so a task and the sweep cannot
both open a terminal for the same branch.
"""
from __future__ import annotations

import datetime
import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from . import audit
from .constants import PaymentAuditAction, TerminalStatus
from .models import VirtualTerminal
from .providers.registry import get_provider

logger = logging.getLogger("vs_payments.terminals")

#: How long a worker holds a terminal row for one provider call.
LEASE = datetime.timedelta(minutes=10)

#: The longest the sweep waits between two attempts to open the same terminal.
MAX_BACKOFF = datetime.timedelta(hours=6)

#: Most terminals the sweep works on per run.
SWEEP_LIMIT = 100


# --------------------------------------------------------------------------- #
# Which branches have a terminal, and what it is called                       #
# --------------------------------------------------------------------------- #

def auto_create_enabled() -> bool:
    """Whether this deployment opens a terminal when a branch is created."""
    return bool(getattr(settings, "PAYMENTS_TERMINALS_AUTO_CREATE", False))


def terminal_applies_to(tenant) -> bool:
    """Whether ``tenant`` keeps branch terminals: every tenant but the platform's own."""
    from vs_tenants.models import Tenant

    return tenant is not None and tenant.kind != Tenant.Kind.PLATFORM


def terminal_name(tenant, branch) -> str:
    """The terminal's name at the provider: the tenant, a hyphen, then the branch."""
    return f"{tenant.name} - {branch.name}"[:255]


#: What a terminal that cannot be opened for want of a number tells its readers.
NEEDS_NUMBER_REASON = "Add the branch's phone number to open its terminal."

#: Most numbers the provider notifies for one terminal.
MAX_DESTINATIONS = 5

_DIGIT_RUN = re.compile(r"\+?\d{9,}")


def normalise_whatsapp(raw) -> str:
    """A WhatsApp number in international form without a plus (``2348012345678``), or "" when it is not one.

    Spaces, hyphens, brackets and a leading plus or ``00`` are dropped. A Nigerian
    number written with its leading zero (``08012345678``) gets the country code,
    and a ten-digit mobile number written without either gets it too. Any other
    number must already start with its country code: a number starting with a
    zero that is not an eleven-digit Nigerian one is refused, since no
    international number does.
    """
    digits = re.sub(r"[\s\-()]", "", str(raw or ""))
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits.isdigit():
        return ""
    if digits.startswith("0"):
        if len(digits) != 11:
            return ""
        digits = "234" + digits[1:]
    elif len(digits) == 10 and digits[0] in "789":
        digits = "234" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def scrub_numbers(message) -> str:
    """``message`` with every phone-number-like run of digits replaced, so no number reaches a log or the audit trail."""
    return _DIGIT_RUN.sub("[number]", str(message or ""))


def branch_destinations(branch) -> list[dict]:
    """The branch's own phone as a terminal's first destination, named for the branch; empty with no usable phone."""
    number = normalise_whatsapp(branch.phone)
    return [{"name": branch.name[:100], "number": number}] if number else []


def tenant_entity(tenant):
    """The tenant's active set of books that a terminal payment is booked in, or None."""
    from vs_finance.models import LedgerEntity

    return (LedgerEntity.objects.filter(tenant=tenant, is_active=True)
            .select_related("base_currency").order_by("pk").first())


def live_terminal(branch, provider=None):
    """The branch's terminal that has not been deactivated, or None."""
    qs = VirtualTerminal.objects.filter(branch=branch).exclude(status=TerminalStatus.DEACTIVATED)
    if provider:
        qs = qs.filter(provider=provider)
    return qs.order_by("-id").first()


def ensure_terminal(branch, *, provider=None):
    """The branch's live terminal row, created ``PENDING`` when it has none. None where none applies.

    Writes only the row; the provider is not called. Safe to call inside the
    transaction that creates the branch, and again at any time: the partial
    unique index on live terminals is what keeps two callers from making two.
    """
    from .services import resolve_provider_name

    tenant = branch.tenant
    if not terminal_applies_to(tenant):
        return None
    provider_name = resolve_provider_name(provider)
    existing = live_terminal(branch, provider_name)
    if existing is not None:
        return existing
    destinations = branch_destinations(branch)
    try:
        with transaction.atomic():
            return VirtualTerminal.objects.create(
                tenant=tenant, branch=branch, provider=provider_name,
                name=terminal_name(tenant, branch), destinations=destinations,
                status=TerminalStatus.PENDING if destinations else TerminalStatus.NEEDS_NUMBER,
                last_error="" if destinations else NEEDS_NUMBER_REASON)
    except IntegrityError:  # Another caller made the branch's terminal first.
        return live_terminal(branch, provider_name)


# --------------------------------------------------------------------------- #
# Reacting to branches                                                        #
# --------------------------------------------------------------------------- #

def on_branch_saved(sender, instance, created, update_fields=None, **kwargs):
    """Post-save receiver for a branch: open its terminal, or switch it off when it leaves service.

    Acts when the branch is created, or saved with a status change (or with no
    field list, which may carry one). Creation and a return to service ensure
    the terminal row in the branch's own transaction and queue the provider
    call for after it commits; an inactive or closed branch queues the
    deactivation. A save that changes some other column does nothing, so a
    renamed branch of a tenant that has no terminal does not suddenly get one.
    A save that may have given the branch a phone number wakes its terminal if
    that is waiting for one (``NEEDS_NUMBER``).
    """
    if kwargs.get("raw") or not auto_create_enabled():
        return
    if not terminal_applies_to(instance.tenant):
        return
    if not created and (update_fields is None or "phone" in update_fields):
        waiting = VirtualTerminal.objects.filter(
            branch=instance, status=TerminalStatus.NEEDS_NUMBER).first()
        if waiting is not None:
            transaction.on_commit(lambda: _queue("vs_payments.provision_terminal", waiting.pk))
    if not created and update_fields is not None and "status" not in update_fields:
        return
    from vs_tenants.models import BranchStatus

    if instance.status in (BranchStatus.INACTIVE, BranchStatus.CLOSED):
        transaction.on_commit(lambda: _queue("vs_payments.deactivate_branch_terminals", instance.pk))
        return
    if instance.status == BranchStatus.SUSPENDED:
        return
    if not created and not VirtualTerminal.objects.filter(branch=instance).exists():
        return  # A branch that never had a terminal is the backfill's, not a status change's.
    if not created and live_terminal(instance) is not None:
        return
    row = ensure_terminal(instance)
    if row is not None:
        transaction.on_commit(lambda: _queue("vs_payments.provision_terminal", row.pk))


def _queue(task_name: str, *args) -> None:
    """Hand ``task_name`` to the worker; under eager execution it runs here, after the commit."""
    from . import tasks

    getattr(tasks, task_name.split(".", 1)[1]).delay(*args)


# --------------------------------------------------------------------------- #
# Opening a terminal                                                          #
# --------------------------------------------------------------------------- #

def _claim(terminal_id: int, *, now) -> bool:
    """Lease the row to this worker for one provider call; False when another holds it."""
    return bool(
        VirtualTerminal.objects.filter(
            pk=terminal_id, status__in=(TerminalStatus.PENDING, TerminalStatus.FAILED),
        ).filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - LEASE))
        .update(claimed_at=now, attempts=F("attempts") + 1)
    )


def provision_terminal(terminal_id: int):
    """Open the terminal at the provider, or record why it could not be. Returns the row.

    A terminal that is already open, deactivated, or leased to another worker is
    returned untouched, so a duplicate task or a sweep overlapping a retry is
    harmless. A terminal with no destination takes the branch's own phone as its
    first; if the branch has none it is left ``NEEDS_NUMBER`` and the provider is
    not called. A refusal or an unreachable provider leaves it ``FAILED`` with the
    reason in ``last_error`` (with any phone number in the provider's words
    masked); the sweep tries again. A terminal that opened has
    its settlement route attached straight away, and a failure there is logged
    and left to the sweep, since the terminal itself works without it.
    """
    from .services import _assert_no_open_transaction

    _assert_no_open_transaction("Opening a payment terminal")
    now = timezone.now()
    row = VirtualTerminal.objects.select_related("branch").filter(pk=terminal_id).first()
    if row is None or row.status not in (
            TerminalStatus.NEEDS_NUMBER, TerminalStatus.PENDING, TerminalStatus.FAILED):
        return row
    if not row.destinations:
        seeded = branch_destinations(row.branch)
        if not seeded:
            VirtualTerminal.objects.filter(pk=row.pk).update(
                status=TerminalStatus.NEEDS_NUMBER, last_error=NEEDS_NUMBER_REASON)
            return VirtualTerminal.objects.get(pk=row.pk)
        VirtualTerminal.objects.filter(pk=row.pk).update(destinations=seeded)
    if row.status == TerminalStatus.NEEDS_NUMBER:
        VirtualTerminal.objects.filter(pk=row.pk).update(
            status=TerminalStatus.PENDING, last_error="")
    if not _claim(terminal_id, now=now):
        return VirtualTerminal.objects.filter(pk=terminal_id).first()
    terminal = VirtualTerminal.objects.select_related("tenant", "branch").get(pk=terminal_id)
    entity = tenant_entity(terminal.tenant)
    currency = getattr(getattr(entity, "base_currency", None), "code", None) or "NGN"
    sent = [{"target": d["number"], "name": d["name"]} for d in terminal.destinations]
    try:
        client = get_provider(terminal.provider)
        result = client.create_terminal(
            name=terminal.name, destinations=sent, currency=currency,
            metadata={"tenant": terminal.tenant.slug, "branch_id": terminal.branch_id},
        )
        if not result.code:
            from .exceptions import ProviderError

            raise ProviderError("The payment provider returned no terminal code.",
                                provider=terminal.provider)
    except Exception as exc:  # noqa: BLE001 - recorded on the row; the sweep retries.
        message = scrub_numbers(str(getattr(exc, "message", exc)))[:500]
        VirtualTerminal.objects.filter(pk=terminal.pk).update(
            status=TerminalStatus.FAILED, last_error=message, claimed_at=None,
            updated_at=timezone.now())
        audit.record(
            action=PaymentAuditAction.TERMINAL_FAILED, entity=entity, succeeded=False,
            provider=terminal.provider, reference=terminal.name[:64], message=message[:255],
            metadata={"branch_id": terminal.branch_id, "terminal_id": terminal.pk},
        )
        return VirtualTerminal.objects.get(pk=terminal.pk)

    VirtualTerminal.objects.filter(pk=terminal.pk).update(
        code=result.code, status=TerminalStatus.ACTIVE, last_error="",
        provider_details=result.details or {}, claimed_at=None, updated_at=timezone.now())
    terminal.refresh_from_db()
    audit.record(
        action=PaymentAuditAction.TERMINAL_CREATED, entity=entity,
        provider=terminal.provider, reference=terminal.code,
        message=f"Opened the payment terminal for {terminal.branch.name}.",
        metadata={"branch_id": terminal.branch_id, "terminal_id": terminal.pk},
    )
    try:
        _reconcile_destinations(terminal, sent)
        sync_terminal_routing(terminal)
    except Exception:  # noqa: BLE001 - the sweep repairs a route that did not attach.
        logger.exception("Terminal %s opened but its route could not be attached.", terminal.code)
    return terminal


def _reconcile_destinations(terminal, sent) -> None:
    """Bring the provider's numbers in line with the terminal's if they changed while it was being opened."""
    terminal.refresh_from_db(fields=["destinations"])
    have = {d["target"] for d in sent}
    want = {d["number"]: d["name"] for d in terminal.destinations}
    client = get_provider(terminal.provider)
    extra = sorted(have - set(want))
    if extra:
        client.unassign_terminal_destinations(code=terminal.code, targets=extra)
    missing = [{"target": number, "name": name} for number, name in want.items() if number not in have]
    if missing:
        client.assign_terminal_destinations(code=terminal.code, destinations=missing)


def retry_terminal(terminal):
    """Try again to open a ``PENDING`` or ``FAILED`` terminal now; refresh an active one's details.

    Used by the retry endpoint. An active terminal is asked about instead,
    which fills in payment details the provider had not produced when it was
    opened. A deactivated one is refused by the caller.
    """
    if terminal.status == TerminalStatus.ACTIVE:
        terminal = refresh_terminal(terminal)
        sync_terminal_routing(terminal)
        return terminal
    VirtualTerminal.objects.filter(pk=terminal.pk).update(claimed_at=None)
    return provision_terminal(terminal.pk)


def refresh_terminal(terminal):
    """Read the terminal's details from the provider and keep them. Returns the row."""
    result = get_provider(terminal.provider).fetch_terminal(code=terminal.code)
    VirtualTerminal.objects.filter(pk=terminal.pk).update(
        provider_details=result.details or terminal.provider_details, updated_at=timezone.now())
    terminal.refresh_from_db()
    return terminal


# --------------------------------------------------------------------------- #
# The numbers a terminal notifies                                             #
# --------------------------------------------------------------------------- #

def add_destination(terminal, *, name, number, actor_user=None) -> list[dict]:
    """Add a WhatsApp number to the terminal's notifications. Returns the terminal's destinations.

    The number is normalised (:func:`normalise_whatsapp`); one the terminal
    already has, an empty name, an unusable number, or a sixth number is
    refused. The row changes first, under a lock, and the provider is then
    asked to add it when the terminal is open; if the provider refuses, the row
    is put back and the refusal surfaces. A terminal waiting for a number
    (``NEEDS_NUMBER``) becomes ``PENDING`` and is opened once the change
    commits. The audit trail records that a number was added, never the number.
    """
    from rest_framework.exceptions import ValidationError

    from .exceptions import PaymentStateError

    target = normalise_whatsapp(number)
    if not target:
        raise ValidationError({"number": (
            "Enter a WhatsApp number in international form, for example 2348012345678, "
            "or a Nigerian number starting with 0.")})
    label = str(name or "").strip()[:100]
    if not label:
        raise ValidationError({"name": "Give the name of the person this number belongs to."})
    with transaction.atomic():
        row = VirtualTerminal.objects.select_for_update().get(pk=terminal.pk)
        if row.status == TerminalStatus.DEACTIVATED:
            raise PaymentStateError("This terminal has been deactivated.", terminal=row.pk)
        current = list(row.destinations or [])
        if any(d["number"] == target for d in current):
            raise ValidationError({"number": "This number is already on the terminal."})
        if len(current) >= MAX_DESTINATIONS:
            raise PaymentStateError(
                f"A terminal can notify at most {MAX_DESTINATIONS} numbers.", terminal=row.pk)
        row.destinations = [*current, {"name": label, "number": target}]
        was_waiting = row.status == TerminalStatus.NEEDS_NUMBER
        if was_waiting:
            row.status, row.last_error = TerminalStatus.PENDING, ""
        row.save(update_fields=["destinations", "status", "last_error", "updated_at"])
        if was_waiting:
            transaction.on_commit(lambda: _queue("vs_payments.provision_terminal", row.pk))
    if row.code and row.status == TerminalStatus.ACTIVE:
        try:
            get_provider(row.provider).assign_terminal_destinations(
                code=row.code, destinations=[{"target": target, "name": label}])
        except Exception:
            VirtualTerminal.objects.filter(pk=row.pk).update(destinations=current)
            raise
    audit.record(
        action=PaymentAuditAction.TERMINAL_DESTINATION_ADDED, entity=tenant_entity(row.tenant),
        provider=row.provider, reference=row.code or row.name[:64], actor_user=actor_user,
        message=f"Added a notification number to the terminal for {row.branch.name}.",
        metadata={"branch_id": row.branch_id, "terminal_id": row.pk, "count": len(row.destinations)},
    )
    return list(row.destinations)


def remove_destination(terminal, *, number, actor_user=None) -> list[dict]:
    """Remove a number from the terminal's notifications. Returns the terminal's destinations.

    The terminal's last number cannot be removed (:class:`LastDestinationError`):
    the provider requires one, and a terminal that notified nobody would also
    reopen the way for a payment to arrive unseen. A number the terminal does not
    have is refused (400). The row changes first, under a lock, and the provider
    is then asked to drop it when the terminal is open; a provider refusal puts
    the row back. The audit trail records that a number was removed, never the
    number.
    """
    from rest_framework.exceptions import ValidationError

    from .exceptions import LastDestinationError

    target = normalise_whatsapp(number)
    with transaction.atomic():
        row = VirtualTerminal.objects.select_for_update().get(pk=terminal.pk)
        current = list(row.destinations or [])
        remaining = [d for d in current if d["number"] != target]
        if not target or len(remaining) == len(current):
            raise ValidationError({"number": "This number is not on the terminal."})
        if not remaining:
            raise LastDestinationError(terminal=row.pk)
        row.destinations = remaining
        row.save(update_fields=["destinations", "updated_at"])
    if row.code and row.status == TerminalStatus.ACTIVE:
        try:
            get_provider(row.provider).unassign_terminal_destinations(
                code=row.code, targets=[target])
        except Exception:
            VirtualTerminal.objects.filter(pk=row.pk).update(destinations=current)
            raise
    audit.record(
        action=PaymentAuditAction.TERMINAL_DESTINATION_REMOVED, entity=tenant_entity(row.tenant),
        provider=row.provider, reference=row.code or row.name[:64], actor_user=actor_user,
        message=f"Removed a notification number from the terminal for {row.branch.name}.",
        metadata={"branch_id": row.branch_id, "terminal_id": row.pk, "count": len(remaining)},
    )
    return remaining


# --------------------------------------------------------------------------- #
# Switching a terminal off                                                    #
# --------------------------------------------------------------------------- #

def deactivate_terminal(terminal) -> VirtualTerminal:
    """Switch the terminal off at the provider and mark it deactivated.

    A terminal that was never opened (no code) is only marked. A provider
    refusal propagates, leaving the terminal as it was, and the sweep tries
    again while its branch stays out of service.
    """
    from .services import _assert_no_open_transaction

    if terminal.status == TerminalStatus.DEACTIVATED:
        return terminal
    _assert_no_open_transaction("Deactivating a payment terminal")
    if terminal.code:
        get_provider(terminal.provider).deactivate_terminal(code=terminal.code)
    VirtualTerminal.objects.filter(pk=terminal.pk).update(
        status=TerminalStatus.DEACTIVATED, deactivated_at=timezone.now(), claimed_at=None,
        updated_at=timezone.now())
    terminal.refresh_from_db()
    audit.record(
        action=PaymentAuditAction.TERMINAL_DEACTIVATED, entity=tenant_entity(terminal.tenant),
        provider=terminal.provider, reference=terminal.code or terminal.name[:64],
        message=f"Deactivated the payment terminal for {terminal.branch.name}.",
        metadata={"branch_id": terminal.branch_id, "terminal_id": terminal.pk},
    )
    return terminal


def deactivate_branch_terminals(branch_id: int) -> int:
    """Switch off the live terminals of ``branch_id`` if the branch is out of service for good.

    Returns the number deactivated. A branch back in service by the time this
    runs is left alone.
    """
    from vs_tenants.models import Branch, BranchStatus

    branch = Branch.all_objects.filter(pk=branch_id).first()
    if branch is None or branch.status not in (BranchStatus.INACTIVE, BranchStatus.CLOSED):
        return 0
    done = 0
    for terminal in (VirtualTerminal.objects.select_related("branch", "tenant")
                     .filter(branch=branch).exclude(status=TerminalStatus.DEACTIVATED)):
        deactivate_terminal(terminal)
        done += 1
    return done


# --------------------------------------------------------------------------- #
# Routing to the right money                                                  #
# --------------------------------------------------------------------------- #

def desired_subaccount(terminal) -> str:
    """The subaccount the terminal's payments must settle to now, or "" for the platform's balance.

    A subaccount only when the tenant's payments settle directly and the
    branch's collection account is set up at this terminal's provider; in
    every other case the money rightly goes to the platform's balance, which
    holds it for the branch.
    """
    from . import custody

    entity = tenant_entity(terminal.tenant)
    if entity is None or not custody.is_direct(entity):
        return ""
    bank = custody.branch_collection_account(entity, terminal.branch_id)
    if bank is not None and bank.gateway_subaccount_code and (
            bank.gateway_subaccount_provider in ("", terminal.provider)):
        return bank.gateway_subaccount_code
    return ""


def routing_is_current(terminal) -> bool:
    """Whether the split attached at the provider is the one the terminal should have."""
    return desired_subaccount(terminal) == terminal.split_subaccount and (
        bool(terminal.split_subaccount) == bool(terminal.split_code))


def sync_terminal_routing(terminal) -> bool:
    """Attach, replace or remove the terminal's split so it matches :func:`desired_subaccount`.

    Returns True when anything changed. A split that no longer applies is
    removed first, then a new one is created for the branch's subaccount and
    attached, and the row records the split and subaccount it now has. Skips a
    terminal that is not active. A provider failure propagates after leaving
    the row describing what is really attached.
    """
    from .services import _assert_no_open_transaction

    if terminal.status != TerminalStatus.ACTIVE or not terminal.code:
        return False
    want = desired_subaccount(terminal)
    if want == terminal.split_subaccount and bool(want) == bool(terminal.split_code):
        return False
    _assert_no_open_transaction("Routing a payment terminal")
    client = get_provider(terminal.provider)
    entity = tenant_entity(terminal.tenant)
    before = terminal.split_subaccount
    if terminal.split_code:
        client.detach_terminal_split(code=terminal.code, split_code=terminal.split_code)
        VirtualTerminal.objects.filter(pk=terminal.pk).update(
            split_code="", split_subaccount="", updated_at=timezone.now())
        terminal.split_code = terminal.split_subaccount = ""
    if want:
        currency = getattr(getattr(entity, "base_currency", None), "code", None) or "NGN"
        split = client.create_split(
            name=f"{terminal.name} settlement"[:100], subaccount_code=want, currency=currency)
        client.attach_terminal_split(code=terminal.code, split_code=split.split_code)
        VirtualTerminal.objects.filter(pk=terminal.pk).update(
            split_code=split.split_code, split_subaccount=want, updated_at=timezone.now())
        terminal.split_code, terminal.split_subaccount = split.split_code, want
    audit.record(
        action=PaymentAuditAction.TERMINAL_ROUTING_CHANGED, entity=entity,
        provider=terminal.provider, reference=terminal.code,
        message=(f"Routed {terminal.branch.name}'s terminal to its bank."
                 if want else f"Routed {terminal.branch.name}'s terminal to the platform's balance."),
        metadata={"branch_id": terminal.branch_id, "terminal_id": terminal.pk,
                  "from_subaccount": before, "to_subaccount": want},
    )
    return True


def sync_tenant_terminals(tenant, *, branch_id=None) -> dict:
    """Bring every active terminal of ``tenant`` (or of one branch) in line with its route.

    Called when a tenant's custody mode switches and when a branch's collection
    subaccount is saved. One terminal's failure is logged and counted and never
    stops the rest; the sweep retries it.
    """
    summary = {"changed": 0, "failed": 0}
    qs = VirtualTerminal.objects.select_related("tenant", "branch").filter(
        tenant=tenant, status=TerminalStatus.ACTIVE)
    if branch_id is not None:
        qs = qs.filter(branch_id=branch_id)
    for terminal in qs:
        try:
            if sync_terminal_routing(terminal):
                summary["changed"] += 1
        except Exception:  # noqa: BLE001 - the sweep retries it
            logger.exception("Terminal %s could not be re-routed.", terminal.code)
            summary["failed"] += 1
    return summary


# --------------------------------------------------------------------------- #
# The sweep and the backfill                                                  #
# --------------------------------------------------------------------------- #

def _retry_due(terminal, now) -> bool:
    """Whether a terminal that is not open has waited out its backoff since it was last tried."""
    wait = min(datetime.timedelta(minutes=5) * (2 ** max(terminal.attempts - 1, 0)), MAX_BACKOFF)
    return now - terminal.updated_at >= wait


def sweep(*, now=None) -> dict:
    """Open terminals that failed or never started, and switch off those of branches that left.

    Returns counts: ``opened``, ``still_failing`` and ``needs_number`` for set-up
    retries and ``deactivated`` for branches that left service. Each pass is bounded by
    :data:`SWEEP_LIMIT`, and one terminal's failure never stops the rest.
    Settlement routes are repaired by :func:`repair_routes`.
    """
    from vs_tenants.models import BranchStatus

    now = now or timezone.now()
    summary = {"opened": 0, "still_failing": 0, "needs_number": 0, "deactivated": 0}
    waiting = (VirtualTerminal.objects
               .filter(status__in=(TerminalStatus.PENDING, TerminalStatus.FAILED,
                                   TerminalStatus.NEEDS_NUMBER))
               .exclude(branch__status__in=(BranchStatus.INACTIVE, BranchStatus.CLOSED))
               .order_by("updated_at")[:SWEEP_LIMIT * 3])
    tried = 0
    for terminal in waiting:
        if tried >= SWEEP_LIMIT:
            break
        if not _retry_due(terminal, now):
            continue
        tried += 1
        row = provision_terminal(terminal.pk)
        if row is not None and row.status == TerminalStatus.ACTIVE:
            summary["opened"] += 1
        elif row is not None and row.status == TerminalStatus.NEEDS_NUMBER:
            summary["needs_number"] += 1
        else:
            summary["still_failing"] += 1

    leavers = (VirtualTerminal.objects.select_related("branch", "tenant")
               .exclude(status=TerminalStatus.DEACTIVATED)
               .filter(branch__status__in=(BranchStatus.INACTIVE, BranchStatus.CLOSED))
               .order_by("pk")[:SWEEP_LIMIT])
    for terminal in leavers:
        try:
            deactivate_terminal(terminal)
            summary["deactivated"] += 1
        except Exception:  # noqa: BLE001 - tried again next run
            logger.exception("Terminal %s could not be deactivated.", terminal.pk)

    return summary


def repair_routes() -> dict:
    """Re-route every active terminal whose split no longer matches its tenant's custody.

    Only terminals that can differ are read: those with a split attached, and
    those of a tenant that has chosen a custody setting. Returns ``rerouted``
    and ``failed`` counts; a failure is logged and the next run tries again.
    """
    summary = {"rerouted": 0, "failed": 0}
    routed = (VirtualTerminal.objects.select_related("tenant", "branch")
              .filter(status=TerminalStatus.ACTIVE)
              .filter(Q(split_code__gt="") | Q(tenant__payment_custody__isnull=False))
              .order_by("pk"))
    for terminal in routed.iterator(chunk_size=200):
        try:
            if sync_terminal_routing(terminal):
                summary["rerouted"] += 1
        except Exception:  # noqa: BLE001 - tried again next run
            logger.exception("Terminal %s could not be re-routed.", terminal.code)
            summary["failed"] += 1
    return summary


def backfill(*, tenant_slug=None, dry_run=False, open_now=True) -> dict:
    """Give every in-service branch that has no live terminal one. Idempotent.

    Skips the platform's own tenant, a branch that already has a live terminal,
    and a branch that is out of service. With ``open_now`` the provider is
    asked for each new terminal straight away; without it only the rows are
    written and the sweep opens them. ``dry_run`` writes nothing.
    Returns ``created``, ``existing``, ``opened``, ``needs_number`` (the branch has
    no phone number) and ``failed`` counts.
    """
    from vs_tenants.models import Branch, BranchStatus, Tenant

    summary = {"created": 0, "existing": 0, "opened": 0, "needs_number": 0, "failed": 0}
    branches = (Branch.all_objects.select_related("tenant")
                .filter(status__in=(BranchStatus.ACTIVE, BranchStatus.PENDING))
                .exclude(tenant__kind=Tenant.Kind.PLATFORM).order_by("tenant_id", "pk"))
    if tenant_slug:
        branches = branches.filter(tenant__slug=tenant_slug)
    for branch in branches:
        if live_terminal(branch) is not None:
            summary["existing"] += 1
            if not dry_run and open_now:
                _open_if_waiting(branch, summary)
            continue
        summary["created"] += 1
        if dry_run:
            continue
        row = ensure_terminal(branch)
        if row is not None and open_now:
            _open_row(row, summary)
    return summary


def _open_if_waiting(branch, summary) -> None:
    row = live_terminal(branch)
    if row is not None and row.status in (
            TerminalStatus.PENDING, TerminalStatus.FAILED, TerminalStatus.NEEDS_NUMBER):
        _open_row(row, summary)


def _open_row(row, summary) -> None:
    opened = provision_terminal(row.pk)
    if opened is not None and opened.status == TerminalStatus.ACTIVE:
        summary["opened"] += 1
    elif opened is not None and opened.status == TerminalStatus.NEEDS_NUMBER:
        summary["needs_number"] += 1
    else:
        summary["failed"] += 1
