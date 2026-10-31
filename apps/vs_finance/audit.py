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


#: The metadata keys of a mirrored entry that name no value: the finance action,
#: the error code of a refusal and the proxy attribution. Every other key a
#: caller passes stays in the finance trail and goes no further.
MIRROR_METADATA_KEYS = frozenset({
    "finance_action", "error_code", "impersonation_session_id", "effective_user_id",
})

#: Metadata key marking a mirrored entry written without values.
MIRROR_MARKER = "recorded_without_values"

#: Metadata key every finance entry carries from the moment its sentence was
#: made figure-free. See :func:`entry_sentence` for how it divides the trail.
ENTRY_MARKER = "sentence_without_values"


def _action_words(action) -> str:
    """The plain-words name of a finance action, whether or not it is a known one."""
    from .constants import FinanceAuditAction

    try:
        return FinanceAuditAction(str(action)).label
    except ValueError:
        return str(action).replace("_", " ").capitalize()


def mirror_summary(action, document_number="") -> str:
    """The one sentence the central trail keeps about a finance entry.

    It names the act and the document ("Invoice posted: INV-0042") and nothing
    the entry's own sentence or metadata says: no amount, no third party, no
    address, no narration, no reason. Who acted, in which branch and when are
    the central event's own columns.
    """
    words = _action_words(action)
    return f"{words}: {document_number}" if document_number else words


def _legacy_summary(event) -> str:
    """:func:`mirror_summary` for a central event written before the mirror was value-free."""
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return mirror_summary(metadata.get("finance_action", ""), event.entity_label or "")


def _legacy_summary_sql():
    """:func:`_legacy_summary` as a database expression, for a search or an export."""
    from django.db.models import Case, F, Q, TextField, Value, When
    from django.db.models.functions import Concat

    from .constants import FinanceAuditAction

    words = Case(
        *(
            When(Q(metadata__finance_action=value), then=Value(label))
            for value, label in FinanceAuditAction.choices
        ),
        default=Value("Finance entry"),
        output_field=TextField(),
    )
    return Case(
        When(entity_label="", then=words),
        default=Concat(words, Value(": "), F("entity_label")),
        output_field=TextField(),
    )


def register_mirror_rule():
    """Keep every finance entry in the central trail free of values, old ones included.

    The mirror writes only :func:`mirror_summary` and :data:`MIRROR_METADATA_KEYS`.
    Entries mirrored before that rule carry the finance sentence and every
    metadata argument, so the central trail rebuilds their sentence and cuts
    their metadata to the same keys when it reads them
    (:class:`vs_audit.protected_words.RecordOnly`). The stored rows are not
    rewritten.
    """
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_audit.protected_words import RecordOnly, register_record_only

    register_record_only("finance.mirror", RecordOnly(
        module_key=AuditModuleKey.FINANCE,
        entity_prefix="vs_finance",
        action_types=frozenset({AuditActionType.FINANCIAL_TRANSACTION}),
        required_keys=frozenset({"finance_action"}),
        metadata_allow=MIRROR_METADATA_KEYS | {MIRROR_MARKER},
        marker=MIRROR_MARKER,
        summary_for=_legacy_summary,
        summary_sql=_legacy_summary_sql,
    ))


# Support the mirror to central workflow.
def _mirror_to_central(*, action, actor_user, entity, target_type, target_id,
                       document_number, status, metadata, branch_id=None):
    """Best-effort copy into central vs_audit. Never raises - the in-app log is truth.

    The copy records who did what to which document, in which branch and when,
    and carries no value. Its sentence is built from the action and the document
    number alone (:func:`mirror_summary`) and its metadata is cut to
    :data:`MIRROR_METADATA_KEYS`, so a figure, a name, an address or a reason a
    caller adds to the finance entry later cannot travel into the central trail.
    The copy carries the entry's branch, so the platform trail narrows a
    branch-bound reader exactly as the finance trail does.
    """
    try:  # Mirroring must never block the primary finance write.
        from vs_audit.services import emit_audit_event
        from vs_audit.models import AuditModuleKey, AuditActionType

        kept = {k: v for k, v in (metadata or {}).items() if k in MIRROR_METADATA_KEYS}
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
            summary=mirror_summary(action, document_number or str(target_id or "")),
            metadata={"finance_action": str(action), **kept, MIRROR_MARKER: True},
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

    ``message`` is the entry's sentence and says what happened to which document and
    where, in words a reader of any role may read: a document number, a person, a
    customer, a vendor, a branch or a bank account is a label and may appear; an
    amount, a balance, an address, a narration or a free-text reason is a value
    and never does. Values travel as ``before``/``after`` (read through the
    reader's Field Access, see ``AUDIT_SNAPSHOT_FIELDS``) or as metadata. Metadata
    is never served whole; the one key a screen reads from it is ``reason``, the
    free-text reason a person gave, which :func:`entry_reason` serves beside the
    sentence. Every entry is stamped with :data:`ENTRY_MARKER`, which is how
    :func:`entry_sentence` tells it from an entry written before sentences were
    figure-free.

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
    from core.encryption import withhold

    from .models import FinanceAuditLog
    from vs_tenants.context import add_proxy_audit_metadata, resolve_audit_identity

    actor_user, effective_user, proxy_session = resolve_audit_identity(actor_user)
    metadata = {**add_proxy_audit_metadata(metadata, effective_user, proxy_session),
                ENTRY_MARKER: True}

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
        # An encrypted column's value is never kept whole (core.encryption).
        before=withhold(before or {}),
        after=withhold(after or {}),
        metadata=withhold(metadata or {}),
        branch_id=entry_branch_id(target, branch),
    )

    if mirror:  # Optionally mirror the event into the platform-wide audit log.
        _mirror_to_central(  # Copy the finance event into the central audit system.
            action=action, actor_user=actor_user, entity=entity,
            target_type=target_type, target_id=target_id,
            document_number=document_number, status=status,
            metadata=metadata, branch_id=log.branch_id,
        )
    return log  # Return the authoritative finance audit row.


