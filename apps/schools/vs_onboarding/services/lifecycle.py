"""Abandoned onboarding: warning, expiry, the operator list, and reinstatement.

The owner's decision, in four parts. A school that stays PENDING for ninety
days has abandoned its onboarding and is suspended; it is warned fourteen days
before that happens; platform operators get a list of the ageing ones every two
weeks; a suspended school can be returned to PENDING by hand.

**The clock is not reset by activity, by decision.** It runs from entry into
PENDING and only reinstatement restarts it, so a school that finishes four
tasks on day eighty still expires on day ninety. Do not add "last activity" to
this module without going back to the owner.

Three things in here are worth reading before changing anything.

**The clock is ``Tenant.pending_since``, never ``Tenant.created_at``.** A
reinstated school is old and pending at the same time, so a sweep measuring
from creation would suspend it again the morning after an operator restored it,
and again the morning after that. ``pending_since`` records entry into the
current PENDING spell and is re-stamped by reinstatement, which is the whole
reason it exists.

**The write goes through the School.** ``School.save()`` mirrors name, status
and the lifecycle stamps onto its tenant, so a tenant suspended beside its
school would be silently returned to PENDING by the next ordinary school edit.
This module therefore writes ``School.status`` and lets the mirror do the rest,
exactly as activation does in :mod:`~schools.vs_onboarding.services.go_live`.
A school-kind tenant that has no school profile at all (creation that failed
half way, or a tenant made directly) is written on the tenant, because there is
nothing to write through; it is counted separately so the report says so.

**A warning is sent once per pending spell, not once per day it is due.**
``Tenant.expiry_warned_at`` is what makes that true, and it is cleared with
``pending_since`` so a reinstated school is warned again in its new cycle. The
warning is never a precondition for expiry: a school nobody could reach still
expires on the ninetieth day.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditSeverity

from ..constants import (
    EVENT_EXPIRY_WARNING,
    EVENT_STALE_REPORT,
    ONBOARDING_EXPIRY_DAYS,
    ONBOARDING_EXPIRY_WARNING_DAYS,
    STALE_ONBOARDING_AFTER_DAYS,
    STALE_REPORT_WINDOW_DAYS,
)
from ..exceptions import OnboardingNotSuspended, OnboardingTenantNotSchool
from . import effects

logger = logging.getLogger("vs_onboarding")


def _pending_school_tenants(cutoff=None):
    """School tenants that are PENDING, oldest spell measured by ``since``.

    ``pending_since`` is the measure, with ``created_at`` only as a fallback
    for a row that predates the column and was never written through a status
    change. The fallback cannot bite a reinstated school: reinstatement always
    stamps ``pending_since``, so such a school never falls back again.

    Annotated rather than filtered with a pair of Q objects, because three
    callers now want the same "how long has this spell run" expression and one
    of them needs it as a range.
    """
    from vs_tenants.models import Tenant

    rows = (
        Tenant.objects
        .filter(kind=Tenant.Kind.SCHOOL, status=Tenant.Status.PENDING)
        .annotate(since=Coalesce("pending_since", "created_at"))
        # The school profile is read for every row (its name, and whether the
        # write can go through it at all), so it is joined rather than fetched
        # one school at a time.
        .select_related("school_profile")
        .order_by("pk")
    )
    if cutoff is not None:
        rows = rows.filter(since__lte=cutoff)
    return rows


def _spell_started(tenant):
    """When the tenant's current PENDING spell began.

    ``pending_since`` where it is set, ``created_at`` only as the fallback for
    a row written before that column existed. Every reader of the expiry window
    goes through this, so the sweep and the control room cannot disagree about
    which date they are counting from.
    """
    return (
        getattr(tenant, "since", None)
        or tenant.pending_since
        or tenant.created_at
    )


def _expires_at(since):
    """When a spell that started at ``since`` runs out. The only place it is computed."""
    return since + timezone.timedelta(days=ONBOARDING_EXPIRY_DAYS)


def _whole_days_between(later, earlier) -> int:
    """Days between two moments, counted as calendar days and never negative.

    Between dates rather than between timestamps: a school warned one second
    after it entered the window is fourteen days from expiry, and
    ``(14 days - 1 second).days`` is 13.
    """
    return max(
        (timezone.localtime(later).date() - timezone.localtime(earlier).date()).days,
        0,
    )


def expiry_outlook(tenant, *, now=None) -> dict:
    """Everything a screen needs to explain this school's expiry window.

    The single authority. The sweep warns and expires from these numbers and
    the control room renders them, so a countdown can never show a date the
    sweep does not intend to act on. Reads only columns the tenant already
    carries, so it costs no query: ``state/`` is query-budgeted and this must
    not be what breaks it.

    A tenant that is not PENDING has no expiry at all, and says so with
    ``applies: False`` and nulls rather than a leftover date. A live school is
    not "expiring in 2 days"; it is not expiring.
    """
    from vs_tenants.models import Tenant

    now = now or timezone.now()
    window = {
        "applies": False,
        "pending_since": None,
        "expires_at": None,
        "days_remaining": None,
        "warning_sent": False,
        "warning_sent_at": None,
        "expiry_days": ONBOARDING_EXPIRY_DAYS,
        "warning_days": ONBOARDING_EXPIRY_WARNING_DAYS,
    }
    if tenant is None or getattr(tenant, "status", None) != Tenant.Status.PENDING:
        return window

    since = _spell_started(tenant)
    expires_at = _expires_at(since)
    warned_at = getattr(tenant, "expiry_warned_at", None)
    window.update({
        "applies": True,
        "pending_since": since,
        "expires_at": expires_at,
        "days_remaining": _whole_days_between(expires_at, now),
        "warning_sent": warned_at is not None,
        "warning_sent_at": warned_at,
    })
    return window


def _pending_days(tenant, now) -> int:
    return max((now - _spell_started(tenant)).days, 0)


def _row(tenant, now) -> dict:
    school = effects.school_of(tenant)
    return {
        "tenant_id": tenant.pk,
        "slug": tenant.slug,
        "name": getattr(school, "name", None) or tenant.name,
        "since": _spell_started(tenant),
        "pending_days": _pending_days(tenant, now),
        "has_school_profile": school is not None,
    }


# ── The 90-day sweep ───────────────────────────────────────────────────────

def _suspend(tenant, now) -> bool:
    """Suspend one tenant. Returns True when it was written through its school."""
    from vs_tenants.models import Tenant

    school = effects.school_of(tenant)
    if school is not None:
        from schools.vs_schools.models import SchoolStatus

        school.status = SchoolStatus.SUSPENDED
        school.deactivated_at = now
        # No update_fields: the mirror in School.save() is what writes the
        # tenant, and it is the only thing keeping the two statuses equal.
        school.save()
        return True

    Tenant.objects.filter(pk=tenant.pk).update(
        status=Tenant.Status.SUSPENDED,
        deactivated_at=now,
        # The spell it was in has ended, so neither column that describes that
        # spell describes anything any more. Exactly what the mirror does for a
        # tenant that has a school to be written through.
        pending_since=None,
        expiry_warned_at=None,
        updated_at=now,
    )
    return False


def expire_stale_onboarding(*, now=None, dry_run: bool = False, actor=None) -> dict:
    """Suspend every school that has been onboarding for the expiry window.

    Idempotent by construction: a suspended tenant is no longer PENDING, so it
    is not selected again. Authentication already refuses a SUSPENDED tenant,
    so the school's sign-in dies with the status change and nothing else has to
    be revoked.
    """
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=ONBOARDING_EXPIRY_DAYS)

    expired: list[dict] = []
    for tenant in list(_pending_school_tenants(cutoff)):
        row = _row(tenant, now)
        expired.append(row)
        if dry_run:
            continue

        with transaction.atomic():
            _suspend(tenant, now)
            effects.record(
                tenant=tenant,
                action_type=AuditActionType.ONBOARDING_EXPIRED,
                entity_type="Tenant",
                entity_id=tenant.pk,
                actor=actor,
                entity_label=row["name"],
                severity=AuditSeverity.WARNING,
                before_data={"status": "PENDING"},
                diff_data={
                    "status": {"from": "PENDING", "to": "SUSPENDED"},
                    "pending_days": row["pending_days"],
                },
                metadata={"expiry_days": ONBOARDING_EXPIRY_DAYS},
            )
        logger.info(
            "Onboarding expired for tenant %s after %s days pending.",
            tenant.slug, row["pending_days"],
        )

    return {
        "cutoff": cutoff,
        "expiry_days": ONBOARDING_EXPIRY_DAYS,
        "expired": expired,
        "expired_count": len(expired),
        "without_school_profile": sum(
            1 for row in expired if not row["has_school_profile"]
        ),
        "dry_run": dry_run,
    }


# ── The 14-day warning ─────────────────────────────────────────────────────

def warn_expiring_onboarding(*, now=None, dry_run: bool = False) -> dict:
    """Tell each school still onboarding that its window is about to close.

    Sent once per pending spell, and that is the whole difficulty. "Is this
    school within fourteen days of expiry?" is true on all fourteen of them, so
    a sweep that asked only that would send the same warning every morning.
    ``Tenant.expiry_warned_at`` is what makes it once: set here, and cleared
    whenever the pending spell changes, so a reinstated school is warned again
    in its new cycle instead of being skipped for ever.

    Schools already past the expiry date are deliberately not warned. They are
    not going to expire, they are due to be expired, and :func:`run_sweep`
    expires before it warns for exactly that reason.
    """
    from vs_tenants.models import Tenant

    now = now or timezone.now()
    warn_cutoff = now - timezone.timedelta(
        days=ONBOARDING_EXPIRY_DAYS - ONBOARDING_EXPIRY_WARNING_DAYS,
    )
    expiry_cutoff = now - timezone.timedelta(days=ONBOARDING_EXPIRY_DAYS)

    candidates = (
        _pending_school_tenants(warn_cutoff)
        .filter(expiry_warned_at__isnull=True, since__gt=expiry_cutoff)
    )

    warned: list[dict] = []
    for tenant in list(candidates):
        row = _row(tenant, now)
        # Through the same authority the control room renders, not through
        # arithmetic of its own: a warning that named a different date from the
        # countdown on the school's screen would be worse than no warning.
        outlook = expiry_outlook(tenant, now=now)
        expires_at = outlook["expires_at"]
        row["days_remaining"] = outlook["days_remaining"]
        warned.append(row)
        if dry_run:
            continue

        # The stamp is written first and outside the notification's reach. If
        # dispatch fails, the school is not warned twice tomorrow: it is warned
        # once and the failure is in the log, which is the same trade every
        # other effect in this module makes.
        Tenant.objects.filter(pk=tenant.pk).update(
            expiry_warned_at=now, updated_at=now,
        )
        context = effects.school_context(tenant)
        context.update({
            "days_remaining": row["days_remaining"],
            "expires_on": timezone.localtime(expires_at).date().isoformat(),
            "expiry_days": ONBOARDING_EXPIRY_DAYS,
            "pending_days": row["pending_days"],
        })
        effects._send(
            EVENT_EXPIRY_WARNING,
            context,
            effects.notification_recipients(tenant),
            tenant,
        )
        logger.info(
            "Onboarding expiry warning sent to tenant %s (%s days remaining).",
            tenant.slug, row["days_remaining"],
        )

    return {
        "warn_cutoff": warn_cutoff,
        "warning_days": ONBOARDING_EXPIRY_WARNING_DAYS,
        "warned": warned,
        "warned_count": len(warned),
        "dry_run": dry_run,
    }


def run_sweep(*, now=None, dry_run: bool = False, actor=None) -> dict:
    """The daily job: expire what is out of time, then warn what is nearly out.

    The order is the contract. Expiring first means a school past ninety days
    is suspended rather than sent a warning about a deadline it has already
    missed, and it means the warning can never become a precondition for
    expiry.
    """
    now = now or timezone.now()
    expired = expire_stale_onboarding(now=now, dry_run=dry_run, actor=actor)
    warned = warn_expiring_onboarding(now=now, dry_run=dry_run)
    return {**expired, **warned}


# ── The two-weekly operator list ───────────────────────────────────────────

def _recently_expired(now):
    """Schools this sweep expired inside the reporting window.

    Read from the audit trail rather than from tenant status, because
    "SUSPENDED" alone does not say who suspended it or why, and a school
    suspended by hand for a commercial reason is not this list's business.
    """
    from vs_audit.models import AuditEvent, AuditModuleKey

    since = now - timezone.timedelta(days=STALE_REPORT_WINDOW_DAYS)
    return list(
        AuditEvent.objects
        .filter(
            module_key=AuditModuleKey.ONBOARDING,
            action_type=AuditActionType.ONBOARDING_EXPIRED,
            event_at__gte=since,
        )
        .select_related("tenant")
        .order_by("event_at")
    )


def _lines(rows) -> str:
    """The list as the template renders it: one school per line, or a dash.

    Built here rather than in the template because a template renders a list of
    dicts as its Python repr, and an operator opening the email would be shown
    the inside of this function.
    """
    if not rows:
        return "-"
    return "\n".join(
        f"- {row['name']} ({row['slug']}): {row['pending_days']} days pending"
        for row in rows
    )


def stale_onboarding_report(*, now=None, dispatch: bool = True) -> dict:
    """Assemble the stale-onboarding list and send it to platform operators.

    Silent on a clean fortnight: with nothing ageing and nothing expired there
    is no message, because a standing report that mostly says "nothing" is a
    report people stop opening.
    """
    now = now or timezone.now()
    ageing_cutoff = now - timezone.timedelta(days=STALE_ONBOARDING_AFTER_DAYS)

    ageing = [
        _row(tenant, now)
        for tenant in _pending_school_tenants(ageing_cutoff)
    ]
    expired = [
        {
            "tenant_id": event.tenant_id,
            "slug": getattr(event.tenant, "slug", ""),
            "name": event.entity_label or getattr(event.tenant, "name", ""),
            "pending_days": (event.diff_data or {}).get("pending_days", 0),
        }
        for event in _recently_expired(now)
    ]

    recipients = effects.operator_recipients() if dispatch else []
    context = {
        "ageing_count": len(ageing),
        "expired_count": len(expired),
        "stale_after_days": STALE_ONBOARDING_AFTER_DAYS,
        "expiry_days": ONBOARDING_EXPIRY_DAYS,
        "window_days": STALE_REPORT_WINDOW_DAYS,
        "ageing_list": _lines(ageing),
        "expired_list": _lines(expired),
    }

    dispatched = False
    if dispatch and (ageing or expired) and recipients:
        effects._send(
            EVENT_STALE_REPORT, context, recipients, effects.platform_tenant(),
        )
        dispatched = True

    return {
        "ageing": ageing,
        "expired": expired,
        "recipients": len(recipients),
        "dispatched": dispatched,
        "context": context,
    }


# ── Reinstatement ──────────────────────────────────────────────────────────

def reinstate_school(tenant, *, actor=None):
    """Return a suspended school to PENDING, with a fresh expiry window.

    The manual path, and the only one: whether a school should ever be
    reinstated automatically is an open product question, so nothing here does
    it on its own.

    Re-stamping ``pending_since`` is the load-bearing line. Without it the next
    sweep would find a school that is pending and ninety days old and suspend
    it again within the day, which is exactly what the operator was trying to
    undo. ``expiry_warned_at`` is cleared with it, so the school is warned
    again in its new cycle rather than being skipped because it was warned in
    the old one.
    """
    from vs_tenants.models import Tenant

    if getattr(tenant, "kind", None) != Tenant.Kind.SCHOOL:
        raise OnboardingTenantNotSchool()
    if tenant.status != Tenant.Status.SUSPENDED:
        raise OnboardingNotSuspended()

    now = timezone.now()
    with transaction.atomic():
        school = effects.school_of(tenant)
        if school is not None:
            from schools.vs_schools.models import SchoolStatus

            school.status = SchoolStatus.PENDING
            school.deactivated_at = None
            # The mirror re-stamps pending_since, because this write is the
            # tenant entering PENDING (see Tenant.pending_since_for).
            school.save()
        else:
            Tenant.objects.filter(pk=tenant.pk).update(
                status=Tenant.Status.PENDING,
                deactivated_at=None,
                pending_since=now,
                # A new spell, so the school is warned again in it.
                expiry_warned_at=None,
                updated_at=now,
            )

        effects.record(
            tenant=tenant,
            action_type=AuditActionType.ONBOARDING_REINSTATED,
            entity_type="Tenant",
            entity_id=tenant.pk,
            actor=actor,
            entity_label=getattr(school, "name", None) or tenant.name,
            before_data={"status": "SUSPENDED"},
            diff_data={
                "status": {"from": "SUSPENDED", "to": "PENDING"},
                "expiry_days": ONBOARDING_EXPIRY_DAYS,
            },
        )

    tenant.refresh_from_db()
    return tenant
