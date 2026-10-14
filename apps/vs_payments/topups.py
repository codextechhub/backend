"""Topping up the platform's provider balance from its own bank.

Paystack's "top up balance" raises the platform's single provider balance, to
fund its own payouts for example. The money comes from a platform bank account
and belongs to nobody but the platform, so it is a document of the platform's
own books and of nobody else's: :class:`~vs_payments.models.ProviderBalanceTopUp`.

A platform finance operator writes it down and puts it forward
(:func:`submit_topup`); two other platform people in the settlement approver
group approve it through the workflow engine
(:mod:`vs_payments.workflow_handlers`), exactly as they approve a held
settlement. On the second approval it books (:func:`post_topup`), in the
platform's books and nowhere else:

    Dr provider balance (``PROVIDER_BALANCE``, 1127)     the amount
    Cr the platform bank account the money left          the amount

dated the day the money left the bank, in that bank account's branch, and only
into a month that branch has open.

The provider balance account normally mirrors what the platform holds for client
branches. A top-up raises that account and the provider's own balance together
and is owed to no client, so :func:`topped_up_total` is what the daily
held-ledger check sets aside when it asks whether the account still equals the
held-funds sub-ledger (:mod:`vs_payments.held_reconciliation`). The platform's
own share of the provider balance (:func:`vs_payments.held.own_share_available`)
is read from the provider's real balance, so it rises with a top-up without any
change here.

Ngozi Eze, a platform finance operator, records that Codex paid N2,000,000 from
its Zenith account into Paystack on 12 October and puts it forward. Her
colleague Chioma approves it, and Bola approves it second. Ngozi approving it
herself, or Chioma approving both steps, books nothing. Once Bola approves, the
platform's books show Dr provider balance and Cr Zenith for N2,000,000, and the
next held-ledger check expects N2,000,000 more at Paystack than the clients' held
money explains.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import audit
from .constants import (
    TOPUP_REQUIRED_APPROVERS,
    PaymentAuditAction,
    ProviderTopUpStatus,
)
from .exceptions import PaymentStateError
from .models import ProviderBalanceTopUp

#: The statuses from which a top-up can still be changed or put forward.
EDITABLE = (ProviderTopUpStatus.DRAFT,)

#: The statuses a top-up can be booked from: put forward, or approved a moment ago.
POSTABLE = (ProviderTopUpStatus.PENDING_APPROVAL, ProviderTopUpStatus.APPROVED)


class TopUpApprovalError(PaymentStateError):
    """A top-up has not been approved by two distinct people other than its requester (409)."""

    error_code = "TOPUP_APPROVERS_REQUIRED"
    default_message = "A provider balance top-up needs two distinct approvers."


def _platform():
    """The platform's books, or a refusal where they are not set up."""
    from .held import platform_books

    platform = platform_books()
    if platform is None:
        raise PaymentStateError("The platform's books are not set up.")
    return platform


def _today(platform) -> datetime.date:
    from vs_config.clock import tenant_today

    return tenant_today(platform.tenant)


def topup_branch_id(bank_account) -> int | None:
    """The branch a top-up from ``bank_account`` belongs to: the account's, else the platform's main branch."""
    from .held import _platform_branch_id

    return bank_account.branch_id or _platform_branch_id(bank_account.entity)


def validate_topup(topup, *, today=None) -> None:
    """Refuse a top-up whose figures or bank account the platform's books cannot take.

    The amount is positive kobo, the bank account is an active account of the
    platform's own books, the day is not in the platform's future, and an
    unrejected top-up of the provider with the same reference is not already on
    record: the same transfer is written down once. Writes nothing.
    """
    platform = _platform()
    errors = {}
    if not isinstance(topup.amount, int) or isinstance(topup.amount, bool) or topup.amount <= 0:
        errors["amount"] = "The amount must be more than nothing."
    bank = topup.bank_account
    if bank.entity_id != platform.pk or not bank.is_active:
        errors["bank_account"] = "Choose one of the platform's own active bank accounts."
    if topup.topped_up_on > (today or _today(platform)):
        errors["topped_up_on"] = "The day of a top-up cannot be in the future."
    if topup.reference:
        clash = (ProviderBalanceTopUp.objects.filter(provider=topup.provider, reference=topup.reference)
                 .exclude(status=ProviderTopUpStatus.REJECTED).exclude(pk=topup.pk))
        if clash.exists():
            errors["reference"] = "A top-up with this reference is already recorded."
    if errors:
        raise ValidationError(errors)


