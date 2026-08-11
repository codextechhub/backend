"""Releasing a **parked** spend approval: the audited, permissioned break-glass.

:mod:`vs_procurement.approval_parking` established that procurement stages never
auto-skip, so a document submitted while nobody holds the approving permission *parks*:
the stage is ACTIVE with an empty approver snapshot and the instance holds at
IN_PROGRESS. The lazy repair frees it the moment somebody is granted the permission.

That leaves one genuinely stuck case: nobody can be granted the permission in time and
the spend cannot wait. This module is the only way out of it, and it is deliberately
narrow.

What a release requires
    #. The document is **parked** - re-checked after a repair pass and again under a row
       lock. A document a single human could still decide is never overridable, which is
       the whole safety property: the override cannot be used to *bypass* an approver,
       only to unstick a document that has none.
    #. The actor holds ``procurement.approval.override``, a CRITICAL key seeded to no
       role at all. The check lives here, in the service, not only in the view, so a
       future caller cannot acquire the capability by forgetting the gate.
    #. A written reason. Stored verbatim on an append-only
       :class:`~vs_procurement.models.ApprovalOverride` row that refuses edits.

Nothing here approves a document without a human. The override *is* the human: named,
permissioned, reasoned, and recorded in three places (the procurement override row, the
workflow instance's own audit log, and finance's authoritative
:class:`~vs_finance.models.FinanceAuditLog`).

How the workflow terminates, and why that is safe
    A normal approval ends a stage and calls ``routing.advance_instance``, which walks
    the remaining stages and either activates the next one or terminates the instance
    APPROVED through ``_terminate_approved`` (firing the document handler's
    ``on_approved``). This module mirrors that path exactly rather than hand-rolling a
    shortcut: it resolves the released stage to SKIPPED, carrying
    :data:`~vs_procurement.constants.WF_OVERRIDE_SKIP_REASON` in ``skip_reason`` so the
    stage trail shows the stage that never ran, then hands control to the engine's own
    public ``advance_instance``.

    Consequently the override releases **one stage**, not the document. If a later stage
    has real approvers, the document goes back into ordinary review and the override has
    bought nothing but reachability. If a later stage parks too, it parks visibly and
    needs its own override, its own reason, and its own audit row. Both outcomes are
    strictly safer than releasing the whole ladder on one signature.

``vs_workflow`` is shared with finance approvals and payout batches and is **not**
modified. One private engine helper is reused - ``routing._skip_stage`` - because
duplicating its stage-resolution and audit write would be the more dangerous choice; it
is called with the engine's own arguments and touches only this stage instance.
"""
from __future__ import annotations

from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission
from vs_workflow.constants import AuditEventType
from vs_workflow.services import audit as audit_service
from vs_workflow.services import routing as routing_service

from . import approval_parking
from .constants import WF_APPROVAL_OVERRIDE_PERMISSION, WF_OVERRIDE_SKIP_REASON
from .exceptions import (
    ApprovalNotParkedError,
    ApprovalOverrideNotPermittedError,
    ApprovalOverrideReasonError,
)

#: Upper bound on the stored justification. Generous for a real explanation, small
#: enough that the endpoint cannot be used to write megabytes into the audit trail.
MAX_REASON_LENGTH = 2000


# --------------------------------------------------------------------------- #
# Authorisation                                                                #
# --------------------------------------------------------------------------- #

def can_override(actor_user, *, tenant, branch=None) -> bool:
    """Whether ``actor_user`` may release a parked approval in ``tenant``.

    Scoped to the *document's own* tenant rather than to whatever tenant the caller
    asserted, so the answer cannot be widened by the request. The platform-wide
    ``xvs_super_admin`` bypass applies here exactly as it does to every other RBAC gate
    (``HasRBACPermission`` short-circuits on it), so the service and the view agree.
    """
    if actor_user is None or not getattr(actor_user, "is_authenticated", False):
        return False
    if is_vision_super_admin(actor_user):
        return True
    return user_has_rbac_permission(
        actor_user, WF_APPROVAL_OVERRIDE_PERMISSION, tenant=tenant, branch=branch,
    )


# --------------------------------------------------------------------------- #
# The release                                                                  #
# --------------------------------------------------------------------------- #

def _actor_label(user) -> str:
    """A display name for the audit message, never an email address."""
    return (getattr(user, "full_name", "") or "").strip() or user.get_full_name() or "A user"


