"""FR-007, FR-008 and FR-009: asking to go live, deciding, and going live.

Activation is here and nowhere else. Nothing in the platform sets
``School.status`` to ACTIVE except :func:`_write_school_live`, and the tenant is
never written beside it: ``School.save()`` mirrors name, status and the
activation timestamps onto the tenant inside its own transaction, so writing
both would mean two sources of truth for the same fact.

The failure path is the part worth reading twice. Everything the activation
touched rolls back, so a half-live school is impossible; the request row is then
written to FAILED with a correlation reference *outside* the rolled-back
transaction, and readiness is put back to READY so the school can try again once
whatever failed is fixed.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditSeverity, AuditStatus

from ..constants import (
    EVENT_ACTIVATED,
    EVENT_GO_LIVE_REVIEWED,
    GoLiveStatus,
    ReadinessState,
)
from ..exceptions import (
    AcknowledgementRequired,
    GoLiveActivationFailed,
    GoLiveAlreadyPending,
    GoLiveNotPending,
    OnboardingAlreadyLive,
    OnboardingNotProvisioned,
    OnboardingNotReady,
    RejectionReasonRequired,
)
from ..models import GoLiveRequest, OnboardingProgress
from . import effects
from .readiness import ReadinessService, outstanding_required_keys
from .scoping import rows_for

logger = logging.getLogger("vs_onboarding")


def _progress_or_404(tenant) -> OnboardingProgress:
    progress = OnboardingProgress.all_objects.filter(tenant=tenant).first()
    if progress is None:
        raise OnboardingNotProvisioned()
    return progress


def _request_or_404(tenant, pk) -> GoLiveRequest:
    """Fetch one request, scoped to ``tenant``.

    Scoping here is what makes another tenant's request answer 404 rather than
    403: a reviewer who may act on school A must not be able to learn that
    request 41 exists at school B by trying it.
    """
    request = rows_for(GoLiveRequest, tenant).filter(pk=pk).first()
    if request is None:
        from rest_framework.exceptions import NotFound

        raise NotFound("No go-live request matches this id.")
    return request


# ── FR-007  Submit ─────────────────────────────────────────────────────────

def submit_go_live(
    tenant, *, actor=None, preferred_go_live_at, note="", acknowledged=False,
) -> GoLiveRequest:
    """Record a school's request to be taken live.

    Readiness is re-evaluated here rather than trusted from the stored column,
    because the column was last written when a task moved and the world may
    have changed since. The evaluation is the same single authority the control
    room reads, so the school cannot be refused by a gate its own screen said
    was open.
    """
    progress = _progress_or_404(tenant)
    if progress.readiness_state == ReadinessState.LIVE:
        raise OnboardingAlreadyLive()
    if not acknowledged:
        raise AcknowledgementRequired()

    if rows_for(GoLiveRequest, tenant).filter(
        status=GoLiveStatus.PENDING,
    ).exists():
        raise GoLiveAlreadyPending()

    progress = ReadinessService.evaluate(tenant, actor=actor)
    if progress.readiness_state != ReadinessState.READY:
        raise OnboardingNotReady(outstanding=outstanding_required_keys(tenant))

    try:
        with transaction.atomic():
            request = GoLiveRequest.all_objects.create(
                tenant=tenant,
                requested_by=actor,
                preferred_go_live_at=preferred_go_live_at,
                note=note or "",
                acknowledged=True,
                status=GoLiveStatus.PENDING,
            )
            OnboardingProgress.all_objects.filter(pk=progress.pk).update(
                readiness_state=ReadinessState.PENDING_APPROVAL,
                updated_at=timezone.now(),
            )
    except IntegrityError:
        # The partial unique constraint is the guarantee; the pre-check above is
        # only the friendly path in front of it, and it loses the race it exists
        # to prevent. Both answer the same 409.
        raise GoLiveAlreadyPending()

    effects.record(
        tenant=tenant,
        action_type=AuditActionType.GO_LIVE_REQUESTED,
        entity_type="GoLiveRequest",
        entity_id=request.pk,
        actor=actor,
        entity_label=effects.school_context(tenant)["school_name"],
        diff_data={
            "status": {"from": "", "to": GoLiveStatus.PENDING.value},
            "preferred_go_live_at": str(preferred_go_live_at),
        },
    )
    return request


# ── FR-008  Reject ─────────────────────────────────────────────────────────

def reject_go_live(tenant, pk, *, actor=None, rejection_reason="") -> GoLiveRequest:
    """Decline a pending request, with a reason the school will actually read."""
    reason = (rejection_reason or "").strip()
    with transaction.atomic():
        request = _request_or_404(tenant, pk)
        if request.status != GoLiveStatus.PENDING:
            raise GoLiveNotPending()
        if not reason:
            raise RejectionReasonRequired()

        now = timezone.now()
        request.status = GoLiveStatus.REJECTED
        request.reviewed_by = actor
        request.reviewed_at = now
        request.rejection_reason = reason
        request.save(update_fields=[
            "status", "reviewed_by", "reviewed_at", "rejection_reason",
            "updated_at",
        ])

        # Back to READY, not to NOT_READY: the school did everything asked of
        # it and the platform said no for a reason of its own. Its tasks are
        # untouched, so it may correct the reason and submit again.
        OnboardingProgress.all_objects.filter(tenant=tenant).update(
            readiness_state=ReadinessState.READY,
            updated_at=now,
        )

        context = effects.school_context(tenant)
        context.update({
            "decision": "rejected",
            "reviewed_by_name": effects.actor_name(actor),
            "reviewed_at": now.isoformat(),
            "rejection_reason": reason,
        })
        effects.record(
            tenant=tenant,
            action_type=AuditActionType.GO_LIVE_REJECTED,
            entity_type="GoLiveRequest",
            entity_id=request.pk,
            actor=actor,
            entity_label=context["school_name"],
            before_data={"status": GoLiveStatus.PENDING.value},
            diff_data={
                "status": {
                    "from": GoLiveStatus.PENDING.value,
                    "to": GoLiveStatus.REJECTED.value,
                },
                "rejection_reason": reason,
            },
            event_key=EVENT_GO_LIVE_REVIEWED,
            context=context,
            recipients=effects.notification_recipients(tenant),
        )
    return request


# ── FR-009  Activate ───────────────────────────────────────────────────────

def _write_school_live(school, now):
    """Take the school live and let it mirror onto its tenant.

    Deliberately a named function rather than three inline lines: it is the
    single statement that makes a school live, so it is also the single place a
    test can force to fail in order to prove the rollback.
    """
    from schools.vs_schools.models import SchoolStatus

    school.status = SchoolStatus.ACTIVE
    school.activated_at = now
    school.save()
    return school


# ── FR-008  Approve, and FR-009 immediately after it ───────────────────────

def approve_go_live(tenant, pk, *, actor=None) -> GoLiveRequest:
    """Approve a pending request and take the school live in the same call.

    Approval and activation share one transaction on purpose. A request marked
    APPROVED beside a school that is still pending is a state nobody can act
    on, so either both happen or neither does and the request records the
    failure.
    """
    request = _request_or_404(tenant, pk)
    if request.status != GoLiveStatus.PENDING:
        raise GoLiveNotPending()

    now = timezone.now()
    school = effects.school_of(tenant)
    already_live = False
    failure = None

    try:
        with transaction.atomic():
            locked = (
                GoLiveRequest.all_objects
                .select_for_update()
                .filter(pk=request.pk)
                .first()
            )
            if locked is None or locked.status != GoLiveStatus.PENDING:
                raise GoLiveNotPending()

            progress = (
                OnboardingProgress.all_objects
                .select_for_update()
                .filter(tenant=tenant)
                .first()
            )
            if progress is None:
                raise OnboardingNotProvisioned()

            already_live = progress.readiness_state == ReadinessState.LIVE
            if not already_live:
                # Idempotence lives in this branch: a tenant already LIVE is
                # not re-activated and ``go_live_at`` is not rewritten, because
                # the moment a school went live is a fact about the past.
                _write_school_live(school, now)
                progress.readiness_state = ReadinessState.LIVE
                progress.go_live_at = now
                progress.last_validation_at = now
                progress.save(update_fields=[
                    "readiness_state", "go_live_at", "last_validation_at",
                    "updated_at",
                ])

            locked.status = GoLiveStatus.ACTIVATED
            locked.reviewed_by = actor
            locked.reviewed_at = now
            locked.save(update_fields=[
                "status", "reviewed_by", "reviewed_at", "updated_at",
            ])
            request = locked
    except (GoLiveNotPending, OnboardingNotProvisioned):
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a domain refusal below
        failure = exc

    if failure is not None:
        return _record_activation_failure(tenant, request, actor, failure)

    context = effects.school_context(tenant)
    context.update({
        "decision": "approved",
        "reviewed_by_name": effects.actor_name(actor),
        "reviewed_at": now.isoformat(),
        "rejection_reason": "",
    })
    # Two events, in the order they happened: the decision, then what the
    # decision did. Both are queued after commit, and the audit row of each is
    # written before its notification.
    effects.record(
        tenant=tenant,
        action_type=AuditActionType.GO_LIVE_APPROVED,
        entity_type="GoLiveRequest",
        entity_id=request.pk,
        actor=actor,
        entity_label=context["school_name"],
        before_data={"status": GoLiveStatus.PENDING.value},
        diff_data={
            "status": {
                "from": GoLiveStatus.PENDING.value,
                "to": GoLiveStatus.APPROVED.value,
            },
        },
        event_key=EVENT_GO_LIVE_REVIEWED,
        context=context,
        recipients=effects.notification_recipients(tenant),
    )

    live_progress = OnboardingProgress.all_objects.filter(tenant=tenant).first()
    activated_context = effects.school_context(tenant)
    activated_context["go_live_at"] = (
        live_progress.go_live_at.isoformat()
        if live_progress and live_progress.go_live_at else ""
    )
    effects.record(
        tenant=tenant,
        action_type=AuditActionType.GO_LIVE_ACTIVATED,
        entity_type="GoLiveRequest",
        entity_id=request.pk,
        actor=actor,
        entity_label=activated_context["school_name"],
        diff_data={
            "status": {
                "from": GoLiveStatus.APPROVED.value,
                "to": GoLiveStatus.ACTIVATED.value,
            },
            "already_live": already_live,
        },
        event_key=EVENT_ACTIVATED,
        context=activated_context,
        recipients=effects.notification_recipients(tenant),
    )
    return request


def _record_activation_failure(tenant, request, actor, exc):
    """Write down a failed activation, after its transaction has rolled back.

    Nothing here runs inside the block that failed: that block is gone, along
    with every row it wrote. These writes are new, and they are the only trace
    the school and the platform have of the attempt.
    """
    reference = uuid4().hex
    now = timezone.now()
    logger.exception(
        "Go-live activation failed for tenant %s (reference %s).",
        getattr(tenant, "slug", tenant), reference, exc_info=exc,
    )

    GoLiveRequest.all_objects.filter(pk=request.pk).update(
        status=GoLiveStatus.FAILED,
        failure_reference=reference,
        reviewed_by=actor,
        reviewed_at=now,
        updated_at=now,
    )
    # The school cleared the gate; activation is what broke. Leaving it at
    # PENDING_APPROVAL would strand it waiting on a decision that was already
    # taken, so it goes back to READY and may be submitted again.
    OnboardingProgress.all_objects.filter(tenant=tenant).update(
        readiness_state=ReadinessState.READY,
        updated_at=now,
    )

    effects.record(
        tenant=tenant,
        action_type=AuditActionType.GO_LIVE_FAILED,
        entity_type="GoLiveRequest",
        entity_id=request.pk,
        actor=actor,
        entity_label=effects.school_context(tenant)["school_name"],
        severity=AuditSeverity.CRITICAL,
        status=AuditStatus.FAILED,
        diff_data={
            "status": {
                "from": GoLiveStatus.PENDING.value,
                "to": GoLiveStatus.FAILED.value,
            },
        },
        metadata={"failure_reference": reference, "error": str(exc)[:500]},
    )
    raise GoLiveActivationFailed(failure_reference=reference)