def preflight(topup) -> None:
    """Every check booking ``topup`` would make that needs no write.

    Run when it is put forward and again when a returned one is resumed, so an
    approver is not asked to decide on a top-up whose month its branch has
    closed since it was written down.
    """
    from vs_finance.posting import ensure_date_open

    validate_topup(topup)
    ensure_date_open(topup.entity, topup.topped_up_on, branch=topup.branch_id)


@transaction.atomic
def create_topup(*, data: dict, actor_user) -> ProviderBalanceTopUp:
    """Write down a top-up as a draft. ``data`` holds the validated fields; returns the row.

    ``provider`` is resolved (default Paystack) before anything is written. The
    row is the platform's, in the branch of the bank account named.
    """
    from .services import resolve_provider_name

    platform = _platform()
    bank = data["bank_account"]
    topup = ProviderBalanceTopUp(
        entity=platform, branch_id=topup_branch_id(bank),
        provider=resolve_provider_name(data.get("provider")),
        amount=data["amount"], topped_up_on=data["topped_up_on"], bank_account=bank,
        reference=(data.get("reference") or "").strip(), note=(data.get("note") or "").strip(),
        created_by=actor_user,
    )
    validate_topup(topup)
    topup.save()
    audit.record(
        action=PaymentAuditAction.PROVIDER_TOPUP_CREATED, entity=platform, provider=topup.provider,
        reference=_event_reference(topup), actor_user=actor_user,
        message="A provider balance top-up was recorded.", metadata=_event_metadata(topup),
    )
    return topup


@transaction.atomic
def update_topup(topup, *, data: dict, actor_user) -> ProviderBalanceTopUp:
    """Change the fields of a draft top-up that ``data`` names; any other status is refused (409)."""
    from .services import resolve_provider_name

    topup = ProviderBalanceTopUp.objects.select_for_update().select_related(
        "entity", "bank_account").get(pk=topup.pk)
    if topup.status not in EDITABLE:
        raise PaymentStateError("Only a draft top-up can be changed.")
    if "provider" in data:
        topup.provider = resolve_provider_name(data["provider"])
    for name in ("amount", "topped_up_on", "bank_account"):
        if name in data:
            setattr(topup, name, data[name])
    for name in ("reference", "note"):
        if name in data:
            setattr(topup, name, (data[name] or "").strip())
    topup.branch_id = topup_branch_id(topup.bank_account)
    validate_topup(topup)
    topup.save()
    return topup


@transaction.atomic
def submit_topup(topup, *, requested_by):
    """Put a draft top-up forward for the platform's two-person approval; returns its workflow instance.

    ``requested_by`` is a platform finance operator: the workflow engine refuses
    anybody outside the platform tenant, and the requester can never approve.
    The platform's top-up route is published first if it is missing
    (:func:`vs_payments.approvals.ensure_topup_approval_template`). Replaying a
    submitted top-up returns its existing approval.
    """
    from vs_workflow.models import WorkflowInstance
    from vs_workflow.services.submission import submit_for_approval

    from .approvals import ensure_topup_approval_template

    locked = ProviderBalanceTopUp.objects.select_for_update().select_related(
        "entity__tenant", "bank_account").get(pk=topup.pk)
    if locked.status == ProviderTopUpStatus.PENDING_APPROVAL:
        existing = (WorkflowInstance.all_objects.for_document(locked)
                    .order_by("-created_at", "-id").first())
        if existing is not None:
            return existing
    if locked.status != ProviderTopUpStatus.DRAFT:
        raise PaymentStateError("Only a draft top-up can be put forward for approval.")
    ensure_topup_approval_template(locked.entity.tenant)
    instance = submit_for_approval(locked, requested_by=requested_by)
    audit.record(
        action=PaymentAuditAction.PROVIDER_TOPUP_SUBMITTED, entity=locked.entity,
        provider=locked.provider, reference=_event_reference(locked), actor_user=requested_by,
        message="A provider balance top-up was put forward for approval.",
        metadata=_event_metadata(locked),
    )
    return instance


