"""The single coupling point between vs_config and the platform audit trail.

Every write in this app that must appear platform-wide calls
:func:`write_audit_log` from here, so a change to the audit service's interface
touches this file and no other service in the app.

``ConfigurationAuditEvent`` remains authoritative locally. What this module
writes is a mirror of it.
"""
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
        tenant = branch.tenant
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
        branch=branch,
        # Carried from the local row, which resolves it from the branch when the
        # caller named only that. Null here means the platform default layer.
        tenant=tenant,
    )
    return event


# Send configuration changes to the shared audit module when it is installed.
def write_audit_log(
    actor,
    action: str,
    target_type: str,
    target_id: str,
    branch=None,
    tenant=None,
) -> None:
    """Mirror a configuration change without copying its before and after values.

    The local configuration event keeps the value history. The central trail
    identifies the action and target, while a branch supplies its owning tenant
    when the caller did not name one. Importing the audit service here avoids a
    module-load cycle between configuration and audit.
    """
    try:
        from vs_audit.services import emit_audit_event

        metadata = {}
        # Preserve the config action inside the shared audit payload for downstream filters.
        metadata["config_action"] = action
        if branch is not None:
            metadata["branch_id"] = str(branch.id)
        # A branch carries its tenant on the row itself, so a caller that named
        # only the branch has still told us the customer.
        if tenant is None and branch is not None:
            tenant = branch.tenant

        emit_audit_event(
            module_key="CONFIG",
            action_type="CONFIG_CHANGED",
            entity_type=target_type,
            entity_id=str(target_id),
            actor_user=actor,
            tenant=tenant,
            metadata=metadata,
        )
    except ImportError:
        # vs_audit not yet available (e.g. during initial migrations or tests).
        # Log a warning but do not raise - audit failures must never block
        # the primary config/flag operation.
        logger.warning(
            "vs_audit not available. "
            "Platform audit log entry skipped for action='%s' target='%s:%s'.",
            action,
            target_type,
            target_id,
        )
