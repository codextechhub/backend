# vs_config/services/audit.py
#
# Thin wrapper around Module 5's AuditLog service.
# All writes in vs_config that need to appear in the platform-wide audit trail
# call write_audit_log() from here.
#
# This keeps the coupling point in one place. If Module 5's interface changes,
# only this file needs updating — not every service in vs_config.
#
# ConfigurationAuditEvent is authoritative locally; this module also mirrors
# events to the platform audit trail.

import logging

logger = logging.getLogger(__name__)


def record_configuration_event(
    *, action, target, actor, school=None, branch=None, before=None, after=None,
    reason="", metadata=None,
):
    """Write the authoritative immutable local event and mirror it centrally."""
    from ..models import ConfigurationAuditEvent

    if branch is not None and school is None:
        school = branch.school
    event = ConfigurationAuditEvent(
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        actor=actor,
        school=school,
        branch=branch,
        before_data=before or {},
        after_data=after or {},
        reason=reason,
        metadata=metadata or {},
    )
    event.save()
    write_audit_log(
        actor=actor,
        action=action,
        target_type=event.target_type,
        target_id=event.target_id,
        detail={"before": before or {}, "after": after or {}, **(metadata or {})},
        branch=branch,
    )
    return event


def write_audit_log(
    actor,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict = None,
    branch=None,
) -> None:
    """
    Dispatch a platform-level audit log entry to Module 5.

    Parameters
    ----------
    actor       : UserAccount — the user performing the action
    action      : str         — human-readable action label, e.g. 'config.key.created'
    target_type : str         — the type of object being acted on
    target_id   : str         — string representation of the target's primary key
    detail      : dict        — optional payload with before/after values or extra context
    branch      : Branch      — optional; set for branch-scoped changes

    Design note:
    The import of vs_audit's emit_audit_event is inside the function body to avoid
    circular imports at module load time. vs_config is a low-level module that
    vs_audit may itself depend on for config lookups.
    """
    try:
        from vs_audit.services import emit_audit_event

        metadata = dict(detail or {})
        metadata["config_action"] = action
        if branch is not None:
            metadata["branch_id"] = str(branch.id)

        emit_audit_event(
            module_key="CONFIG",
            action_type="CONFIG_CHANGED",
            entity_type=target_type,
            entity_id=str(target_id),
            actor_user=actor,
            metadata=metadata,
        )
    except ImportError:
        # vs_audit not yet available (e.g. during initial migrations or tests).
        # Log a warning but do not raise — audit failures must never block
        # the primary config/flag operation.
        logger.warning(
            "vs_audit not available. "
            "Platform audit log entry skipped for action='%s' target='%s:%s'.",
            action,
            target_type,
            target_id,
        )
