"""Finance audit service.  # Authoritative finance audit trail plus central mirror.

The module keeps its **own** authoritative audit trail (the append-only
:class:`~vs_finance.models.FinanceAuditLog`) rather than relying on the central
``vs_audit`` system, for two reasons finance can't compromise on:  # Finance log is system-of-record.

* the audit row is written **transactionally** with the action (a posting can't
  commit without it), and a failure to write it is **not** swallowed; whereas  # Must commit with the action.
* central ``vs_audit`` is best-effort by contract (it never raises, so it may drop
  events) — perfect as a platform-wide *mirror*, wrong as the system of record.  # Mirror only, never source of truth.

:func:`record` writes the authoritative row and then mirrors a copy to ``vs_audit``
best-effort, so the global activity view stays complete without becoming load-bearing.  # Keep the mirror non-blocking.
"""
from __future__ import annotations

from django.db import transaction

from .constants import FinanceAuditStatus


# Support the mirror to central workflow.
def _mirror_to_central(*, action, actor_user, entity, target_type, target_id,
                       document_number, status, message, metadata):
    """Best-effort copy into central vs_audit. Never raises — the in-app log is truth."""
    try:  # Mirroring must never block the primary finance write.
        from vs_audit.services import emit_audit_event
        from vs_audit.models import AuditModuleKey, AuditActionType

        emit_audit_event(  # Mirror the finance event into the platform audit log.
            module_key=AuditModuleKey.FINANCE,
            action_type=AuditActionType.FINANCIAL_TRANSACTION,
            entity_type=f"vs_finance.{target_type}" if target_type else "vs_finance",
            entity_id=str(target_id or ""),
            entity_label=document_number or str(target_id or ""),
            actor_user=actor_user,
            status="SUCCESS" if status == FinanceAuditStatus.SUCCESS else "FAILED",
            severity="INFO" if status == FinanceAuditStatus.SUCCESS else "WARNING",
            summary=message or f"Finance: {action}",
            metadata={"finance_action": str(action), **(metadata or {})},
        )
    except Exception:  # pragma: no cover - mirror is best-effort
        pass  # Swallow mirror failures so the authoritative finance log stays intact.


def record(*, entity, action, actor_user=None, target=None, target_type="",
           target_id="", document_number="", status=FinanceAuditStatus.SUCCESS,
           message="", before=None, after=None, mirror=True, **metadata):
    """Write an authoritative :class:`FinanceAuditLog` row (and mirror to vs_audit).

    Call this **inside** the same transaction as a successful action so the audit row
    shares its commit. For a *rejected* action — which rolls its transaction back —
    call it from outside that rolled-back atomic (see ``_record_rejection`` in
    :mod:`vs_finance.posting`) so the rejection still durably records.

    ``target`` may be passed instead of ``target_type``/``target_id`` for convenience;
    its class name and pk are used. Returns the created row.
    """
    from .models import FinanceAuditLog
    from vs_tenants.context import add_proxy_audit_metadata, resolve_audit_identity

    actor_user, effective_user, proxy_session = resolve_audit_identity(actor_user)
    metadata = add_proxy_audit_metadata(metadata, effective_user, proxy_session)

    if target is not None:  # Allow callers to pass a model instance instead of manual identifiers.
        target_type = target_type or type(target).__name__  # Derive the target type from the instance.
        target_id = target_id or str(target.pk)  # Derive the target id from the instance pk.
        document_number = document_number or getattr(target, "document_number", "") or ""  # Pull document number when available.

    log = FinanceAuditLog.objects.create(
        entity=entity,
        actor=actor_user,
        action=action,
        status=status,
        target_type=target_type,
        target_id=str(target_id),
        document_number=document_number,
        message=message,
        before=before or {},
        after=after or {},
        metadata=metadata or {},
    )

    if mirror:  # Optionally mirror the event into the platform-wide audit log.
        _mirror_to_central(  # Copy the finance event into the central audit system.
            action=action, actor_user=actor_user, entity=entity,
            target_type=target_type, target_id=target_id,
            document_number=document_number, status=status,
            message=message, metadata=metadata,
        )
    return log  # Return the authoritative finance audit row.


# Handle the record rejection workflow.
def record_rejection(*, entity, action, exc, actor_user=None, target=None,
                     target_type="", target_id="", document_number="", **metadata):
    """Durably record a *failed* action in its own committed transaction.

    The action's own transaction rolled back (that's what a rejection means), so the
    audit row must be written in a fresh atomic block to survive. Best-effort itself:
    a failure to log the rejection must not mask the original business error the
    caller is about to re-raise.
    """
    error_code = getattr(exc, "error_code", type(exc).__name__)  # Preserve a stable error code when possible.
    try:  # Rejection logging must not mask the original exception.
        with transaction.atomic():
            record(  # Persist the failed finance action.
                entity=entity, action=action, actor_user=actor_user,
                target=target, target_type=target_type, target_id=target_id,
                document_number=document_number,
                status=FinanceAuditStatus.FAILED,
                message=str(exc)[:255],
                error_code=error_code,
                **metadata,
            )
    except Exception:  # pragma: no cover - never mask the real error
        pass  # Swallow logging failures so the original business error still surfaces.