@transaction.atomic
def release_parked_document(document, *, actor_user, reason, branch=None):
    """Release ``document``'s parked approval stage without a vote, and record it.

    Returns the created :class:`~vs_procurement.models.ApprovalOverride`. The document
    is refreshed in place, so the caller sees whatever approval state the engine reached
    (APPROVED when no stage remains, still PENDING when a later stage takes over).

    Raises :class:`~vs_procurement.exceptions.ApprovalOverrideReasonError`,
    :class:`~vs_procurement.exceptions.ApprovalOverrideNotPermittedError` or
    :class:`~vs_procurement.exceptions.ApprovalNotParkedError`. Every refusal leaves
    nothing written: the whole release is one transaction.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import ApprovalOverride

    # Only surrounding whitespace is removed; the text itself is never rewritten,
    # summarised or truncated - it is the actor's own justification on the record.
    if reason is not None and not isinstance(reason, str):
        raise ApprovalOverrideReasonError("The reason must be text.")
    reason_text = (reason or "").strip()
    if not reason_text:
        raise ApprovalOverrideReasonError()
    if len(reason_text) > MAX_REASON_LENGTH:
        raise ApprovalOverrideReasonError(
            f"The reason cannot be longer than {MAX_REASON_LENGTH} characters.",
        )

    entity = document.entity
    if not can_override(actor_user, tenant=entity.tenant, branch=branch):
        raise ApprovalOverrideNotPermittedError()

    # A repair pass runs first: if anybody has since been granted the approving
    # permission, this returns None and the answer is "get them to decide it".
    stage_instance = approval_parking.parked_stage_instance(document)
    if stage_instance is not None:
        # Re-assert the same precondition under a row lock. Between the read above and
        # here a repair could have staffed the stage or a vote could have landed, and an
        # override applied on top of either would be exactly the bypass this refuses.
        stage_instance = approval_parking.lock_parked_stage(stage_instance.pk)
    if stage_instance is None:
        raise ApprovalNotParkedError(
            f"{_document_label(document)} is not waiting on an unstaffed approval stage, "
            f"so it cannot be released by override.",
        )

    instance = stage_instance.instance
    amount = int(getattr(document, getattr(document, "workflow_amount_field", ""), 0) or 0)

    # Written before the workflow moves: if the engine's terminal callbacks run (a PO
    # release, a requisition status change), the evidence of *why* already exists. A
    # failure anywhere below rolls this back with everything else.
    override = ApprovalOverride.objects.create(
        entity=entity,
        document_content_type=ContentType.objects.get_for_model(type(document)),
        document_object_id=document.pk,
        document_type=instance.document_type,
        document_number=document.document_number or "",
        workflow_instance_id=str(instance.id),
        stage_code=stage_instance.stage.code,
        stage_attempt=stage_instance.attempt,
        overridden_by=actor_user,
        amount=amount,
        reason=reason_text,
    )

    # Resolve the stage the way the engine resolves a stage nobody ran. Procurement
    # stages never auto-skip, so within PROCUREMENT_APPROVAL_TYPES this skip reason can
    # only ever mean "released by override".
    routing_service._skip_stage(
        instance, stage_instance.stage, stage_instance.attempt,
        AuditEventType.STAGE_SKIPPED_NO_APPROVER, WF_OVERRIDE_SKIP_REASON,
    )
    # Name the human on the instance's own log, next to the skip. Reusing APPROVER_ACTED
    # with an explicit marker mirrors how the parking repair reuses STAGE_ACTIVATED: the
    # engine's vocabulary has no override member and vs_workflow is not ours to extend.
    audit_service.write(
        instance, AuditEventType.APPROVER_ACTED, actor=actor_user,
        stage_instance=stage_instance,
        context={
            "action": "OVERRIDE_RELEASED",
            "override": True,
            "override_id": override.pk,
            "stage_code": stage_instance.stage.code,
            "attempt": stage_instance.attempt,
            "reason": reason_text,
        },
        message=f"{_actor_label(actor_user)} released a parked approval without review.",
    )

    # The engine's own public advance: it evaluates the remaining stages and either
    # activates the next one or terminates the instance APPROVED through the same
    # _terminate_approved -> handler.on_approved path a final vote would take.
    routing_service.advance_instance(instance, current_attempt=stage_instance.attempt)

    record(
        entity=entity, action=FinanceAuditAction.SPEND_APPROVAL_OVERRIDDEN,
        actor_user=actor_user, target=document,
        message=f"Released the parked approval for {_document_label(document)} without review.",
        reason=reason_text, stage_code=stage_instance.stage.code,
        workflow_instance_id=str(instance.id), amount=amount,
        override_id=override.pk,
    )

    # The terminal callback mutated a separately fetched copy of the document.
    document.refresh_from_db()
    return override


def _document_label(document) -> str:
    """Identify a document for an error or audit message without leaking internals."""
    return f"{type(document).__name__} {document.document_number or document.pk}"


# --------------------------------------------------------------------------- #
# Reading it back                                                              #
# --------------------------------------------------------------------------- #

def _content_type(model):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(model)


def overridden_document_ids(documents) -> set:
    """Primary keys of ``documents`` that were released by an override.

    One query for a whole page, mirroring
    :func:`~vs_procurement.approval_parking.parked_document_ids`. ``documents`` must all
    be of one procurement model; an empty page costs no query at all.
    """
    from .models import ApprovalOverride

    rows = list(documents)
    if not rows:
        return set()
    return set(
        ApprovalOverride.objects.filter(
            document_content_type=_content_type(type(rows[0])),
            document_object_id__in=[d.pk for d in rows],
        ).values_list("document_object_id", flat=True)
    )


def overridden_document_id_subquery(model):
    """Subquery of primary keys released by an override, for a list filter.

    Kept as SQL rather than a materialised id list so the filter stays bounded however
    many overrides a tenant accumulates, matching
    :func:`~vs_procurement.approval_parking.parked_document_id_subquery`.
    """
    from django.db.models import Subquery

    from .models import ApprovalOverride

    return Subquery(
        ApprovalOverride.objects
        .filter(document_content_type=_content_type(model))
        .values("document_object_id"),
    )


def is_document_overridden(document) -> bool:
    """Whether one document was approved without review."""
    from .models import ApprovalOverride

    return ApprovalOverride.objects.filter(
        document_content_type=_content_type(type(document)),
        document_object_id=document.pk,
    ).exists()
