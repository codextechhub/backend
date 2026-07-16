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


# Persist the local audit event first, then mirror it into the platform audit stream.
def record_configuration_event(
    *, action, target, actor, tenant=None, branch=None, before=None, after=None,
    reason="", metadata=None,
):
    """Write the authoritative immutable local event and mirror it centrally."""
    from ..models import ConfigurationAuditEvent
    from vs_tenants.context import add_proxy_audit_metadata, resolve_audit_identity

    actor, effective_user, proxy_session = resolve_audit_identity(actor)
    metadata = add_proxy_audit_metadata(metadata, effective_user, proxy_session)

    # Branch-scoped audit rows also carry tenant for tenant filtering.
    if branch is not None and tenant is None:
        tenant = branch.school.tenant
    # The local row is authoritative because it is committed with the config change.
    event = ConfigurationAuditEvent(
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        actor=actor,
        tenant=tenant,
        branch=branch,
        before_data=before or {},
        after_data=after or {},
        reason=reason,
        metadata=metadata or {},
    )
    event.save()
    # Central audit mirroring is best-effort and must not block config changes.
    write_audit_log(
        actor=actor,
        action=action,
        target_type=event.target_type,
        target_id=event.target_id,
        detail={"before": before or {}, "after": after or {}, **(metadata or {})},
        branch=branch,
    )
    return event


# Send configuration changes to the shared audit module when it is installed.
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
        # Preserve the config action inside the shared audit payload for downstream filters.
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
