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
from __future__ import annotations  # Defer annotation evaluation during app import.

import datetime  # Kept for date-related dunning semantics and compatibility.
import logging  # Used for best-effort notification delivery logging.

from django.db import transaction  # Keeps dunning mutations atomic.
from django.utils import timezone  # Supplies run dates and sent timestamps.

from .audit import record  # Writes finance audit events.
from .constants import (
    DocumentStatus,  # Invoice lifecycle status.
    DunningChannel,  # Reminder delivery channel enum.
    DunningNoticeStatus,  # Reminder lifecycle status.
    FinanceAuditAction,  # Audit action enum values.
)
from .exceptions import PostingError  # Domain error for invalid dunning operations.

logger = logging.getLogger(__name__)  # Module logger for notification dispatch failures.


#: A sensible out-of-the-box ladder: (level, name, min_days_overdue, channel).
DEFAULT_STAGES = (  # Standard reminder ladder used when no policy exists.
    (1, "Friendly reminder", 1, DunningChannel.EMAIL),  # First overdue-day reminder.
    (2, "Second reminder", 14, DunningChannel.EMAIL),  # Escalation after two weeks.
    (3, "Final notice", 30, DunningChannel.EMAIL),  # Final escalation after thirty days.
)


def ensure_default_policy(entity, *, name="Standard reminders"):  # Ensure an entity has a default dunning policy.
    """Return ``entity``'s default dunning policy, creating a standard ladder if absent.

    Idempotent: if a default policy already exists it is returned untouched; otherwise a
    new default policy with the :data:`DEFAULT_STAGES` ladder is created.
    """
    from .models import DunningPolicy, DunningStage  # Local import avoids model import cycles.

    existing = DunningPolicy.objects.filter(entity=entity, is_default=True).first()  # Look for existing default policy.
    if existing is not None:  # Keep existing policy untouched.
        return existing

    policy = DunningPolicy.objects.create(  # Create the default policy header.
        entity=entity, name=name, is_active=True, is_default=True,  # Mark it active and default.
    )
    DunningStage.objects.bulk_create([  # Create the standard ladder efficiently.
        DunningStage(  # One reminder stage row.
            policy=policy, level=level, name=stage_name,  # Attach policy and stage identity.
            min_days_overdue=days, channel=channel,  # Store trigger threshold and channel.
            message=f"{stage_name}: your account has an overdue balance.",  # Default reminder wording.
        )
        for level, stage_name, days, channel in DEFAULT_STAGES  # Expand each default stage tuple.
    ])
    return policy  # Return the newly created default policy.


def _resolve_policy(entity, policy=None):  # Choose which dunning policy a run should use.
    """Pick the policy to run: the one passed, else the entity's active default."""
    from .models import DunningPolicy  # Local import avoids model import cycles.

    if policy is not None:  # Explicit policy wins.
        return policy
    chosen = (  # Prefer active default, otherwise the first active policy.
        DunningPolicy.objects.filter(entity=entity, is_default=True, is_active=True).first()  # Active default policy.
        or DunningPolicy.objects.filter(entity=entity, is_active=True).order_by("id").first()  # Fallback active policy.
    )
    if chosen is None:  # Dunning cannot run without a stage policy.
        raise PostingError(
            "No active dunning policy for this entity. Create one (or call "
            "ensure_default_policy) before generating reminders.",
        )
    return chosen  # Return the selected active policy.


def _stage_for(stages, days_overdue: int):  # Find the highest threshold reached by an overdue invoice.
    """Highest stage whose ``min_days_overdue`` is met by ``days_overdue`` (or ``None``)."""
    match = None  # No stage qualifies until a threshold is met.
    for stage in stages:  # stages pre-sorted ascending by min_days_overdue
        if days_overdue >= stage.min_days_overdue:  # This threshold has been reached.
            match = stage  # Keep walking so the highest qualifying stage wins.
        else:  # Later stages have higher thresholds and cannot qualify.
            break
    return match  # Return highest qualifying stage or None.