def distinct_approvers(instance) -> int:
    """How many real people have approved ``instance``, leaving out everyone who put it forward.

    Each live approving vote counts for the person who cast it: its
    ``proxied_by`` when cast under a proxy (an impersonation session), else its
    actor. Ada who submits a top-up and approves it as Chioma and as Bola by
    proxy is one person and the requester, so the top-up counts no approver.
    """
    from vs_workflow.constants import WorkflowStageAction as StageActionEnum
    from vs_workflow.models import WorkflowStageAction
    from vs_workflow.services.approvers import requester_ids

    voters = set(
        WorkflowStageAction.objects.filter(
            stage_instance__instance=instance, action=StageActionEnum.APPROVED,
            reversed_at__isnull=True, is_reversal_of__isnull=True,
        ).annotate(real_voter=Coalesce("proxied_by", "actor"))
        .values_list("real_voter", flat=True)
    )
    return len(voters - requester_ids(instance))


def require_distinct_approvers(instance) -> None:
    """Refuse (409) unless two real people besides the requester have approved ``instance``."""
    count = distinct_approvers(instance)
    if count < TOPUP_REQUIRED_APPROVERS:
        raise TopUpApprovalError(
            "A provider balance top-up needs two distinct approvers besides the person who "
            "put it forward.",
            required_approvers=TOPUP_REQUIRED_APPROVERS, distinct_approved_actors=count,
        )


@transaction.atomic
def post_topup(topup, *, actor_user=None) -> ProviderBalanceTopUp:
    """Book an approved top-up in the platform's books and mark it posted; returns the row.

    Dr provider balance, Cr the bank account the money left, on the top-up's own
    day in its branch. A closed month, an inactive account or an unbalanced
    posting raises, and the caller's transaction (the approval that booked it)
    rolls back with it, so nothing is half done. Booking twice is refused.
    """
    from vs_finance.constants import JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    from .held import ensure_platform_accounts

    topup = (ProviderBalanceTopUp.objects.select_for_update()
             .select_related("entity", "bank_account__gl_account").get(pk=topup.pk))
    if topup.status not in POSTABLE:
        raise PaymentStateError("This top-up is not waiting to be booked.")
    validate_topup(topup)
    platform = topup.entity
    _, provider_account, _ = ensure_platform_accounts(platform)
    bank = topup.bank_account
    entry = JournalEntry.objects.create(
        entity=platform, branch_id=topup.branch_id, date=topup.topped_up_on,
        period=resolve_period(platform, topup.topped_up_on), source=JournalSource.BANK,
        narration=f"{topup.get_provider_display()} balance top-up from {bank.name}"[:255],
        reference=topup.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=provider_account, line_no=1, debit=topup.amount, credit=0,
        description="Provider balance")
    JournalLine.objects.create(
        entry=entry, account=bank.gl_account, line_no=2, debit=0, credit=topup.amount,
        description=f"Top-up of the {topup.get_provider_display()} balance")
    post_journal(entry, actor_user=actor_user)

    topup.status = ProviderTopUpStatus.POSTED
    topup.journal = entry
    topup.posted_at = timezone.now()
    topup.save(update_fields=["status", "journal", "posted_at", "updated_at"])
    audit.record(
        action=PaymentAuditAction.PROVIDER_TOPUP_POSTED, entity=platform, provider=topup.provider,
        reference=_event_reference(topup), actor_user=actor_user,
        message="A provider balance top-up was booked.",
        metadata={**_event_metadata(topup), "journal_id": entry.pk},
    )
    return topup


def topped_up_total(provider_name: str, currency: str = "NGN") -> int:
    """Kobo of every booked top-up of ``provider_name`` whose journal still stands.

    A top-up whose journal was later reversed is netted out of the provider
    balance account by the reversal, so it no longer counts here either.
    """
    from vs_finance.constants import DocumentStatus

    total = (ProviderBalanceTopUp.objects.filter(
        provider=provider_name, currency=currency.upper(),
        status=ProviderTopUpStatus.POSTED, journal__status=DocumentStatus.POSTED,
    ).aggregate(total=Sum("amount"))["total"])
    return int(total or 0)


def _event_reference(topup) -> str:
    """The reference a top-up's gateway events are filed under."""
    return (topup.reference or f"TOPUP-{topup.pk}")[:64]


def _event_metadata(topup) -> dict:
    """What a top-up's gateway events carry: the amount and the branch it belongs to."""
    return {"topup_id": topup.pk, "amount": int(topup.amount), "branch_id": topup.branch_id}
