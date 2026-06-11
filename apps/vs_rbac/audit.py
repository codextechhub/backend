"""
Durable RBAC audit (B21) — hybrid pattern borrowed from vs_finance.

``record_rbac_audit`` writes the authoritative, append-only
:class:`~vs_rbac.models.RBACAuditLog` row FIRST (a failure raises and rolls
back the surrounding action — never silently dropped), then mirrors the event
best-effort to the central ``vs_audit`` trail for the platform-wide activity
view.

The signature is a superset of ``vs_audit.services.emit_audit_event`` so the
existing call sites in ``signals.py`` / ``services.py`` convert by swapping
the import alone.
"""
from __future__ import annotations

from .models import RBACAuditLog


def record_rbac_audit(
    *,
    action_type: str,
    entity_type: str,
    entity_id: str,
    module_key: str = "RBAC",
    actor_user=None,
    entity_label: str = "",
    severity: str = "INFO",
    status: str = "SUCCESS",
    summary: str = "",
    before_data: dict | None = None,
    diff_data: dict | None = None,
    metadata: dict | None = None,
):
    """Write the durable RBAC audit row, then mirror to central audit.

    Returns the created :class:`RBACAuditLog`. Raises on failure of the
    durable write (by design — the caller's transaction must roll back with
    it). The central mirror never raises.
    """
    school_id = str((metadata or {}).get("school_id", "") or "")

    log = RBACAuditLog.objects.create(
        action_type=str(action_type),
        severity=str(severity),
        status=str(status),
        actor=actor_user if getattr(actor_user, "pk", None) else None,
        school_id=school_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_label=entity_label or "",
        summary=summary or "",
        before_data=before_data,
        diff_data=diff_data,
        metadata=metadata,
    )

    # Best-effort mirror. emit_audit_event swallows its own failures, but the
    # durable row is already committed-with-the-action — nothing the mirror
    # does may break the caller, so guard the boundary here too.
    try:
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key=module_key,
            action_type=action_type,
            actor_user=actor_user,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_label=entity_label,
            severity=severity,
            status=status,
            summary=summary,
            before_data=before_data,
            diff_data=diff_data,
            metadata=metadata,
        )
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger("vs_rbac.audit").warning(
            "Central audit mirror failed for %s %s:%s",
            action_type, entity_type, entity_id, exc_info=True,
        )

    return log