@transaction.atomic
def generate_dunning(entity, *, as_of=None, policy=None, customer=None, actor_user=None):  # Generate overdue invoice reminders.
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
    from collections import defaultdict  # Tracks issued levels per invoice.
    from .models import DunningNotice, Invoice  # Reminder and invoice models.

    as_of = as_of or timezone.now().date()  # Default run date to today.
    policy = _resolve_policy(entity, policy)  # Choose the policy for this run.
    stages = list(policy.stages.order_by("min_days_overdue", "level"))  # Load stages in threshold order.
    if not stages:  # A policy with no ladder cannot generate notices.
        raise PostingError(f"Dunning policy '{policy.name}' has no stages defined.")

    # Narrow to overdue, still-owing invoices in SQL (not in Python): balance_due is a
    # property, so annotate it and the effective due date and filter on them — the loop
    # then only loads invoices that can actually qualify for a stage.  # Keep the scan efficient.
    from django.db.models import F  # Builds database-side balance expression.
    from django.db.models.functions import Coalesce  # Uses due_date when present, otherwise invoice_date.

    balance = F("total") - F("amount_paid") - F("amount_credited")  # Outstanding invoice balance expression.
    invoices = (  # Base overdue invoice queryset.
        Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Posted invoices for this entity.
        .annotate(_balance=balance, _due=Coalesce("due_date", "invoice_date"))  # Add balance and effective due date.
        .filter(_balance__gt=0, _due__lt=as_of)  # Keep only overdue invoices with remaining balance.
        .select_related("customer")  # Load customer for notice creation without N+1.
    )
    if customer is not None:  # Optional targeted customer run.
        invoices = invoices.filter(customer=customer)  # Restrict reminders to one customer.

    # Resolve any outstanding reminders whose invoice is now fully settled.  # Keeps notice lifecycle current.
    _resolve_settled(entity, actor_user=actor_user)  # Mark settled invoice notices resolved.

    invoice_list = list(invoices)  # Materialize once for reuse in bulk notice lookups.
    # Pre-load (two queries, no per-invoice N+1): which levels each invoice has already
    # been issued, and which invoices were already advanced on this run date.  # Preserve idempotency.
    issued_levels: dict[int, set] = defaultdict(set)  # Map invoice id to previously issued levels.
    issued_today: set = set()  # Invoice ids already advanced on this run date.
    if invoice_list:  # Avoid querying notices when no invoices qualified.
        for inv_id, lvl, ndate in DunningNotice.objects.filter(  # Load existing notices for candidate invoices.
            invoice_id__in=[i.id for i in invoice_list],  # Restrict to candidate invoice ids.
        ).values_list("invoice_id", "level", "notice_date"):
            issued_levels[inv_id].add(lvl)  # Remember that level was already issued.
            if ndate == as_of:  # Same-day reruns should not advance another rung.
                issued_today.add(inv_id)  # Mark invoice as already advanced today.

    stages_by_level = sorted(stages, key=lambda s: s.level)  # Escalation order is stage level, not threshold order.
    created = []  # Newly created notices returned to caller.
    for invoice in invoice_list:  # Evaluate each overdue invoice.
        if invoice.id in issued_today:  # Same invoice already got one notice today.
            continue  # already advanced one rung on this run date
        days_overdue = (as_of - (invoice.due_date or invoice.invoice_date)).days  # Compute overdue age.
        already = issued_levels[invoice.id]  # Levels already issued for this invoice.
        # Next rung: the lowest-level qualifying stage not yet issued for this invoice.  # Prevent skipped escalation.
        stage = next(  # Find one stage to issue this run.
            (s for s in stages_by_level  # Walk in escalation order.
             if s.min_days_overdue <= days_overdue and s.level not in already),  # Qualifies and not issued before.
            None,  # No new stage qualifies.
        )
        if stage is None:  # Invoice has no new rung to receive.
            continue

        notice = DunningNotice.objects.create(  # Create the pending reminder notice.
            entity=entity, branch=invoice.branch, customer=invoice.customer,  # Scope and recipient context.
            invoice=invoice, policy=policy, stage=stage, level=stage.level,  # Link invoice and selected policy stage.
            notice_date=as_of, days_overdue=days_overdue, amount_due=invoice.balance_due,  # Store run date, age, and balance.
            channel=stage.channel, message=stage.message,  # Copy delivery channel and stage wording.
            notice_status=DunningNoticeStatus.PENDING,  # Delivery has not happened yet.
            created_by=actor_user,  # Attribute notice creation to the caller.
        )
        created.append(notice)  # Include notice in the result list.

    record(  # Audit the dunning run summary.
        entity=entity, action=FinanceAuditAction.DUNNING_RUN_GENERATED,  # Audit action for dunning generation.
        actor_user=actor_user, target=policy,  # Actor and policy context.
        message=f"Generated {len(created)} dunning notice(s) under '{policy.name}' "  # Human-readable summary.
                f"as at {as_of}.",  # Include run date.
        policy_id=policy.pk, as_of=str(as_of), notices_created=len(created),  # Structured run metadata.
    )
    return created  # Return newly created notices.


