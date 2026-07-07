"""Dunning — automated, escalating reminders for overdue receivables.

A :class:`~vs_finance.models.DunningPolicy` is a ladder of stages keyed by *days
overdue*. :func:`generate_dunning` scans an entity's open invoices and, for each one
past due, raises a :class:`~vs_finance.models.DunningNotice` at the **highest** stage it
qualifies for — idempotent per ``(invoice, level)`` so re-running never re-issues a
reminder the customer already got at that rung.

Dunning is a *communications overlay*: nothing here touches the General Ledger.
vs_finance only tracks the reminder lifecycle (PENDING → SENT, or CANCELLED / RESOLVED);
an outer notifications service is expected to read PENDING notices and dispatch them
through the recorded channel. All amounts are integer kobo.
"""
from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.utils import timezone

from .audit import record
from .constants import (
    DocumentStatus,
    DunningChannel,
    DunningNoticeStatus,
    FinanceAuditAction,
)
from .exceptions import PostingError

logger = logging.getLogger(__name__)


#: A sensible out-of-the-box ladder: (level, name, min_days_overdue, channel).
DEFAULT_STAGES = (
    (1, "Friendly reminder", 1, DunningChannel.EMAIL),
    (2, "Second reminder", 14, DunningChannel.EMAIL),
    (3, "Final notice", 30, DunningChannel.EMAIL),
)


def ensure_default_policy(entity, *, name="Standard reminders"):
    """Return ``entity``'s default dunning policy, creating a standard ladder if absent.

    Idempotent: if a default policy already exists it is returned untouched; otherwise a
    new default policy with the :data:`DEFAULT_STAGES` ladder is created.
    """
    from .models import DunningPolicy, DunningStage

    existing = DunningPolicy.objects.filter(entity=entity, is_default=True).first()
    if existing is not None:
        return existing

    policy = DunningPolicy.objects.create(
        entity=entity, name=name, is_active=True, is_default=True,
    )
    DunningStage.objects.bulk_create([
        DunningStage(
            policy=policy, level=level, name=stage_name,
            min_days_overdue=days, channel=channel,
            message=f"{stage_name}: your account has an overdue balance.",
        )
        for level, stage_name, days, channel in DEFAULT_STAGES
    ])
    return policy


def _resolve_policy(entity, policy=None):
    """Pick the policy to run: the one passed, else the entity's active default."""
    from .models import DunningPolicy

    if policy is not None:
        return policy
    chosen = (
        DunningPolicy.objects.filter(entity=entity, is_default=True, is_active=True).first()
        or DunningPolicy.objects.filter(entity=entity, is_active=True).order_by("id").first()
    )
    if chosen is None:
        raise PostingError(
            "No active dunning policy for this entity. Create one (or call "
            "ensure_default_policy) before generating reminders.",
        )
    return chosen


def _stage_for(stages, days_overdue: int):
    """Highest stage whose ``min_days_overdue`` is met by ``days_overdue`` (or ``None``)."""
    match = None
    for stage in stages:  # stages pre-sorted ascending by min_days_overdue
        if days_overdue >= stage.min_days_overdue:
            match = stage
        else:
            break
    return match


