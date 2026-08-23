"""Audit and notification, dispatched after the business state is committed.

Two rules shape this module and both are FR-011.

**After commit.** Nothing here runs inside the transaction that changed the
data. An audit row for a transition that then rolled back is a lie, and an
email about it is a worse one, so every effect is queued on
``transaction.on_commit``.

**Audit first, then notify.** ``emit_audit_event`` never raises, which is
exactly why the closed action-type vocabulary has to be registered before
anything emits: an unregistered value is swallowed and the trail is lost with
nothing to show for it. Notification, by contrast, *does* raise for an unknown
or inactive event key, so it is wrapped: the business state is already
committed and a template problem must not turn a completed activation into a
500 with no way back.
"""
from __future__ import annotations

import logging

from django.db import transaction

from vs_audit.models import AuditModuleKey, AuditSeverity, AuditStatus

from ..constants import PERM_GO_LIVE_APPROVE, PERM_PROGRESS_VIEW

logger = logging.getLogger("vs_onboarding")


def school_of(tenant):
    """The school profile for ``tenant``, or None for a tenant without one."""
    return getattr(tenant, "school_profile", None)


def school_context(tenant) -> dict:
    """The two school keys every seeded onboarding template renders."""
    school = school_of(tenant)
    return {
        "school_name": getattr(school, "name", None) or getattr(tenant, "name", ""),
        "school_slug": getattr(school, "slug", None) or getattr(tenant, "slug", ""),
    }


def actor_name(actor) -> str:
    if actor is None:
        return "System"
    return (
        getattr(actor, "full_name", None)
        or getattr(actor, "get_full_name", lambda: "")()
        or getattr(actor, "email", "")
        or "Unknown user"
    )


def notification_recipients(tenant):
    """Who hears about onboarding for this tenant.

    Resolved from the same permission key that gates the control room, so the
    audience and the gate cannot drift apart. ``branch=None`` is deliberate and
    is not the same as naming no branch: onboarding belongs to the tenant as a
    whole, so a person pinned to one site is not an audience for it.
    """
    from vs_rbac.evaluator import resolve_users_with_permission

    return list(
        resolve_users_with_permission(tenant, None, PERM_PROGRESS_VIEW)
    )


def platform_tenant():
    """The Codex platform tenant, or None where it has not been seeded.

    Everything platform-wide in this module is scoped to it: an operator's
    roles, and therefore an operator audience, live on the platform tenant and
    nowhere else.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.filter(
        slug="codex", kind=Tenant.Kind.PLATFORM,
    ).first()


def operator_recipients():
    """The platform staff who hear about schools the platform has to chase.

    Resolved from ``onboarding.go_live.approve``, which is the key that already
    means "onboarding outcomes for other people's schools are my job": the
    seeder grants it to the two platform roles and to no school role, so the
    audience cannot quietly acquire a school admin. Picked rather than invented
    for that reason - an audience defined by a new key of its own would start
    life held by nobody.
    """
    from vs_rbac.evaluator import resolve_users_with_permission

    tenant = platform_tenant()
    if tenant is None:
        return []
    return list(
        resolve_users_with_permission(tenant, None, PERM_GO_LIVE_APPROVE)
    )


def _send(event_key: str, context: dict, recipients: list, tenant) -> None:
    """Dispatch one notification, swallowing anything it raises.

    The caller's transaction has already committed by the time this runs. There
    is nothing to roll back and nothing useful to tell the client, so a failure
    is logged loudly enough to be found and the request still succeeds.
    """
    if not recipients:
        return
    from vs_notifications.services.dispatch import NotificationService

    try:
        NotificationService.send(
            event_key=event_key,
            context=context,
            recipients=recipients,
            tenant=tenant,
        )
    except Exception:
        logger.exception(
            "Onboarding notification '%s' could not be dispatched for tenant %s.",
            event_key, getattr(tenant, "slug", tenant),
        )


def record(
    *,
    tenant,
    action_type: str,
    entity_type: str,
    entity_id,
    actor=None,
    entity_label: str = "",
    before_data: dict | None = None,
    diff_data: dict | None = None,
    metadata: dict | None = None,
    # Both vocabularies are closed and validated on save, and a value outside
    # them makes ``emit_audit_event`` swallow the whole event. Defaulted from
    # the enums rather than from string literals so a typo cannot silently cost
    # us the trail.
    severity: str = AuditSeverity.INFO,
    status: str = AuditStatus.SUCCESS,
    event_key: str | None = None,
    context: dict | None = None,
    recipients: list | None = None,
):
    """Queue one audit event, and optionally one notification, for after commit.

    Both are queued in a single ``on_commit`` callback so their order is fixed:
    the trail is written before anybody is told, never after.
    """
    from vs_audit.services import emit_audit_event

    def _dispatch():
        emit_audit_event(
            module_key=AuditModuleKey.ONBOARDING,
            action_type=action_type,
            actor_user=actor,
            tenant=tenant,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_label=entity_label,
            severity=severity,
            status=status,
            before_data=before_data,
            diff_data=diff_data,
            metadata=metadata,
        )
        if event_key:
            _send(event_key, context or {}, recipients or [], tenant)

    transaction.on_commit(_dispatch)
