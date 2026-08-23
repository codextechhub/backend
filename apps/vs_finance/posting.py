"""Posting-layer guards.

The principle locked in Phase 0: **business rules are enforced where journals are
posted, not in the UI.** A request that somehow bypasses the screens (an API call, a
script, a webhook) must still be unable to post into a closed period or write an
unbalanced entry.

In Phase 0 the ``FiscalPeriod`` model does not exist yet, so these guards are
duck-typed: they operate on any object exposing a ``status`` (and optionally a
``label``). Phase 1 introduces the real model and the full ``post_journal`` service,
which will call these same guards - keeping the enforcement point singular.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from django.db import transaction
from django.utils import timezone

from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    PERIOD_POSTING_RESTRICTED,
    PeriodStatus,
)
from .exceptions import (
    FinanceError,
    InactiveAccountError,
    PeriodClosedError,
    PostingError,
    UnbalancedJournalError,
)


# Structural type for period guard inputs.
class _PeriodLike(Protocol):
    status: str  # Period-like objects must expose a status.

    # pragma: no cover - structural typing only  # Human period label for errors.
    def __str__(self) -> str:
        ...


# Guard posting period availability.
def ensure_period_open(
    period: _PeriodLike,
    *,
    allow_restricted: bool = False,
    allow_closed: bool = False,
) -> None:
    """Raise :class:`PeriodClosedError` if ``period`` cannot accept a posting.

    Args:
        period: Any object with a ``status`` string drawn from
            :class:`~vs_finance.constants.PeriodStatus`.
        allow_restricted: When ``True``, soft-closed periods are permitted - used by
            privileged close-process auto-postings (depreciation, accruals). Ordinary
            postings pass ``False`` and are blocked from soft-closed periods too.
        allow_closed: A narrower year-end-close escape hatch. When ``True`` a CLOSED
            period may accept the formal closing journal; LOCKED periods remain
            immutable. No ordinary or month-end posting should set this flag.

    A missing period (``None``) is treated as a hard error: nothing posts without a
    period.
    """
    if period is None:  # Nothing should post without a resolved accounting period.
        raise PeriodClosedError(period_label="<none>", status="missing")

    status = getattr(period, "status", None)  # Read status defensively from period-like object.
    label = str(period)  # Human-readable period label for errors.

    if status == PeriodStatus.LOCKED:  # A statutory lock is irreversible, even at year end.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status == PeriodStatus.CLOSED:  # Only the formal year close may bypass CLOSED.
        if allow_closed:
            return
        raise PeriodClosedError(period_label=label, status=str(status))

    if status in PERIOD_POSTING_RESTRICTED and not allow_restricted:  # Soft-closed periods require privileged posting.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status != PeriodStatus.OPEN and status not in PERIOD_POSTING_RESTRICTED:  # Anything unknown must fail closed.
        # Unknown / unset status - fail closed rather than guess.  # Prevent silent posting into invalid state.
        raise PeriodClosedError(period_label=label, status=str(status or "unknown"))


# Non-raising period posting test.
def _period_accepts_posting(
    period,
    *,
    allow_restricted: bool = False,
    allow_closed: bool = False,
) -> bool:
    """Non-raising sibling of :func:`ensure_period_open`: can a posting land here?

    Mirrors the guard's logic without raising, so callers (e.g. reversal-date
    selection) can *test* a period and pick an alternative rather than fail.
    """
    if period is None:  # Missing period cannot accept postings.
        return False
    status = getattr(period, "status", None)  # Read status defensively.
    if status == PeriodStatus.LOCKED:  # A locked period never accepts another entry.
        return False
    if status == PeriodStatus.CLOSED:  # CLOSED is bypassed only for a formal year-end journal.
        return allow_closed
    if status in PERIOD_POSTING_RESTRICTED:  # Restricted periods depend on caller privilege.
        return allow_restricted
    return status == PeriodStatus.OPEN  # Only open periods accept ordinary postings.


# Describe which dates an ordinary posting may use.
def posting_window(entity, *, today=None) -> dict:
    """Return the dates ``entity`` will currently accept an ordinary posting on.

    The read-side mirror of :func:`ensure_period_open`: that guard answers "may this
    date post?" one date at a time after the fact, and this answers "which dates may
    post?" up front, so a screen can stop offering a date the guard would reject with
    a 409. Both read the same ``PeriodStatus``, so they cannot drift.

    ``open`` lists the OPEN periods (oldest first) as selectable ranges. ``blocked``
    lists the rest, so a picker can say *why* a date is unavailable rather than just
    greying it out. SOFT_CLOSED counts as blocked here: only privileged close-process
    postings may use it (``allow_restricted``), and this window describes what an
    ordinary user may pick.

    ``default_date`` is the date a new document should open on: today when today is
    postable, else the nearest open day in either direction - backward to the most
    recent open day, or forward to the earliest upcoming one, whichever is closer,
    preferring the past on a tie (backdating into a still-open period is the ordinary
    case during a close; post-dating is not). ``None`` when the entity has no open
    period at all, which is a real state the caller must handle rather than paper over.
    """
    from .models import FiscalPeriod

    today = today or timezone.localdate()
    periods = list(
        FiscalPeriod.objects
        .filter(entity=entity)
        .order_by("start_date", "period_no")
    )

    open_periods = [p for p in periods if _period_accepts_posting(p)]
    covering = next(  # The open period containing today, if any.
        (p for p in open_periods if p.start_date <= today <= p.end_date), None,
    )

    if covering is not None:
        default_date = today
    else:
        # Nearest open boundary on each side; the closer one wins, past on a tie.
        previous = max(
            (p.end_date for p in open_periods if p.end_date < today), default=None,
        )
        upcoming = min(
            (p.start_date for p in open_periods if p.start_date > today), default=None,
        )
        if previous and upcoming:
            default_date = (
                previous if (today - previous) <= (upcoming - today) else upcoming
            )
        else:
            default_date = previous or upcoming  # Whichever side exists (or None).

    return {
        "today": today,
        "today_is_open": covering is not None,
        "default_date": default_date,
        "default_period": _period_brief(
            next(
                (p for p in open_periods
                 if default_date and p.start_date <= default_date <= p.end_date),
                None,
            ),
        ),
        "open": [_period_brief(p) for p in open_periods],
        "blocked": [_period_brief(p) for p in periods if p not in open_periods],
    }


# How close to the end of the fiscal calendar counts as "running out".
# One constant, because the number is a policy (how much notice an operator needs to
# get a new fiscal year approved and created), not an implementation detail of any
# one caller. Two months is enough notice for a finance team to raise, review and
# create the next year without the request becoming an emergency.
FISCAL_RUNWAY_WARNING_DAYS = 60

# The three states an entity's fiscal calendar can be in, from the runway read.
FISCAL_RUNWAY_HEALTHY = "HEALTHY"    # Plenty of calendar left; nothing to say.
FISCAL_RUNWAY_EXPIRING = "EXPIRING"  # Calendar ends within the warning threshold.
FISCAL_RUNWAY_EXPIRED = "EXPIRED"    # Calendar has run out (or was never created).


# Describe how much fiscal calendar an entity has left.
def fiscal_calendar_runway(entity, *, today=None) -> dict:
    """Return when ``entity`` runs out of fiscal calendar, and whether to warn now.

    The other read-side mirror of the period guard, alongside :func:`posting_window`.
    That one answers "which dates may post *today*?"; this one answers "how long
    until *no* date can post?" - a question nothing else in the engine asks, and the
    reason it needs asking is that the answer arrives as a hard outage:

    :class:`~vs_finance.models.FiscalPeriod` rows are created a year at a time. Once
    the last one's ``end_date`` passes with no new year created, :func:`resolve_period`
    returns ``None`` for every new document date, :func:`ensure_period_open` raises
    :class:`~vs_finance.exceptions.PeriodClosedError` on the ``None``, and *every*
    posting in the entity fails at once - invoices, receipts, payroll, gateway
    webhooks, close journals. Nothing degrades first, so nothing warns unless we read
    ahead of the date deliberately.

    ``calendar_end`` is the last day any period covers, whatever that period's status:
    a CLOSED December still bounds the calendar, and creating the next year is the
    only fix either way. ``days_remaining`` counts from ``today`` and goes negative
    once the calendar has run out, so a caller can say how long ago it lapsed.

    Three states, each real and each needing different words on screen:

    * ``HEALTHY`` - the calendar ends beyond the threshold; say nothing.
    * ``EXPIRING`` - it ends within :data:`FISCAL_RUNWAY_WARNING_DAYS`; still posting,
      but somebody must create the next year before that date.
    * ``EXPIRED`` - it has already ended, so the entity cannot post at all. An entity
      with **no periods whatsoever** lands here too (with a ``None`` calendar end): a
      brand-new entity that was never given a calendar is in exactly the same
      can't-post position as one that ran off the end of its own, and treating it as
      an error would hide the very state the caller asked about.
    """
    from .models import FiscalPeriod

    today = today or timezone.localdate()
    last = (  # The period that bounds the calendar, ignoring status entirely.
        FiscalPeriod.objects
        .filter(entity=entity)
        .order_by("-end_date", "-period_no")
        .first()
    )

    calendar_end = last.end_date if last is not None else None
    days_remaining = (calendar_end - today).days if calendar_end is not None else None

    if days_remaining is None or days_remaining < 0:  # No calendar, or it has lapsed.
        status = FISCAL_RUNWAY_EXPIRED
    elif days_remaining <= FISCAL_RUNWAY_WARNING_DAYS:  # Ends today or within notice.
        status = FISCAL_RUNWAY_EXPIRING
    else:
        status = FISCAL_RUNWAY_HEALTHY

    return {
        "today": today,
        "calendar_end": calendar_end,
        "days_remaining": days_remaining,
        "threshold_days": FISCAL_RUNWAY_WARNING_DAYS,
        "status": status,
        "should_warn": status != FISCAL_RUNWAY_HEALTHY,
        "last_period": _period_brief(last),
    }


# Shrink a period to the fields a date picker needs.
def _period_brief(period) -> dict | None:
    """Serialise a period to the minimum a picker needs: when it is and why."""
    if period is None:
        return None
    return {
        "id": period.id,
        "name": period.name,
        "period_no": period.period_no,
        "status": period.status,
        "start_date": period.start_date,
        "end_date": period.end_date,
    }


# Guard exact double-entry equality.
def ensure_balanced(debit_kobo: int, credit_kobo: int) -> None:
    """Raise :class:`UnbalancedJournalError` unless debits exactly equal credits.

    Amounts are integer kobo, so equality is exact - there is no rounding tolerance,
    and none is wanted: a ledger that is off by one kobo is wrong.
    """
    if debit_kobo != credit_kobo:  # Even one kobo imbalance is invalid.
        raise UnbalancedJournalError(debit=debit_kobo, credit=credit_kobo)


# Sum debit and credit sides over journal-like lines.
def sum_sides(lines: Iterable) -> tuple[int, int]:
    """Sum ``(total_debit, total_credit)`` in kobo over an iterable of journal lines.

    Each line is expected to expose integer ``debit`` and ``credit`` attributes
    (kobo). Lines are one-sided by convention (a line is a debit OR a credit), but
    this tolerates both being present and simply sums them.
    """
    total_debit = 0  # Running debit total in kobo.
    total_credit = 0  # Running credit total in kobo.
    for line in lines:  # Walk each supplied journal line.
        total_debit += getattr(line, "debit", 0) or 0  # Add debit amount, treating None as zero.
        total_credit += getattr(line, "credit", 0) or 0  # Add credit amount, treating None as zero.
    return total_debit, total_credit  # Return both exact integer totals.


# ---------------------------------------------------------------------------
# Phase 1 - posting services
# ---------------------------------------------------------------------------
#
# These are the ONLY supported way to make a journal affect balances. They run the
# Phase-0 guards (period open, balanced), update the denormalised per-period balances
# atomically, stamp the document POSTED, and write an authoritative finance audit row
# (see vs_finance.audit) in the SAME transaction. Posting is never done by flipping
# ``status`` by hand.


# Find the fiscal period covering a document date.
def resolve_period(entity, date):
    """Return the :class:`FiscalPeriod` for ``entity`` covering ``date``, or ``None``.

    Used by sub-ledger services (AR/AP) to attach a journal to the right period from a
    document date. ``None`` is returned when no period covers the date; the posting
    guard then fails closed, refusing to post a dateless/period-less entry.
    """
    from .models import FiscalPeriod

    return (  # Return matching period or None.
        FiscalPeriod.objects
        .filter(entity=entity, start_date__lte=date, end_date__gte=date)
        .order_by("period_no")
        .first()
    )


# Apply or remove journal amounts from account balances.
def _apply_to_balances(entry, *, sign: int) -> None:
    """Add (sign=+1) or remove (sign=-1) an entry's line amounts to per-period balances. 
    So the journal lines are the source of truth, and the denormalised balances are kept in step.
    The ``sign`` argument allows this to be used for both posting and unposting (reversing) journals.

    Maintains one :class:`AccountBalance` row per ``(account, period)``, the fast
    aggregate behind trial balances. Truth still lives in the immutable lines; this is
    a denormalised read model kept in step inside the same transaction as the post.
    """
    from .models import AccountBalance

    period = entry.period  # already guarded to exist and be open  # Posting target period.
    for line in entry.lines.select_related("account").all():
        balance, _ = AccountBalance.objects.select_for_update().get_or_create(
            account=line.account, period=period,  # One balance row per account and period.
        )
        balance.debit_total += sign * (line.debit or 0)  # Add/remove debit amount.
        balance.credit_total += sign * (line.credit or 0)  # Add/remove credit amount.
        balance.save(update_fields=["debit_total", "credit_total", "updated_at"])


# Public journal posting wrapper.
def post_journal(
    entry,
    *,
    actor_user=None,
    allow_restricted: bool = False,
    allow_closed: bool = False,
):
    """Post a draft :class:`~vs_finance.models.JournalEntry`, making it affect balances.

    Thin wrapper around the atomic core (:func:`_post_journal_atomic`) that turns any
    :class:`~vs_finance.exceptions.FinanceError` into a **durable** rejection audit row
    before re-raising. The rejection must be logged *outside* the rolled-back posting
    transaction, which is why this layer sits above the ``@transaction.atomic`` core.

    Idempotent guard: re-posting an already-POSTED entry raises rather than
    double-counting. Returns the entry.
    """
    from .audit import record_rejection

    try:  # Atomic core may roll back; wrapper logs rejection after rollback.
        return _post_journal_atomic(  # Perform the actual posting.
            entry, actor_user=actor_user, allow_restricted=allow_restricted,
            allow_closed=allow_closed,  # Actor and tightly scoped period privileges.
        )
    except FinanceError as exc:  # Finance-domain errors get durable rejection audit.
        record_rejection(  # Record failed posting attempt.
            entity=entry.entity,  # Entity being posted into.
            action=FinanceAuditAction.JOURNAL_POST_REJECTED,  # Audit action for rejected journal post.
            exc=exc, actor_user=actor_user, target=entry,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Transactional journal posting implementation.
def _post_journal_atomic(
    entry,
    *,
    actor_user=None,
    allow_restricted: bool = False,
    allow_closed: bool = False,
):
    """The posting work proper, all in one transaction.

    Steps:
      1. Guard the period is open (SOFT_CLOSED only when ``allow_restricted``).
      2. Guard the lines balance (Σdebits == Σcredits, exact kobo).
      3. Guard every line's account is active and postable.
      4. Apply the line amounts to the per-period :class:`AccountBalance` aggregates.
      5. Stamp the entry POSTED with ``posted_at``/``posted_by``.
      6. Write the authoritative ``JOURNAL_POSTED`` audit row - same commit as 4–5.
    """
    from .audit import record
    from .models import JournalEntry

    # Serialise concurrent posts of the *same* entry: take a row lock and re-read the
    # status under it before doing anything. Without this, two requests can both pass
    # the status guard on a stale in-memory copy and each apply the lines to
    # AccountBalance - double-counting the ledger. The loser blocks here, then sees
    # POSTED and is rejected. (Document numbering is already lock-safe; posting was not.)
    locked_status = (  # Re-read status while holding the journal row lock.
        JournalEntry.objects.select_for_update()
        .values_list("status", flat=True).get(pk=entry.pk)
    )
    if locked_status == DocumentStatus.POSTED:  # Posted journals must not be posted twice.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is already posted.",
        )
    if locked_status in (DocumentStatus.REVERSED, DocumentStatus.CANCELLED):  # Terminal journals cannot post.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is '{locked_status}' and cannot be posted.",
        )

    ensure_period_open(  # Guard period status; CLOSED bypass is reserved for formal year close.
        entry.period,
        allow_restricted=allow_restricted,
        allow_closed=allow_closed,
    )

    lines = list(entry.lines.select_related("account").all())
    if not lines:  # A journal with no lines has no accounting substance.
        raise PostingError("A journal must have at least one line to post.")

    total_debit, total_credit = sum_sides(lines)  # Calculate exact debit and credit totals.
    ensure_balanced(total_debit, total_credit)  # Enforce double-entry equality.

    for line in lines:  # Validate every account touched by the journal.
        account = line.account  # Account on this line.
        if not (account.is_active and account.is_postable):  # Inactive/header accounts cannot post.
            raise InactiveAccountError(account_code=account.code)

    _apply_to_balances(entry, sign=+1)  # Add line amounts to per-period balances.

    entry.status = DocumentStatus.POSTED  # Mark journal posted.
    entry.posted_at = timezone.now()
    entry.posted_by = actor_user  # Store posting actor.
    entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])

    record(  # Audit the successful journal post.
        entity=entry.entity,  # Entity posted into.
        action=FinanceAuditAction.JOURNAL_POSTED,  # Audit action.
        actor_user=actor_user, target=entry,  # Actor and target context.
        message=f"Posted into {entry.period}.",  # Human-readable audit message.
        after={"status": DocumentStatus.POSTED, "posted_at": entry.posted_at.isoformat()},  # Post-state snapshot.
        debit=total_debit, credit=total_credit,  # Structured totals.
    )
    return entry  # Return posted journal.


def _journal_document_owner(entry):
    """Return the first sub-ledger object whose journal field points at ``entry``.

    This deliberately uses model metadata rather than a short hard-coded list: new
    finance/procurement documents that add a ``journal``/``*_journal`` FK are guarded
    automatically instead of quietly reopening the same bypass.
    """
    from django.core.exceptions import ObjectDoesNotExist

    for relation in entry._meta.related_objects:
        if "journal" not in relation.field.name:
            continue
        accessor = relation.get_accessor_name()
        try:
            related = getattr(entry, accessor)
            owner = related.order_by("pk").first() if relation.one_to_many else related
        except ObjectDoesNotExist:
            continue
        if owner is None:
            continue
        # A later credit/advance reclassification belongs to its receipt, note or
        # vendor payment, not to the small linkage row that makes the relationship
        # durable. Both sides of the ledger park early money in a holding account and
        # reclassify it when the document it belongs to arrives.
        if type(owner).__name__ == "CustomerCreditAllocationJournal":
            owner = owner.payment or owner.note
        elif type(owner).__name__ == "VendorAdvanceAllocationJournal":
            owner = owner.payment
        return owner

    # Pre-migration later-allocation journals were linked only through the
    # append-only audit metadata.  Keep the raw-reversal guard fail-closed even if a
    # historical row could not be backfilled into CustomerCreditAllocationJournal.
    from .constants import FinanceAuditAction, FinanceAuditStatus
    from .models import CreditNote, FinanceAuditLog, Payment

    legacy_sources = (
        (FinanceAuditAction.PAYMENT_ALLOCATED, Payment),
        (FinanceAuditAction.CREDIT_NOTE_ALLOCATED, CreditNote),
    )
    for action, model in legacy_sources:
        audit = FinanceAuditLog.objects.filter(
            entity=entry.entity, action=action, status=FinanceAuditStatus.SUCCESS,
            metadata__journal_id=entry.pk,
        ).order_by("pk").first()
        if audit and str(audit.target_id).isdigit():
            owner = model.objects.filter(pk=int(audit.target_id), entity=entry.entity).first()
            if owner is not None:
                return owner
    return None


_DOCUMENT_VOID_ROUTES = {
    "Invoice": ("INVOICE", "invoices/{pk}/void/"),
    "Payment": ("PAYMENT", "payments/{pk}/void/"),
    "CreditNote": ("CREDIT_NOTE", "credit-notes/{pk}/void/"),
    "Refund": ("REFUND", "refunds/{pk}/void/"),
    "Concession": ("CONCESSION", "concessions/{pk}/void/"),
}


def journal_reversal_action(entry):
    """Describe the safe action the journal screen may offer.

    The frontend must never infer document ownership from narration/source text.
    This contract is derived from the same relationship lookup that enforces the
    raw-reversal guard, so the button and the service cannot disagree.
    """
    owner = _journal_document_owner(entry)
    if owner is None:
        return {"kind": "REVERSE_JOURNAL"}

    model_name = type(owner).__name__
    config = _DOCUMENT_VOID_ROUTES.get(model_name)
    if config is None:
        return {
            "kind": "SOURCE_DOCUMENT_ACTION",
            "document_type": model_name,
            "document_number": getattr(owner, "document_number", "") or str(owner.pk),
        }

    document_type, _ = config
    return {
        "kind": "VOID_DOCUMENT",
        "document_type": document_type,
        "document_id": owner.pk,
        "document_number": getattr(owner, "document_number", "") or str(owner.pk),
    }


def _document_void_instruction(owner):
    model_name = type(owner).__name__
    label = getattr(owner, "document_number", "") or str(owner.pk)
    config = _DOCUMENT_VOID_ROUTES.get(model_name)
    if config:
        _, route = config
        remedy = f"Use POST /finance/{route.format(pk=owner.pk)} from the document screen instead."
    else:
        remedy = f"Use the {model_name} document-level void/reversal service instead."
    return model_name, label, remedy


@transaction.atomic
# Reverse a posted journal.
def reverse_journal(entry, *, actor_user=None, date=None, allow_restricted: bool = False,
                    document_owner=None):
    """Reverse a posted journal by raising a mirror-image entry that nets it to zero.

    The original is left untouched on the record (marked REVERSED) and a new journal -
    debits and credits swapped - is created and posted, linked back via ``reverses``.
    This is the audit-correct way to undo: history is appended to, never edited.

    The reversing entry posts into ``date``'s period (defaults to the original's
    period). Returns the new reversing entry.
    """
    from .models import JournalEntry, JournalLine

    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)

    owner = _journal_document_owner(entry)
    if owner is not None:
        owner_matches = (
            document_owner is not None
            and type(owner) is type(document_owner)
            and owner.pk == document_owner.pk
        )
        if not owner_matches:
            model_name, label, remedy = _document_void_instruction(owner)
            raise PostingError(
                f"Journal {entry.document_number or entry.pk} belongs to "
                f"{model_name} {label} and cannot be reversed on its own. {remedy}",
                owner_type=model_name, owner_id=str(owner.pk),
                owner_document_number=label,
            )

    if entry.status != DocumentStatus.POSTED:  # Only posted journals can be reversed.
        raise PostingError(
            f"Only a posted journal can be reversed; {entry.document_number or entry.pk} "
            f"is '{entry.status}'.",
        )
    if hasattr(entry, "reversed_by") and entry.reversed_by is not None:  # Prevent duplicate reversals.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} has already been reversed.",
        )

    # Resolve the reversal's date and period from that date (an old bug pinned the
    # reversal to the original's period regardless of the date passed). Prefer the
    # caller's date; else the original's. But if the original's period has since
    # closed and no explicit date was given, fall back to today so a prior-period
    # correction can still be booked into the current open period - the standard way
    # to reverse after a period closes.
    reversal_date = date or entry.date  # Prefer explicit reversal date, otherwise original date.
    from .chronology import ensure_on_or_after
    ensure_on_or_after(
        subject=f"Reversal of journal {entry.document_number or entry.pk}",
        subject_date=reversal_date,
        source=f"journal {entry.document_number or entry.pk}",
        source_date=entry.date,
        remedy=f"Date the reversal {entry.date} or later.",
    )
    period = resolve_period(entry.entity, reversal_date)  # Resolve period for selected reversal date.
    if date is None and not _period_accepts_posting(period, allow_restricted=allow_restricted):  # Original period may now be closed.
        reversal_date = timezone.now().date()
        # Falling forward to today must not turn a future-dated source into a
        # backdated reversal.  Re-run chronology after changing the date; the
        # surrounding transaction leaves the source untouched if today is earlier.
        ensure_on_or_after(
            subject=f"Reversal of journal {entry.document_number or entry.pk}",
            subject_date=reversal_date,
            source=f"journal {entry.document_number or entry.pk}",
            source_date=entry.date,
            remedy=f"Date the reversal {entry.date} or later.",
        )
        period = resolve_period(entry.entity, reversal_date)  # Resolve today's period.

    reversal = JournalEntry.objects.create(
        entity=entry.entity,  # Same entity as original.
        branch=entry.branch,  # Same branch as original.
        date=reversal_date,  # Reversal accounting date.
        period=period,  # Reversal posting period.
        source=JournalSource.SYSTEM,  # System-generated reversal.
        currency=entry.currency,  # Same currency as original.
        fx_rate=entry.fx_rate,  # Same FX rate as original.
        narration=f"Reversal of {entry.document_number or entry.pk}",  # Clear reversal narration.
        reference=entry.reference,  # Preserve reference.
        created_by=actor_user,  # Attribute reversal to actor.
        reverses=entry,  # Link reversal back to original.
    )
    for line in entry.lines.all():  # Mirror every original journal line.
        JournalLine.objects.create(
            entry=reversal,  # Attach to reversal journal.
            account=line.account,  # Same account as original line.
            debit=line.credit,   # swap sides  # Original credit becomes reversal debit.
            credit=line.debit,  # Original debit becomes reversal credit.
            description=f"Reversal: {line.description}".strip(": "),  # Label reversal line.
            cost_center=line.cost_center,  # Preserve analytics.
            dimensions=line.dimensions,  # Preserve dimensions.
            line_no=line.line_no,  # Preserve line order.
        )

    post_journal(reversal, actor_user=actor_user, allow_restricted=allow_restricted)  # Post mirror journal.

    entry.status = DocumentStatus.REVERSED  # Mark original as reversed.
    entry.save(update_fields=["status", "updated_at"])

    from .audit import record
    record(  # Audit reversal of original journal.
        entity=entry.entity,  # Entity context.
        action=FinanceAuditAction.JOURNAL_REVERSED,  # Audit action.
        actor_user=actor_user, target=entry,  # Actor and original journal target.
        message=f"Reversed by {reversal.document_number or reversal.pk}.",  # Human-readable audit message.
        after={"status": DocumentStatus.REVERSED},  # Original journal final status.
        reversal_id=reversal.pk, reversal_number=reversal.document_number,  # Link to reversal journal.
    )
    return reversal  # Return the posted reversal journal.


@transaction.atomic
def post_direct_entry(entity, *, lines, date=None, narration="", reference="",
                      actor_user=None):  # Create and post a raw direct journal entry.
    """Post a direct journal entry - money/balances seated into the books with no source doc.

    This is the *sanctioned* way to record anything that has no sub-ledger document behind
    it: capital injections and equity contributions, loan drawdowns, grants, opening cash,
    opening AR/AP, and manual adjustments. Unlike sub-ledger postings (which derive their
    journal from an invoice/payment/etc.), a direct entry is the one place a caller supplies
    raw lines. It posts with ``source=OPENING`` (the catch-all for sourceless entries).

    ``lines`` is a list of ``(account, debit_kobo, credit_kobo)`` - optionally extended
    with a 4th element ``cost_center`` and a 5th element ``dimensions`` - where ``account``
    is a code string (resolved within ``entity``) or an :class:`~vs_finance.models.Account`,
    the optional ``cost_center`` is a :class:`~vs_finance.models.CostCenter` (or ``None``)
    and ``dimensions`` is a ``{axis_code: value}`` map, both carried onto the GL line. The
    entry must balance (Σdebits == Σcredits); it posts into
    ``date``'s open period - ``date`` defaults to the entity's earliest period start, else
    today. The normal :func:`post_journal` guards apply (period open, balanced, accounts
    active/postable), and it is reversible like any journal. Returns the posted entry.
    """
    from django.utils import timezone

    from .accounts import resolve_account
    from .models import FiscalPeriod, JournalEntry, JournalLine

    rows = list(lines or [])  # Normalize iterable input and handle None.
    if not rows:  # Direct entries must include at least one line.
        raise PostingError("A direct entry needs at least one line.")

    if date is None:  # Default direct-entry date when caller omits one.
        date = (  # Prefer earliest fiscal period start, otherwise today.
            FiscalPeriod.objects.filter(entity=entity)
            .order_by("start_date").values_list("start_date", flat=True).first()
            or timezone.now().date()
        )

    entry = JournalEntry.objects.create(
        entity=entity, date=date, period=resolve_period(entity, date),  # Entity, date, and resolved period.
        source=JournalSource.OPENING,  # Direct entries use opening/sourceless source.
        narration=narration or "Opening balances",  # Default narration.
        reference=reference, created_by=actor_user,  # External reference and actor.
    )
    for i, row in enumerate(rows, start=1):  # Create journal lines in input order.
        # optional 4th element: cost_center, optional 5th: dimensions JSON map  # Extra tuple values carry analytics.
        account, debit, credit, *rest = row  # Unpack mandatory and optional line values.
        cost_center = rest[0] if rest else None  # Optional cost center.
        dimensions = rest[1] if len(rest) > 1 else {}  # Optional dimensions JSON map.
        acct = account if not isinstance(account, str) else resolve_account(entity, account)  # Resolve code strings.
        JournalLine.objects.create(
            entry=entry, account=acct,  # Attach line to entry and account.
            debit=int(debit or 0), credit=int(credit or 0),  # Store integer kobo side amounts.
            cost_center=cost_center, dimensions=dimensions or {}, line_no=i,  # Store analytics and line number.
        )

    post_journal(entry, actor_user=actor_user)  # Run normal posting guards and balance updates.
    entry.refresh_from_db()
    return entry  # Return posted direct entry.