@transaction.atomic
def generate_dunning(entity, *, as_of=None, policy=None, customer=None, actor_user=None):
    """Raise dunning notices for ``entity``'s overdue invoices as at ``as_of`` (today default).

    For each posted, not-fully-paid invoice with an outstanding balance, days-overdue is
    measured from its due date (falling back to its invoice date) and the invoice is
    advanced **one rung**: the *lowest-level* :class:`~vs_finance.models.DunningStage` it
    qualifies for that has not been issued yet. So escalation never skips a rung — a
    backlog climbs L1 → L2 → L3 over successive runs rather than jumping straight to the
    final notice — and at most one new notice is raised per invoice **per run date**
    (``as_of``), making same-day re-runs idempotent. Notices on invoices that have since
    been settled are flipped to RESOLVED. Returns the list of newly created notices.
    """
    from collections import defaultdict
    from .models import DunningNotice, Invoice

    as_of = as_of or timezone.now().date()
    policy = _resolve_policy(entity, policy)
    stages = list(policy.stages.order_by("min_days_overdue", "level"))
    if not stages:
        raise PostingError(f"Dunning policy '{policy.name}' has no stages defined.")

    # Narrow to overdue, still-owing invoices in SQL (not in Python): balance_due is a
    # property, so annotate it and the effective due date and filter on them — the loop
    # then only loads invoices that can actually qualify for a stage.
    from django.db.models import F
    from django.db.models.functions import Coalesce

    balance = F("total") - F("amount_paid") - F("amount_credited")
    invoices = (
        Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
        .annotate(_balance=balance, _due=Coalesce("due_date", "invoice_date"))
        .filter(_balance__gt=0, _due__lt=as_of)
        .select_related("customer")
    )
    if customer is not None:
        invoices = invoices.filter(customer=customer)

    # Resolve any outstanding reminders whose invoice is now fully settled.
    _resolve_settled(entity, actor_user=actor_user)

    invoice_list = list(invoices)
    # Pre-load (two queries, no per-invoice N+1): which levels each invoice has already
    # been issued, and which invoices were already advanced on this run date.
    issued_levels: dict[int, set] = defaultdict(set)
    issued_today: set = set()
    if invoice_list:
        for inv_id, lvl, ndate in DunningNotice.objects.filter(
            invoice_id__in=[i.id for i in invoice_list],
        ).values_list("invoice_id", "level", "notice_date"):
            issued_levels[inv_id].add(lvl)
            if ndate == as_of:
                issued_today.add(inv_id)

    stages_by_level = sorted(stages, key=lambda s: s.level)
    created = []
    for invoice in invoice_list:
        if invoice.id in issued_today:
            continue  # already advanced one rung on this run date
        days_overdue = (as_of - (invoice.due_date or invoice.invoice_date)).days
        already = issued_levels[invoice.id]
        # Next rung: the lowest-level qualifying stage not yet issued for this invoice.
        stage = next(
            (s for s in stages_by_level
             if s.min_days_overdue <= days_overdue and s.level not in already),
            None,
        )
        if stage is None:
            continue

        notice = DunningNotice.objects.create(
            entity=entity, branch=invoice.branch, customer=invoice.customer,
            invoice=invoice, policy=policy, stage=stage, level=stage.level,
            notice_date=as_of, days_overdue=days_overdue, amount_due=invoice.balance_due,
            channel=stage.channel, message=stage.message,
            notice_status=DunningNoticeStatus.PENDING,
            created_by=actor_user,
        )
        created.append(notice)

    record(
        entity=entity, action=FinanceAuditAction.DUNNING_RUN_GENERATED,
        actor_user=actor_user, target=policy,
        message=f"Generated {len(created)} dunning notice(s) under '{policy.name}' "
                f"as at {as_of}.",
        policy_id=policy.pk, as_of=str(as_of), notices_created=len(created),
    )
    return created


@transaction.atomic
def remind_invoice(invoice, *, actor_user=None, send=True, message=""):
    """Raise (and, by default, send) a dunning reminder for a single invoice.

    The per-invoice counterpart to :func:`generate_dunning` — used by the invoice
    drawer's *Send reminder* action. Picks the highest dunning stage the invoice's
    days-overdue qualifies for (or the gentlest stage if it isn't overdue yet), and
    reuses the existing notice for that ``(invoice, level)`` so the unique pair is
    never violated; a previously cancelled/resolved notice is reactivated to PENDING.
    Returns the notice. Raises :class:`PostingError` if there is nothing to remind.
    """
    from .models import DunningNotice

    if invoice.status != DocumentStatus.POSTED:
        raise PostingError("Only a posted invoice can be reminded.")
    if invoice.balance_due <= 0:
        raise PostingError("This invoice has no outstanding balance to remind on.")

    policy = ensure_default_policy(invoice.entity)
    stages = list(policy.stages.order_by("min_days_overdue", "level"))
    if not stages:
        raise PostingError(f"Dunning policy '{policy.name}' has no stages defined.")

    as_of = timezone.now().date()
    due = invoice.due_date or invoice.invoice_date
    days_overdue = max((as_of - due).days, 0)
    stage = _stage_for(stages, days_overdue) or stages[0]

    notice, created = DunningNotice.objects.get_or_create(
        invoice=invoice, level=stage.level,
        defaults={
            "entity": invoice.entity, "branch": invoice.branch,
            "customer": invoice.customer, "policy": policy, "stage": stage,
            "notice_date": as_of, "days_overdue": days_overdue,
            "amount_due": invoice.balance_due, "channel": stage.channel,
            "message": message or stage.message,
            "notice_status": DunningNoticeStatus.PENDING, "created_by": actor_user,
        },
    )
    if not created:
        notice.days_overdue = days_overdue
        notice.amount_due = invoice.balance_due
        if notice.notice_status in (DunningNoticeStatus.CANCELLED, DunningNoticeStatus.RESOLVED):
            notice.notice_status = DunningNoticeStatus.PENDING
            notice.sent_at = None
        notice.save(update_fields=["days_overdue", "amount_due", "notice_status", "sent_at", "updated_at"])

    if send:
        mark_notice_sent(notice, actor_user=actor_user)
    notice.refresh_from_db()
    return notice