def refusal_words(error_code) -> str:
    """A refusal's sentence, made from its error code alone.

    The refusal's own text can name an amount or a balance ("holds N, more than
    a float of N"), so the sentence says only which rule refused; the text itself
    stays in the entry's metadata.
    """
    words = str(error_code or "").replace("_", " ").strip().lower()
    return f"Refused: {words}." if words else "Refused."


def entry_sentence(log) -> str:
    """The sentence a screen shows for *log*, whatever era the entry was written in.

    An entry written now carries :data:`ENTRY_MARKER` in its metadata and its
    stored ``message`` is figure-free by construction: it names the act, the
    document, the people, the branches and the accounts, and no amount,
    narration, address or reason, which travel in ``before``/``after`` (filtered
    by Field Access) or in the metadata.

    An entry without the marker predates that rule, and its stored sentence may
    carry any of those values. The trail is append-only, so the row is never
    rewritten; every screen shows the sentence the central mirror keeps for the
    same act instead, built from the action's plain-words name and the document
    number alone (:func:`mirror_summary`: "Invoice posted: INV-0042"). Presence
    of the marker, not the shape of ``before``/``after``, is the cut-off: it is
    set in one place (:func:`record`), by every writer, in the same row as the
    sentence it vouches for, so no entry is ever judged by a guess.

    Every feed that serves an entry's sentence (the finance trail and its
    detail, the settings history, and the procurement document activity feeds)
    passes the row through here.
    """
    metadata = log.metadata if isinstance(log.metadata, dict) else {}
    if ENTRY_MARKER in metadata:
        return log.message
    return mirror_summary(log.action, log.document_number)


#: Actions whose free-text ``reason`` belongs to a registered pay field, and the
#: Field Access keys of which a reader needs at least one to read it. A change
#: to someone's pay terms is explained in words about their pay, so the reason
#: follows the pay switches.
_PAY_KEYS = ("finance.salary.gross_amount", "finance.salary.paye_amount",
             "finance.salary.pension_amount")
GUARDED_METADATA_REASONS = {"SALARY_CHANGED": _PAY_KEYS}

#: Actions whose reason is a registered field of the entry's own ``before`` or
#: ``after`` snapshot: action -> (snapshot name, key in the snapshot). The
#: snapshot the reader is given has already been through Field Access, so the
#: reason is read from it and is absent whenever the field is hidden.
SNAPSHOT_REASONS = {"INTER_BRANCH_DECLINED": ("after", "decline_reason")}


def entry_reason(log, *, can_read=None, snapshots=None) -> str:
    """The free-text reason a person gave for the act *log* records, or "".

    Reasons for cancelling an order, closing a request for quotation,
    terminating a contract, ignoring a statement line and the like are kept in
    the entry's metadata and never in its sentence, so a screen shows them as a
    value of their own beside the sentence. A reason no Field Access field
    guards is shown to every reader of the entry.

    A reason that belongs to a registered field follows that field. A pay terms
    change needs a readable pay switch (:data:`GUARDED_METADATA_REASONS`); a
    declined inter-branch request is read from the reader's own filtered
    ``after`` (:data:`SNAPSHOT_REASONS`, passed as ``snapshots``). ``can_read``
    answers Field Access for one key; a feed with no reader passes neither
    argument, and a guarded reason then reads as empty.
    """
    metadata = log.metadata if isinstance(log.metadata, dict) else {}
    action = str(log.action)
    if action in SNAPSHOT_REASONS:
        name, key = SNAPSHOT_REASONS[action]
        value = (snapshots or {}).get(name)
        text = value.get(key) if isinstance(value, dict) else ""
        return text if isinstance(text, str) else ""
    keys = GUARDED_METADATA_REASONS.get(action)
    if keys is not None and not (can_read is not None and any(can_read(k) for k in keys)):
        return ""
    text = metadata.get("reason")
    return text.strip() if isinstance(text, str) else ""


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
    detail = str(exc)[:255]
    try:  # Rejection logging must not mask the original exception.
        with transaction.atomic():
            record(  # Persist the failed finance action.
                entity=entity, action=action, actor_user=actor_user,
                target=target, target_type=target_type, target_id=target_id,
                document_number=document_number, branch=branch,
                status=FinanceAuditStatus.FAILED,
                message=refusal_words(error_code),
                error_code=error_code,
                refusal=detail,
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


def activity_entry(log) -> dict:
    """One row of a document's activity feed, built from a finance audit row.

    ``message`` is the entry's sentence (:func:`entry_sentence`), ``reason`` the
    free-text reason behind the act when there is one (:func:`entry_reason`),
    and the actor keys are :func:`activity_actor`'s. The raw metadata is never
    served. Callers select ``actor`` and ``effective_user`` with the rows and
    prime them with :func:`prime_activity_actors`.
    """
    return {
        "id": log.id, "action": log.action, "message": entry_sentence(log),
        "reason": entry_reason(log), "status": log.status,
        **activity_actor(log),
        "created_at": log.created_at,
    }


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
