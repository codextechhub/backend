"""Audit emission for the Export Centre.

Every event listed in the handoff (D10·3) goes through :func:`record`, so the module
key, entity type and severity are decided in one place rather than at twenty call
sites.

**Audit failures never block business logic**, and that is enforced here rather than
merely assumed. :func:`vs_audit.services.emit_audit_event` swallows its own errors, but
the metadata handed to it is built by *this* module, and building it reads the object
being audited - so a null relation or a renamed attribute raises before the swallowing
code is ever reached. That is exactly what stranded export runs: a tenant-scoped run
has no entity, ``run.entity.code`` raised, and a file that had already been written was
left attached to a run stuck in RUNNING. Bookkeeping must not be able to fail the work
it is describing, so :func:`record` now catches and logs instead of propagating.

Two rules the design is explicit about and this module enforces by construction:
including a sensitive field is an event in its own right, and an administrator reading
*someone else's* export activity is itself an event.
"""
from __future__ import annotations

import logging

from .constants import MODULE_KEY

logger = logging.getLogger(__name__)


# Emit one export audit event.
def record(action: str, *, actor=None, tenant=None, obj=None, label: str = "",
           severity: str = "INFO", status: str = "SUCCESS", metadata: dict | None = None):
    """Write one immutable audit event against ``obj``.

    ``obj`` may be any model instance; its class name becomes the audited entity type,
    which keeps the trail queryable by object without this module knowing the shape of
    every model it records.
    """
    try:
        from vs_audit.services import emit_audit_event

        # AuditEvent.entity_id is not nullable and is validated on save, so an
        # object-less event (an admin reading the activity list) still needs an id. "-"
        # keeps the row writable rather than letting emit_audit_event swallow a
        # validation error and lose the event entirely.
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
    except Exception:
        # Losing one audit row is bad; failing the export the row describes - and
        # stranding it half-finished - is worse. Logged at exception level so the
        # gap is visible to whoever reviews the trail rather than silently absent.
        logger.exception("Export audit event '%s' could not be written", action)
        return None


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
            # Null for a tenant-scoped dataset (admin.users, audit.events, the
            # schools list…), which has no set of books to name. Guarded the same
            # way every other reader of this relation in the app is - see
            # ExportRun.name_tokens and ExportRunSerializer.get_entity_code.
            "entity": run.entity.code if run.entity_id else None,
            "fields": [f.id for f in fields],
            "field_labels": [f.label for f in fields],
        },
    )