@transaction.atomic
def remind_invoice(invoice, *, actor_user=None, send=True, message=""):  # Create/send one invoice reminder.
    """Raise (and, by default, send) a dunning reminder for a single invoice.

    The per-invoice counterpart to :func:`generate_dunning` — used by the invoice
    drawer's *Send reminder* action. Picks the highest dunning stage the invoice's
    days-overdue qualifies for (or the gentlest stage if it isn't overdue yet), and
    reuses the existing notice for that ``(invoice, level)`` so the unique pair is
    never violated; a previously cancelled/resolved notice is reactivated to PENDING.
    Returns the notice. Raises :class:`PostingError` if there is nothing to remind.
    """
    from .models import DunningNotice  # Local import avoids model import cycles.

    if invoice.status != DocumentStatus.POSTED:  # Draft/cancelled invoices should not be reminded.
        raise PostingError("Only a posted invoice can be reminded.")
    if invoice.balance_due <= 0:  # Fully settled invoices have nothing to chase.
        raise PostingError("This invoice has no outstanding balance to remind on.")

    policy = ensure_default_policy(invoice.entity)  # Ensure a usable policy exists.
    stages = list(policy.stages.order_by("min_days_overdue", "level"))  # Load stages in threshold order.
    if not stages:  # A policy with no ladder cannot generate a reminder.
        raise PostingError(f"Dunning policy '{policy.name}' has no stages defined.")

    as_of = timezone.now().date()  # Use today's date for manual reminder.
    due = invoice.due_date or invoice.invoice_date  # Fall back to invoice date when no due date exists.
    days_overdue = max((as_of - due).days, 0)  # Do not report negative overdue days.
    stage = _stage_for(stages, days_overdue) or stages[0]  # Use qualifying stage or gentlest stage.

    notice, created = DunningNotice.objects.get_or_create(  # Reuse notice for the invoice/level pair when present.
        invoice=invoice, level=stage.level,  # Unique reminder rung per invoice.
        defaults={  # Fields for a newly created manual reminder.
            "entity": invoice.entity, "branch": invoice.branch,  # Scope context.
            "customer": invoice.customer, "policy": policy, "stage": stage,  # Recipient and policy stage.
            "notice_date": as_of, "days_overdue": days_overdue,  # Date and overdue age.
            "amount_due": invoice.balance_due, "channel": stage.channel,  # Balance and delivery channel.
            "message": message or stage.message,  # Custom message overrides stage message.
            "notice_status": DunningNoticeStatus.PENDING, "created_by": actor_user,  # Pending status and actor.
        },
    )
    if not created:  # Existing notice gets refreshed/reactivated.
        notice.days_overdue = days_overdue  # Update overdue age.
        notice.amount_due = invoice.balance_due  # Update outstanding amount.
        if notice.notice_status in (DunningNoticeStatus.CANCELLED, DunningNoticeStatus.RESOLVED):  # Terminal notice can be reused.
            notice.notice_status = DunningNoticeStatus.PENDING  # Reactivate for delivery.
            notice.sent_at = None  # Clear old sent timestamp when reactivated.
        notice.save(update_fields=["days_overdue", "amount_due", "notice_status", "sent_at", "updated_at"])  # Persist refresh.

    if send:  # Caller can create-only or create-and-send.
        mark_notice_sent(notice, actor_user=actor_user)  # Dispatch and mark sent.
    notice.refresh_from_db()  # Reload final sent status/timestamp.
    return notice  # Return the reminder notice.


def _resolve_settled(entity, *, actor_user=None):  # Resolve open notices for invoices now fully paid.
    """Flip any PENDING/SENT notice whose invoice is now fully paid to RESOLVED."""
    from .models import DunningNotice  # Local import avoids model import cycles.

    open_notices = DunningNotice.objects.filter(  # Load unresolved notices for the entity.
        entity=entity,  # Scope by entity.
        notice_status__in=[DunningNoticeStatus.PENDING, DunningNoticeStatus.SENT],  # Only active reminders can resolve.
    ).select_related("invoice")
    for notice in open_notices:  # Check each notice's linked invoice balance.
        if notice.invoice.balance_due <= 0:  # Fully settled invoice no longer needs reminders.
            notice.notice_status = DunningNoticeStatus.RESOLVED  # Mark the notice resolved.
            notice.save(update_fields=["notice_status", "updated_at"])  # Persist status change.