def _resolve_settled(entity, *, actor_user=None):
    """Flip any PENDING/SENT notice whose invoice is now fully paid to RESOLVED."""
    from .models import DunningNotice

    open_notices = DunningNotice.objects.filter(
        entity=entity,
        notice_status__in=[DunningNoticeStatus.PENDING, DunningNoticeStatus.SENT],
    ).select_related("invoice")
    for notice in open_notices:
        if notice.invoice.balance_due <= 0:
            notice.notice_status = DunningNoticeStatus.RESOLVED
            notice.save(update_fields=["notice_status", "updated_at"])


def _dispatch_notice(notice, *, actor_user=None):
    """Deliver a dunning ``notice`` through **vs_notifications** — never directly.

    Routing all delivery through the notification system keeps vs_finance out of the
    email business: it fires the ``billing.invoice_overdue`` event through the
    consolidated :func:`vs_notifications.notify.send_notification` API, with a context
    carrying every variable the templates reference (plus the policy stage's
    ``reminder_message`` so the escalation wording comes from the dunning policy, not a
    per-level event), targeting the customer's ``billing_email`` as an
    :class:`~vs_notifications.notify.UnregisteredRecipient`.

    Notifications are **recipient-centric**: ``school`` is an optional scope, not a
    gate. A platform/product book (``entity.source_school is None``) still delivers to
    the customer's billing email — the school only resolves settings overrides and
    record attribution, so we pass it through as-is (possibly ``None``).

    * **vs_notifications unavailable** (ImportError) — logs and returns ``None``.

    A genuine :class:`~vs_notifications.exceptions.UnknownEventTypeError` (the event key
    isn't seeded/active) propagates — that is a deploy-config error the caller decides
    how to handle. Returns the ``send_notification`` result (list of ids).
    """
    from .money import to_naira

    try:
        from vs_notifications.notify import send_notification, UnregisteredRecipient
    except ImportError:
        logger.warning(
            "vs_notifications unavailable; dunning notice %s not delivered.",
            notice.document_number or notice.pk,
        )
        return None

    school = notice.entity.source_school  # optional scope; may be None (platform books)
    customer = notice.customer
    invoice = notice.invoice
    context = {
        "student_first_name": customer.name,
        "student_last_name": "",
        "invoice_number": invoice.document_number,
        "amount_outstanding": f"{to_naira(notice.amount_due):,.2f}",
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "—",
        "days_overdue": notice.days_overdue,
        "school_name": school.name if school else "",
        # Escalation wording is owned by the dunning policy stage, not the event.
        "reminder_message": notice.message,
        "level": notice.level,
    }

    return send_notification(
        event_key="billing.invoice_overdue",
        context=context,
        recipients=[],
        school=school,
        unregistered_recipients=[
            UnregisteredRecipient(
                email=customer.billing_email or "", name=customer.name,
            ),
        ],
    )


def mark_notice_sent(notice, *, actor_user=None):
    """Deliver a PENDING notice through vs_notifications, then record it SENT.

    Idempotent once sent. Delivery runs **before** the SENT flip, so a dispatch
    failure leaves the notice PENDING for the next run to retry rather than falsely
    marking it sent. Delivery is recipient-centric (works with or without a school);
    a vs_notifications-unavailable skip still flips SENT (nothing to retry there).
    """
    from .models import DunningNotice  # noqa: F401  (typing/clarity)

    if notice.notice_status == DunningNoticeStatus.SENT:
        return notice
    if notice.notice_status != DunningNoticeStatus.PENDING:
        raise PostingError(
            f"Notice {notice.document_number} is '{notice.notice_status}'; "
            f"only a pending notice can be marked sent.",
        )

    _dispatch_notice(notice, actor_user=actor_user)

    notice.notice_status = DunningNoticeStatus.SENT
    notice.sent_at = timezone.now()
    notice.save(update_fields=["notice_status", "sent_at", "updated_at"])
    record(
        entity=notice.entity, action=FinanceAuditAction.DUNNING_NOTICE_SENT,
        actor_user=actor_user, target=notice,
        message=f"Dunning notice {notice.document_number} (L{notice.level}) sent to "
                f"{notice.customer.code} via {notice.channel}.",
        level=notice.level, channel=notice.channel,
    )
    return notice


def cancel_notice(notice, *, reason="", actor_user=None):
    """Withdraw a notice before/after sending. Idempotent on terminal states."""
    if notice.notice_status in (DunningNoticeStatus.CANCELLED, DunningNoticeStatus.RESOLVED):
        return notice
    notice.notice_status = DunningNoticeStatus.CANCELLED
    notice.save(update_fields=["notice_status", "updated_at"])
    record(
        entity=notice.entity, action=FinanceAuditAction.DUNNING_NOTICE_CANCELLED,
        actor_user=actor_user, target=notice,
        message=f"Dunning notice {notice.document_number} cancelled."
                + (f" Reason: {reason}" if reason else ""),
        level=notice.level,
    )
    return notice
