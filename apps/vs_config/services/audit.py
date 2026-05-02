# vs_config/services/audit.py
#
# Thin wrapper around Module 5's AuditLog service.
# All writes in vs_config that need to appear in the platform-wide audit trail
# call write_audit_log() from here.
#
# This keeps the coupling point in one place. If Module 5's interface changes,
# only this file needs updating — not every service in vs_config.
#
# Note: vs_config also writes its own ConfigurationChangeLog entries (via
# ConfigurationService and FlagService). That is the module-local history.
# This file covers the platform-level audit trail that Module 5 owns.

import logging

logger = logging.getLogger(__name__)


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
    target_type : str         — the type of object being acted on, e.g. 'ConfigurationKey'
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
        if branch is not None:
            metadata["branch_id"] = str(branch.id)

        emit_audit_event(
            module_key="config",
            action_type=action,
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


# ---------------------------------------------------------------------------
# Predefined action labels for vs_config audit events
# These align with the module.resource.action pattern used across the platform.
# ---------------------------------------------------------------------------
class ConfigAuditActions:
    # Global config key actions
    KEY_CREATED  = "config.key.created"
    KEY_UPDATED  = "config.key.updated"
    KEY_DELETED  = "config.key.deleted"    # soft delete
    KEY_RESTORED = "config.key.restored"

    # Feature flag actions
    FLAG_ENABLED  = "config.flag.enabled"
    FLAG_DISABLED = "config.flag.disabled"

    # Branch override actions
    OVERRIDE_SET = "config.override.set"

    # Export
    CONFIG_EXPORTED = "config.export.downloaded"