def _dispatch_notice(notice, *, actor_user=None):  # Send one notice through vs_notifications.
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

    **Best-effort:** delivery is handed off to vs_notifications, which owns its own
    delivery tracking and email retries. Any problem here (notifications app absent, an
    unseeded/inactive event, a template render error) is logged and swallowed — it must
    not break the dunning ladder or leave a notice wedged in PENDING forever. Returns
    the ``send_notification`` result (list of ids), or ``None`` when delivery was
    skipped/failed.
    """
    from .money import to_naira  # Convert integer kobo to decimal naira for templates.

    try:  # Delivery is best-effort and must not break dunning lifecycle.
        from vs_notifications.notify import send_notification, UnregisteredRecipient  # Platform notification API.

        school = notice.entity.source_school  # optional scope; may be None (platform books)  # Scope settings when present.
        customer = notice.customer  # Recipient information.
        invoice = notice.invoice  # Invoice context for the template.
        context = {  # Template variables for overdue invoice event.
            "customer_name": customer.name,  # Customer display name.
            "invoice_number": invoice.document_number,  # Posted invoice number.
            "amount_outstanding": f"{to_naira(notice.amount_due):,.2f}",  # Human-readable outstanding amount.
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "—",  # ISO due date or dash.
            "days_overdue": notice.days_overdue,  # Overdue age for template copy.
            "school_name": school.name if school else "",  # Optional school display name.
            # Escalation wording is owned by the dunning policy stage, not the event.  # Keep wording configurable.
            "reminder_message": notice.message,  # Stage-specific reminder text.
            "level": notice.level,  # Dunning escalation level.
        }
        return send_notification(  # Delegate delivery to vs_notifications.
            event_key="billing.invoice_overdue",  # Event key configured in notification templates.
            context=context,  # Render variables for template.
            recipients=[],  # No registered portal recipients are targeted here.
            school=school,  # Optional school scope.
            unregistered_recipients=[  # Billing emails can receive without portal accounts.
                UnregisteredRecipient(  # Customer recipient payload.
                    email=customer.billing_email or "", name=customer.name,  # Recipient email and display name.
                ),
            ],
        )
    except Exception:  # best-effort — a notification problem must not break dunning
        logger.warning(  # Log failure with stack trace for operations.
            "Dunning notice %s delivery failed; marking sent anyway "  # Explain lifecycle still advances.
            "(vs_notifications owns delivery tracking/retries).",  # Ownership of retries.
            notice.document_number or notice.pk, exc_info=True,  # Prefer document number, fallback to pk.
        )
        return None  # Swallow failures so notice can still move to SENT.


def mark_notice_sent(notice, *, actor_user=None):  # Dispatch and mark a pending notice sent.
    """Deliver a PENDING notice through vs_notifications, then record it SENT.

    Idempotent once sent. Delivery is handed to vs_notifications (best-effort — see
    :func:`_dispatch_notice`) before the SENT flip; a delivery problem is logged there
    and the notice is still marked sent, since vs_notifications owns delivery tracking
    and its own email retries. Delivery is recipient-centric (works with or without a
    school).
    """
    from .models import DunningNotice  # noqa: F401  (typing/clarity)  # Keeps model dependency explicit.

    if notice.notice_status == DunningNoticeStatus.SENT:  # Already sent is idempotent success.
        return notice
    if notice.notice_status != DunningNoticeStatus.PENDING:  # Only pending notices can be sent.
        raise PostingError(
            f"Notice {notice.document_number} is '{notice.notice_status}'; "
            f"only a pending notice can be marked sent.",
        )

    _dispatch_notice(notice, actor_user=actor_user)  # Best-effort delivery through notifications app.

    notice.notice_status = DunningNoticeStatus.SENT  # Advance notice lifecycle.
    notice.sent_at = timezone.now()  # Stamp delivery handoff time.
    notice.save(update_fields=["notice_status", "sent_at", "updated_at"])  # Persist sent fields.
    record(  # Audit the sent notice.
        entity=notice.entity, action=FinanceAuditAction.DUNNING_NOTICE_SENT,  # Audit action for sent notice.
        actor_user=actor_user, target=notice,  # Actor and target context.
        message=f"Dunning notice {notice.document_number} (L{notice.level}) sent to "  # Human-readable sent message.
                f"{notice.customer.code} via {notice.channel}.",  # Include customer and channel.
        level=notice.level, channel=notice.channel,  # Structured audit metadata.
    )
    return notice  # Return the sent notice.


def cancel_notice(notice, *, reason="", actor_user=None):  # Cancel an active reminder notice.
    """Withdraw a notice before/after sending. Idempotent on terminal states."""
    if notice.notice_status in (DunningNoticeStatus.CANCELLED, DunningNoticeStatus.RESOLVED):  # Terminal states stay unchanged.
        return notice
    notice.notice_status = DunningNoticeStatus.CANCELLED  # Mark notice withdrawn.
    notice.save(update_fields=["notice_status", "updated_at"])  # Persist cancellation status.
    record(  # Audit the cancellation.
        entity=notice.entity, action=FinanceAuditAction.DUNNING_NOTICE_CANCELLED,  # Audit action for cancellation.
        actor_user=actor_user, target=notice,  # Actor and target context.
        message=f"Dunning notice {notice.document_number} cancelled."  # Human-readable cancellation message.
                + (f" Reason: {reason}" if reason else ""),  # Append optional reason.
        level=notice.level,  # Structured escalation level.
    )
    return notice  # Return the cancelled notice.
