"""Finance audit service.  # Authoritative finance audit trail plus central mirror.

The module keeps its **own** authoritative audit trail (the append-only
:class:`~vs_finance.models.FinanceAuditLog`) rather than relying on the central
``vs_audit`` system, for two reasons finance can't compromise on:  # Finance log is system-of-record.

* the audit row is written **transactionally** with the action (a posting can't
  commit without it), and a failure to write it is **not** swallowed; whereas  # Must commit with the action.
* central ``vs_audit`` is best-effort by contract (it never raises, so it may drop
  events) - perfect as a platform-wide *mirror*, wrong as the system of record.  # Mirror only, never source of truth.

:func:`record` writes the authoritative row and then mirrors a copy to ``vs_audit``
best-effort, so the global activity view stays complete without becoming load-bearing.  # Keep the mirror non-blocking.
"""
from __future__ import annotations

from django.db import transaction

from .constants import FinanceAuditStatus


# Support the mirror to central workflow.
def _mirror_to_central(*, action, actor_user, entity, target_type, target_id,
                       document_number, status, message, metadata, branch_id=None):
    """Best-effort copy into central vs_audit. Never raises - the in-app log is truth.

    The copy carries the entry's branch, so the platform trail narrows a
    branch-bound reader exactly as the finance trail does.
    """
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
            # A set of books has exactly one canonical owner (LedgerEntity.tenant
            # is not nullable), so the mirror can say which customer the posting
            # belongs to instead of leaving the platform trail to guess. Guarded
            # because posting code may mirror without a loaded entity.
            tenant=getattr(entity, "tenant", None),
            status="SUCCESS" if status == FinanceAuditStatus.SUCCESS else "FAILED",
            severity="INFO" if status == FinanceAuditStatus.SUCCESS else "WARNING",
            summary=message or f"Finance: {action}",
            metadata={"finance_action": str(action), **(metadata or {})},
            branch=branch_id,
        )
    except Exception:  # pragma: no cover - mirror is best-effort
        pass  # Swallow mirror failures so the authoritative finance log stays intact.


#: :func:`record`'s default: the entry takes the branch of its ``target``.
FROM_TARGET = object()


def audit_value(value):
    """``value`` as an audit row's ``before`` or ``after`` keeps it: JSON, and readable later.

    A related row is kept as its primary key, a date as ISO text and a decimal as
    text, so a correction's trail can be compared field by field after the rows
    it named have changed.
    """
    import decimal

    from django.db import models

    if isinstance(value, models.Model):
        return value.pk
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def entry_branch_id(target=None, branch=FROM_TARGET):
    """The branch an entry about ``target`` is filed under, as an id or ``None``.

    ``branch`` (a Branch, its id, or ``None``) wins when given. Otherwise the
    target's own ``branch`` column answers, and a target without one (a setting,
    a fiscal year, a vendor contract) belongs to the whole tenant.
    """
    if branch is not FROM_TARGET:
        return getattr(branch, "pk", branch)
    return getattr(target, "branch_id", None) if target is not None else None


def record(*, entity, action, actor_user=None, target=None, target_type="",
           target_id="", document_number="", status=FinanceAuditStatus.SUCCESS,
           message="", before=None, after=None, mirror=True, branch=FROM_TARGET,
           **metadata):
    """Write an authoritative :class:`FinanceAuditLog` row (and mirror to vs_audit).

    Call this **inside** the same transaction as a successful action so the audit row
    shares its commit. For a *rejected* action - which rolls its transaction back -
    call it from outside that rolled-back atomic (see ``_record_rejection`` in
    :mod:`vs_finance.posting`) so the rejection still durably records.

    ``target`` may be passed instead of ``target_type``/``target_id`` for convenience;
    its class name and pk are used. Returns the created row.

    The entry's branch is the document's, never the acting person's
    (:func:`entry_branch_id`): a target carrying a ``branch`` column files the
    entry under it. ``branch`` is passed only where the document is not the
    target itself: a bank statement correction is the bank account's, a stock
    movement its store's, and one branch share of a central payroll run or of the
    tenant's tax return is that share's branch. The branch decides who reads the
    entry (:class:`~vs_finance.models.FinanceAuditLog`), so an entry about a
    document booked per branch is written once per share, with that share's
    figures, never once with the whole-school total.

    Under a proxy, an ``actor_user`` that is either side of the proxy is recorded
    as the real person, and the impersonated person lands in ``effective_user``,
    so the trail can say "done by <real> for <impersonated>".
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
        effective_user=effective_user if proxy_session is not None else None,
        action=action,
        status=status,
        target_type=target_type,
        target_id=str(target_id),
        document_number=document_number,
        message=message,
        before=before or {},
        after=after or {},
        metadata=metadata or {},
        branch_id=entry_branch_id(target, branch),
    )

    if mirror:  # Optionally mirror the event into the platform-wide audit log.
        _mirror_to_central(  # Copy the finance event into the central audit system.
            action=action, actor_user=actor_user, entity=entity,
            target_type=target_type, target_id=target_id,
            document_number=document_number, status=status,
            message=message, metadata=metadata, branch_id=log.branch_id,
        )
    return log  # Return the authoritative finance audit row.


# Handle the record rejection workflow.
def record_rejection(*, entity, action, exc, actor_user=None, target=None,
                     target_type="", target_id="", document_number="",
                     branch=FROM_TARGET, **metadata):
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
                document_number=document_number, branch=branch,
                status=FinanceAuditStatus.FAILED,
                message=str(exc)[:255],
                error_code=error_code,
                **metadata,
            )
    except Exception:  # pragma: no cover - never mask the real error
        pass  # Swallow logging failures so the original business error still surfaces.


def prime_activity_actors(rows):
    """Resolve employment flags once for a bounded document activity feed."""
    from core.person_exit import prime_exit_states

    rows = list(rows)
    states = prime_exit_states({}, (
        user_id for row in rows for user_id in (row.actor_id, row.effective_user_id)
    ))
    for row in rows:
        row._person_exit_states = states
    return rows


def activity_actor(log) -> dict:
    """The "who did it" keys of one activity-feed row built from a finance audit row.

    Document drawers (invoices, payments, contracts, sourcing documents) list
    their :class:`FinanceAuditLog` rows as activity. ``actor_name`` is the person
    who really acted, or "System"; the attribution keys from
    :mod:`core.attribution` add whom they acted as under a proxy, with a ready
    ``acted_label``. Callers select ``actor`` and ``effective_user`` with the rows.
    """
    from core.attribution import audit_row_attribution, person_name
    from core.person_exit import prime_exit_states

    states = getattr(log, "_person_exit_states", None)
    if states is None:
        states = prime_exit_states({}, (log.actor_id, log.effective_user_id))

    return {
        "actor_name": person_name(log.actor) if log.actor_id else "System",
        "actor_is_exited": states.get(log.actor_id) if log.actor_id else None,
        "effective_user_is_exited": (
            states.get(log.effective_user_id) if log.effective_user_id else None
        ),
        **audit_row_attribution(log),
    }
