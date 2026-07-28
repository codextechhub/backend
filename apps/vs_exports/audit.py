"""Audit emission for the Export Centre.

Every event listed in the handoff (D10·3) goes through :func:`record`, so the module
key, entity type and severity are decided in one place rather than at twenty call
sites. Audit failures never block business logic — :func:`vs_audit.services.emit_audit_event`
already swallows its own errors, and nothing here adds a path that can raise.

Two rules the design is explicit about and this module enforces by construction:
including a sensitive field is an event in its own right, and an administrator reading
*someone else's* export activity is itself an event.
"""
from __future__ import annotations

from .constants import MODULE_KEY


# Emit one export audit event.
def record(action: str, *, actor=None, tenant=None, obj=None, label: str = "",
           severity: str = "INFO", status: str = "SUCCESS", metadata: dict | None = None):
    """Write one immutable audit event against ``obj``.

    ``obj`` may be any model instance; its class name becomes the audited entity type,
    which keeps the trail queryable by object without this module knowing the shape of
    every model it records.
    """
    from vs_audit.services import emit_audit_event

    # AuditEvent.entity_id is not nullable and is validated on save, so an object-less
    # event (an admin reading the activity list) still needs an id. "-" keeps the row
    # writable rather than letting emit_audit_event swallow a validation error and
    # lose the event entirely.
    return emit_audit_event(
        module_key=MODULE_KEY,
        action_type=action,
        entity_type=type(obj).__name__ if obj is not None else "ExportCentre",
        entity_id=str(getattr(obj, "pk", "") or "-"),
        entity_label=(label or str(obj or ""))[:255],
        actor_user=actor,
        tenant=tenant,
        severity=severity,
        status=status,
        metadata=metadata or {},
    )


# Record that a run included restricted fields.
def record_sensitive_fields(run, fields, *, actor=None):
    """One event naming the restricted columns, recorded against the actor.

    This is what makes "did anything sensitive leave the building last month" a
    one-query question, so it carries the field ids rather than only a count.
    """
    if not fields:
        return None
    from .constants import AuditAction

    return record(
        AuditAction.SENSITIVE_FIELD_INCLUDED,
        actor=actor, tenant=run.tenant, obj=run, label=run.reference,
        severity="WARNING",
        metadata={
            "dataset": run.frozen_config.get("dataset_key"),
            "entity": run.entity.code,
            "fields": [f.id for f in fields],
            "field_labels": [f.label for f in fields],
        },
    )
