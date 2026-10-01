"""Held custody: the money the platform keeps for a tenant's branches, and paying it on.

A held-mode tenant's online payments settle to the platform's own provider balance
(:mod:`vs_payments.custody`). Two sets of books then have to agree about it:

* **The tenant's books** debit gateway clearing when a payment is confirmed
  (:func:`vs_payments.services._book_receipt`), and empty it when the platform
  pays the branch: Dr bank, Dr bank charges, Cr gateway clearing.
* **The platform's books** (the ``PLATFORM`` ledger entity of the platform
  tenant, kept in its own branch) owe the money to the branch. A confirmed
  payment books Dr provider balance (``PROVIDER_BALANCE``), Cr client funds held
  (``CLIENT_FUNDS_HELD``) for what reached the balance, the payment less the
  provider's fee; a settlement or an online payout for the branch books the
  reverse for what it transfers.

What the platform holds for each branch is kept as a sub-ledger:
:class:`~vs_payments.models.HeldMovement` rows keyed by tenant and branch, summed on
one :class:`~vs_payments.models.HeldBalance` row per branch. The sub-ledger is the
authority a payout is checked against; the platform's journal mirrors each row and
is posted with it whenever the platform's books can take it, or later by
:func:`post_pending_platform_journals`. Nothing that records money already moved
is ever refused because the platform's books were not ready.

Bright Star Lekki takes N180,000 from Mrs Adeyemi; Paystack keeps N2,000. Lekki's
books show N180,000 in clearing; the platform's show N178,000 held for Bright Star
Lekki. Lekki pays a supplier N50,000 online: the payout is checked against the
N178,000 under a lock on Lekki's balance, the balance drops to N128,000 when the
transfer is sent, and Lekki's books credit clearing, since the money left the
provider balance and not Lekki's bank. Next morning the settlement run prepares
the platform's payout of that N128,000 to Lekki's Zenith account, less Paystack's
N50 transfer fee, which Lekki bears. A platform finance operator puts it forward
and two platform people approve it; once Paystack confirms the transfer, Lekki's
books record Dr Zenith N127,950, Dr bank charges N2,050, Cr clearing N130,000
without anybody at Lekki doing anything, and both sets of books read zero for
Lekki.

A settlement is the platform paying money it owes, so it is the platform's
document: a ``SETTLEMENT`` payout batch in the platform's books (branch Lagos),
approved through the platform tenant's own settlement route
(:func:`vs_payments.approvals.ensure_settlement_approval_template`), and listed
for its operators across every tenant (:func:`settlements_due`). The tenant sees
its settlements and the money arriving, and acts on neither.

A chargeback on held money lowers the branch's held balance at once and is booked
in both books (:func:`record_dispute`). Where the branch holds less than the
chargeback took, its balance goes below zero: that is what it owes the platform
(``CLIENT_FUNDS_OWED``), repaid from its next payments before anything is settled
to it again.

Engines stay out of the tenant domain: the platform's books are found through
``vs_tenants`` (the tenant of kind ``PLATFORM``) and ``vs_finance``, never through
a school app.
"""
from __future__ import annotations

import datetime
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from . import audit
from .constants import (
    CustodyMode,
    HeldMovementKind,
    HeldSettlementStatus,
    PaymentAuditAction,
    PayoutBatchStatus,
    PayoutPurpose,
    PayoutStatus,
    VirtualAccountStatus,
)
from .exceptions import PaymentStateError

logger = logging.getLogger("vs_payments.held")


class InsufficientHeldFundsError(PaymentStateError):
    """A transfer for a held-mode branch is more than the platform holds for it (409)."""

    error_code = "HELD_FUNDS_INSUFFICIENT"
    default_message = "The platform does not hold enough of this branch's money for this transfer."


# --------------------------------------------------------------------------- #
# Whose money, and where the platform keeps its books                          #
# --------------------------------------------------------------------------- #

def platform_books():
    """The platform's own ledger entity, or None where it is not set up.

    It is the active ``PLATFORM`` entity owned by the tenant of kind ``PLATFORM``.
    """
    from vs_finance.models import LedgerEntity
    from vs_tenants.models import Tenant

    return (
        LedgerEntity.objects.filter(
            kind=LedgerEntity.Kind.PLATFORM, is_active=True,
            tenant__kind=Tenant.Kind.PLATFORM,
        ).select_related("tenant").order_by("pk").first()
    )


def holds_for(entity) -> bool:
    """Whether the platform can hold online money for ``entity``'s branches.

    Every tenant's books except the platform's own: the platform's provider
    balance holding the platform's own takings is its own money, owed to nobody.
    """
    from vs_tenants.models import Tenant

    if entity is None or not entity.tenant_id:
        return False
    return entity.tenant.kind != Tenant.Kind.PLATFORM


def collection_is_held(intent) -> bool:
    """Whether a confirmed collection's money settled to the platform's provider balance.

    It did unless the checkout, or the virtual account it was paid into, named a
    branch subaccount: that money went to the branch's own bank. Decided by how
    the payment was routed, not by today's mode, so a payment begun before a
    switch is held or not exactly as it was taken.
    """
    if not holds_for(intent.entity):
        return False
    if (intent.metadata or {}).get("settlement_subaccount"):
        return False
    if intent.virtual_account_id and intent.virtual_account.settlement_subaccount:
        return False
    return True


def held_branch_id(entity, branch_id):
    """The branch money of ``branch_id`` is held for: itself, or the tenant's only branch."""
    from vs_rbac.scoping import only_branch_id

    return branch_id or (only_branch_id(entity.tenant_id) if entity.tenant_id else None)


def _branch_label(tenant, branch_id) -> str:
    from vs_tenants.models import Branch

    name = Branch.all_objects.filter(pk=branch_id).values_list("name", flat=True).first() or ""
    return " ".join(part for part in (getattr(tenant, "name", ""), name) if part)


def _naira(kobo) -> str:
    from vs_finance.money import format_naira

    return format_naira(int(kobo))


def held_balance(branch_id) -> int:
    """Kobo the platform holds for ``branch_id`` (0 when it never held any)."""
    from .models import HeldBalance

    return int(HeldBalance.objects.filter(branch_id=branch_id).values_list(
        "balance", flat=True).first() or 0)


# --------------------------------------------------------------------------- #
# The sub-ledger and the platform's journal                                    #
# --------------------------------------------------------------------------- #

def _locked_balance(tenant_id, branch_id):
    """The branch's :class:`HeldBalance`, created at zero if new, locked for this transaction."""
    from .models import HeldBalance

    row = HeldBalance.objects.select_for_update().filter(branch_id=branch_id).first()
    if row is None:
        try:
            with transaction.atomic():
                HeldBalance.objects.create(tenant_id=tenant_id, branch_id=branch_id)
        except IntegrityError:  # Another transaction created it first.
            pass
        row = HeldBalance.objects.select_for_update().get(branch_id=branch_id)
    return row


def _move(*, tenant, branch_id, kind, amount, on, reference="", narration="",
          collection=None, payout=None, actor_user=None, balance=None):
    """Record one movement of a branch's held balance and mirror it in the platform's books.

    Must run inside the caller's transaction. ``balance`` is the branch's row when
    the caller already holds its lock.
    """
    from .models import HeldMovement

    balance = balance or _locked_balance(tenant.pk, branch_id)
    balance.balance += int(amount)
    balance.save(update_fields=["balance", "updated_at"])
    movement = HeldMovement.objects.create(
        tenant=tenant, branch_id=branch_id, kind=kind, amount=int(amount),
        balance_after=balance.balance, occurred_on=on,
        collection=collection, payout=payout, reference=reference[:64],
        narration=(narration or f"Held for {_branch_label(tenant, branch_id)}")[:255],
        created_by=actor_user,
    )
    _post_platform_journal(movement, actor_user=actor_user)
    return movement


def ensure_platform_accounts(platform):
    """The platform's (client funds held, provider balance, owed by clients) accounts, created when absent.

    Each is its mapped account, else the account at its default code (2180, 1127,
    1128), else a new one at that code under the chart's liabilities or assets
    header.
    Raises :class:`~vs_finance.exceptions.MissingAccountError` when neither can
    be found or made.
    """
    from vs_finance.account_mappings import ACCOUNT_MAPPING_SPECS, resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, AccountType, IFRSLine
    from vs_finance.exceptions import MissingAccountError
    from vs_finance.models import Account, FinanceAccountMapping

    found = []
    for key, name, header, line in (
            (AccountMappingKey.CLIENT_FUNDS_HELD, "Client Funds Held", "2000",
             IFRSLine.TRADE_PAYABLES),
            (AccountMappingKey.PROVIDER_BALANCE, "Payment Provider Balance", "1000", IFRSLine.CASH),
            (AccountMappingKey.CLIENT_FUNDS_OWED, "Owed by Clients", "1000",
             IFRSLine.TRADE_RECEIVABLES)):
        try:
            found.append(resolve_mapped_account(platform, key))
            continue
        except MissingAccountError:
            if FinanceAccountMapping.objects.filter(entity=platform, key=key).exists():
                raise
        code, account_type = ACCOUNT_MAPPING_SPECS[key]
        if Account.objects.filter(entity=platform, code=code).exists():
            raise MissingAccountError(code, label=AccountMappingKey(key).label)
        parent = Account.objects.filter(entity=platform, code=header).first()
        if parent is None:
            raise MissingAccountError(header, label="chart of accounts")
        found.append(Account.objects.create(
            entity=platform, code=code, name=name, account_type=account_type,
            is_postable=True, ifrs_line=line, parent=parent,
        ))
    return tuple(found)


def _platform_branch_id(platform):
    from vs_tenants.models import Branch

    return (Branch.all_objects.filter(tenant_id=platform.tenant_id)
            .order_by("-is_main", "pk").values_list("pk", flat=True).first())


def _post_platform_journal(movement, *, actor_user=None):
    """Post ``movement``'s journal in the platform's books, or record why it could not.

    In a savepoint, so a refusal leaves the movement (and the caller's work)
    standing with ``journal_error`` set. Dated on the movement's day, or the
    first open day after it when that day's period is closed.

    The provider balance moves by the whole amount. The other side is split where
    the branch's balance crosses zero: the part above zero moves the client-funds
    liability, the part below it what the branch owes the platform. A N30,000
    chargeback against a branch holding N10,000 books Dr client funds held
    N10,000, Dr owed by clients N20,000, Cr provider balance N30,000; the branch's
    next N25,000 payment books Dr provider balance N25,000, Cr owed by clients
    N20,000, Cr client funds held N5,000.
    """
    from vs_finance.banking import resolve_adjustment_date
    from vs_finance.constants import JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    if movement.platform_journal_id or not movement.amount:
        return movement
    platform = platform_books()
    try:
        if platform is None:
            raise PaymentStateError("The platform's books are not set up.")
        with transaction.atomic():
            held, provider, owed = ensure_platform_accounts(platform)
            day = resolve_adjustment_date(platform, movement.occurred_on)
            entry = JournalEntry.objects.create(
                entity=platform, branch_id=_platform_branch_id(platform), date=day,
                period=resolve_period(platform, day), source=JournalSource.SYSTEM,
                narration=(f"{movement.get_kind_display()}: held for "
                           f"{_branch_label(movement.tenant, movement.branch_id)}")[:255],
                reference=movement.reference, created_by=actor_user,
            )
            after = int(movement.balance_after)
            before = after - int(movement.amount)
            lines = [
                (provider, int(movement.amount), "Provider balance"),
                # A liability rises with a credit, so its change is entered negated.
                (held, -(max(after, 0) - max(before, 0)), "Client funds held"),
                (owed, -(min(after, 0) - min(before, 0)), "Owed by clients"),
            ]
            line_no = 0
            for account, signed, description in lines:
                if not signed:
                    continue
                line_no += 1
                JournalLine.objects.create(
                    entry=entry, account=account, line_no=line_no, description=description,
                    debit=signed if signed > 0 else 0, credit=-signed if signed < 0 else 0)
            post_journal(entry, actor_user=actor_user)
    except Exception as exc:  # noqa: BLE001 - money already moved; record, do not refuse.
        movement.journal_error = str(getattr(exc, "message", exc))[:255]
        movement.save(update_fields=["journal_error", "updated_at"])
        logger.warning("Held movement %s not posted to the platform's books: %s",
                       movement.pk, movement.journal_error)
        return movement
    movement.platform_journal = entry
    movement.journal_error = ""
    movement.save(update_fields=["platform_journal", "journal_error", "updated_at"])
    return movement


def post_pending_platform_journals() -> dict:
    """Post every held movement the platform's books could not take when it happened."""
    from .models import HeldMovement

    posted = still = 0
    for movement in (HeldMovement.objects.filter(platform_journal__isnull=True)
                     .exclude(amount=0).select_related("tenant").order_by("pk")):
        _post_platform_journal(movement)
        if movement.platform_journal_id:
            posted += 1
        else:
            still += 1
    return {"posted": posted, "waiting": still}


def platform_liability_balance() -> int:
    """Kobo the platform's books show as client funds held (credit balance)."""
    from vs_finance.constants import AccountMappingKey, DocumentStatus
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.models import JournalLine

    platform = platform_books()
    if platform is None:
        return 0
    try:
        account = resolve_mapped_account(platform, AccountMappingKey.CLIENT_FUNDS_HELD)
    except Exception:  # noqa: BLE001 - no account means nothing booked to it.
        return 0
    totals = JournalLine.objects.filter(
        account=account, entry__status__in=(DocumentStatus.POSTED, DocumentStatus.REVERSED),
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return int((totals["credit"] or 0) - (totals["debit"] or 0))


def platform_owed_balance() -> int:
    """Kobo the platform's books show its clients owe it (debit balance)."""
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, DocumentStatus
    from vs_finance.models import JournalLine

    platform = platform_books()
    if platform is None:
        return 0
    try:
        account = resolve_mapped_account(platform, AccountMappingKey.CLIENT_FUNDS_OWED)
    except Exception:  # noqa: BLE001 - no account means nothing booked to it.
        return 0
    totals = JournalLine.objects.filter(
        account=account, entry__status__in=(DocumentStatus.POSTED, DocumentStatus.REVERSED),
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return int((totals["debit"] or 0) - (totals["credit"] or 0))


# --------------------------------------------------------------------------- #
# What moves the balance                                                       #
# --------------------------------------------------------------------------- #

def record_collection(intent, *, actor_user=None):
    """Raise the branch's held balance for a confirmed collection whose money the platform holds.

    Called inside the confirmation's transaction, after the receipt is booked.
    Sets ``intent.held_by_platform`` for the caller to save. What is held is
    what reached the provider balance: the payment less the fee the provider
    reported, or the whole payment when it reported none.
    """
    if not collection_is_held(intent):
        return None
    intent.held_by_platform = True
    branch_id = held_branch_id(intent.entity, intent.branch_id)
    if branch_id is None:
        logger.warning("Held collection %s has no branch to hold it for.", intent.reference)
        return None
    net = int(intent.amount) - int(intent.fee or 0)
    return _move(
        tenant=intent.entity.tenant, branch_id=branch_id, kind=HeldMovementKind.COLLECTION,
        amount=net, on=intent.payment.payment_date, reference=intent.reference,
        collection=intent, actor_user=actor_user,
    )


def reserve_for_transfer(payout, amount, *, actor_user=None):
    """Check and take ``amount`` from the held balance of the branch ``payout`` pays for.

    Runs inside the dispatch's claim, under a lock on the branch's balance, so two
    transfers for one branch cannot both spend the same money. Refused (409)
    when the branch holds less; nothing is taken then.

    A settlement payout sits in the platform's books but spends the held money of
    the branch it pays (its :class:`HeldSettlement`'s), and takes what is sent
    plus the transfer fee the branch bears, which is what leaves the provider
    balance; it is taken as ``SETTLEMENT``. Any other payout is taken as
    ``PAYOUT`` from the branch whose bank it names. The platform's own vendor
    payouts spend no client's money and are not checked.
    """
    settlement = settlement_for(payout)
    if settlement is not None:
        tenant, branch_id = settlement.tenant, settlement.branch_id
        amount = int(amount) + int(settlement.transfer_fee)
        balance = _locked_balance(tenant.pk, branch_id)
        _refuse_above(balance, branch_id, amount)
        return _move(
            tenant=tenant, branch_id=branch_id, kind=HeldMovementKind.SETTLEMENT,
            amount=-amount, on=_today(tenant), reference=payout.reference,
            payout=payout, actor_user=actor_user, balance=balance,
        )
    entity = payout.entity
    if not holds_for(entity):
        return None
    tenant = entity.tenant
    branch_id = held_branch_id(entity, payout.branch_id)
    if branch_id is None:
        raise InsufficientHeldFundsError(
            "This payout's source account has not been given a branch, so there is no "
            "branch's held money to pay it from. Give the account its branch first.")
    balance = _locked_balance(tenant.pk, branch_id)
    _refuse_above(balance, branch_id, amount)
    return _move(
        tenant=tenant, branch_id=branch_id, kind=HeldMovementKind.PAYOUT,
        amount=-int(amount), on=_today(tenant), reference=payout.reference,
        payout=payout, actor_user=actor_user, balance=balance,
    )


def _refuse_above(balance, branch_id, amount):
    """Refuse a transfer of ``amount`` the locked ``balance`` does not cover (409)."""
    if int(amount) > balance.balance:
        label = _branch_label(None, branch_id)
        raise InsufficientHeldFundsError(
            f"{label} has {_naira(max(balance.balance, 0))} of online payments held by the "
            f"platform, and this transfer needs {_naira(amount)}. Wait for more online "
            f"payments to clear, or pay from the branch's bank and record the payment.",
            branch=branch_id, held=balance.balance, needed=int(amount),
        )


def settlement_for(payout):
    """The :class:`HeldSettlement` a payout line pays, or None for any other payout."""
    from .models import HeldSettlement

    if not payout.batch_id:
        return None
    return HeldSettlement.objects.select_related("tenant").filter(batch_id=payout.batch_id).first()


def _reservation(payout):
    from .models import HeldMovement

    return HeldMovement.objects.filter(
        payout=payout, kind__in=(HeldMovementKind.PAYOUT, HeldMovementKind.SETTLEMENT),
    ).first()


def drew_on_held(payout) -> bool:
    """Whether ``payout`` was paid out of a branch's held money (its money left the provider balance)."""
    return _reservation(payout) is not None


def release_transfer(payout, *, reason="", actor_user=None):
    """Give back what a failed transfer took from its branch's held balance, once.

    A failed settlement payout also fails its settlement, releasing its payments
    for the next run. Must run inside a transaction.
    """
    from .models import HeldMovement

    reserved = _reservation(payout)
    if reserved is not None and not HeldMovement.objects.filter(
            payout=payout, kind=HeldMovementKind.RELEASE).exists():
        _move(
            tenant=reserved.tenant, branch_id=reserved.branch_id, kind=HeldMovementKind.RELEASE,
            amount=-reserved.amount, on=_today(reserved.tenant), reference=payout.reference,
            payout=payout, actor_user=actor_user,
            narration=f"Returned: {reason}" if reason else "",
        )
    if is_settlement_payout(payout):
        fail_settlement(payout, reason=reason or "The transfer failed.", actor_user=actor_user)


def true_up_transfer(payout, sent, *, actor_user=None):
    """Correct a branch's held balance when the provider sent other than was reserved.

    ``sent`` is what the provider reports sending; for a settlement the transfer
    fee the branch bears also left the balance. The difference is recorded once,
    as a release (negative when more left than was reserved).
    """
    from .models import HeldMovement

    reserved = _reservation(payout)
    if reserved is None:
        return None
    settlement = settlement_for(payout)
    left = int(sent) + (int(settlement.transfer_fee) if settlement is not None else 0)
    gap = -int(reserved.amount) - left
    if not gap or HeldMovement.objects.filter(payout=payout, kind=HeldMovementKind.RELEASE).exists():
        return None
    return _move(
        tenant=reserved.tenant, branch_id=reserved.branch_id, kind=HeldMovementKind.RELEASE,
        amount=gap, on=_today(reserved.tenant), reference=payout.reference, payout=payout,
        actor_user=actor_user, narration="The provider sent a different amount than reserved.",
    )


@transaction.atomic
def record_opening_balance(*, tenant, branch, amount, reason, actor_user=None):
    """Record money the platform already holds for a branch when its held ledger starts.

    For a platform operator, from the provider's records: the sub-ledger starts
    at zero, so money a branch's payments put in the balance before held custody
    was tracked is entered here, once per branch; a second is refused. The
    operator (``actor_user``) and ``reason`` are audited.
    """
    from vs_finance.models import LedgerEntity

    from .models import HeldMovement

    if HeldMovement.objects.filter(branch=branch, kind=HeldMovementKind.OPENING).exists():
        raise PaymentStateError(
            f"{_branch_label(tenant, branch.pk)} already has its opening held balance. "
            f"Correct the provider's records with a movement the ledger can explain instead.")
    if int(amount) <= 0:
        raise PaymentStateError("An opening held balance must be a positive amount.")
    if not str(reason or "").strip():
        raise PaymentStateError("Say where the opening balance comes from.")
    if branch.tenant_id != tenant.pk:
        raise PaymentStateError("That branch belongs to another tenant.")
    movement = _move(
        tenant=tenant, branch_id=branch.pk, kind=HeldMovementKind.OPENING, amount=int(amount),
        on=_today(tenant), reference="OPENING", narration=str(reason)[:255],
        actor_user=actor_user,
    )
    audit.record(
        action=PaymentAuditAction.HELD_OPENING_BALANCE,
        entity=LedgerEntity.objects.filter(tenant=tenant).order_by("pk").first(),
        actor_user=actor_user,
        message=f"Recorded {_naira(amount)} already held for {_branch_label(tenant, branch.pk)}.",
        metadata={"branch_id": branch.pk, "amount": int(amount), "reason": str(reason)[:200],
                  "movement_id": movement.pk,
                  "operator": getattr(actor_user, "email", "") if actor_user else ""},
    )
    return movement


def _today(tenant):
    from vs_config.clock import tenant_today

    return tenant_today(tenant)


def is_settlement_payout(payout) -> bool:
    """Whether ``payout`` is the line of a ``SETTLEMENT`` batch."""
    from .models import PayoutBatch

    if not payout.batch_id:
        return False
    return PayoutBatch.objects.filter(
        pk=payout.batch_id, purpose=PayoutPurpose.SETTLEMENT).exists()


# --------------------------------------------------------------------------- #
# The settlement run                                                           #
# --------------------------------------------------------------------------- #

def _waiting_held_q():
    from .settlement import awaiting_settlement_q

    return Q(held_by_platform=True, held_settlement__isnull=True) & awaiting_settlement_q()


def settlement_destination(entity, branch_id):
    """``(bank_account, None)`` for the account a branch's settlement is paid into, or ``(None, reason)``.

    The branch's collection account, which must be active and carry its account
    number and its bank's provider code (recorded when it is set up with the
    provider under payment settings).
    """
    from .custody import branch_collection_account

    bank = branch_collection_account(entity, branch_id)
    label = _branch_label(None, branch_id)
    if bank is None or not bank.is_active:
        return None, f"{label} has no active collection bank account to be paid into."
    if not str(bank.account_number or "").strip() or not bank.settlement_bank_code:
        return None, (f"{label}'s collection account {bank.name} is not set up with the "
                      f"payment provider, so it cannot be paid into yet.")
    return bank, None


def build_settlement(entity, branch_id, *, today=None, final=False, actor_user=None):
    """Prepare one branch's settlement of what the platform holds for it: ``(settlement, note)``.

    ``settlement`` is None when nothing was built, and ``note`` says why. A
    regular run waits ``settlement_interval_days`` after the branch's last
    settlement and includes payments confirmed before the start of the tenant's
    day; a final run (a switch to direct custody) includes everything confirmed
    so far and ignores the interval. A branch with a settlement still pending is
    left alone.

    The settlement claims its payments and fixes its amount: the payments less
    the provider's fees, capped at the branch's held balance, less the provider's
    fee for the transfer, which the branch bears. With money to send it is a
    ``SETTLEMENT`` payout batch in the platform's books, one line into the
    branch's collection account, waiting for a platform operator to put it
    forward (:func:`submit_settlement`); with none (online payouts spent it) it
    books its fees at once. A branch whose money would not cover the transfer
    fee is left for a later run.
    """
    from vs_config.clock import tenant_zone
    from vs_rbac.scoping import transaction_branch_match_q

    from .custody import custody_row
    from .models import CollectionIntent, HeldSettlement

    tenant = entity.tenant
    today = today or _today(tenant)
    label = _branch_label(None, branch_id)
    with transaction.atomic():
        balance = _locked_balance(tenant.pk, branch_id)
        pending = HeldSettlement.objects.filter(
            branch_id=branch_id, status=HeldSettlementStatus.PENDING).first()
        if pending is not None:
            return None, f"{label}: settlement {pending.pk} is still waiting for approval or transfer."
        if HeldSettlement.objects.filter(branch_id=branch_id, run_on=today, final=final).exists():
            return None, f"{label}: already settled today."
        if not final:
            interval = custody_row(tenant).settlement_interval_days
            last = (HeldSettlement.objects.filter(branch_id=branch_id)
                    .exclude(status=HeldSettlementStatus.FAILED).order_by("-run_on").first())
            if last is not None and (today - last.run_on).days < interval:
                return None, f"{label}: next settlement is due {interval} day(s) after {last.run_on}."
        cutoff = (timezone.now() if final else datetime.datetime.combine(
            today, datetime.time.min, tzinfo=tenant_zone(tenant)))
        claims = list(
            CollectionIntent.objects.select_for_update(of=("self",))
            .filter(entity=entity, confirmed_at__lt=cutoff)
            .filter(_waiting_held_q())
            .filter(transaction_branch_match_q(tenant.pk, branch_id))
            .order_by("pk")
        )
        if not claims:
            return None, f"{label}: nothing waiting to be settled."
        bank, reason = settlement_destination(entity, branch_id)
        if bank is None:
            return None, reason
        platform = platform_books()
        if platform is None:
            return None, f"{label}: the platform's books are not set up to pay settlements."
        gross = sum(int(c.amount) for c in claims)
        fees = sum(int(c.fee or 0) for c in claims)
        due = max(0, min(balance.balance, gross - fees))
        transfer_fee = transfer_fee_for(due) if due else 0
        if due and due <= transfer_fee:
            return None, (f"{label}: {_naira(due)} would not cover the {_naira(transfer_fee)} "
                          f"transfer fee; it waits for the next run.")
        settlement = HeldSettlement.objects.create(
            entity=entity, tenant=tenant, branch_id=branch_id, run_on=today, final=final,
            cutoff=cutoff, gross=gross, fees=fees, amount=due - transfer_fee,
            transfer_fee=transfer_fee, bank_account=bank,
        )
        CollectionIntent.objects.filter(pk__in=[c.pk for c in claims]).update(
            held_settlement=settlement, updated_at=timezone.now())
        if due:
            settlement.batch = _settlement_batch(settlement, bank, platform, actor_user=actor_user)
            settlement.save(update_fields=["batch", "updated_at"])
        else:
            _book_tenant_settlement(settlement, sent=0, paid_at=None, actor_user=actor_user)
        audit.record(
            action=PaymentAuditAction.HELD_SETTLEMENT_BUILT, entity=entity,
            reference=claims[0].reference, actor_user=actor_user,
            message=(f"{'Final s' if final else 'S'}ettlement for {label}: {len(claims)} payment(s), "
                     f"{_naira(gross)} less {_naira(fees)} fees; transfer "
                     f"{_naira(settlement.amount)} after a {_naira(transfer_fee)} transfer fee."),
            metadata={"settlement_id": settlement.pk, "branch_id": branch_id, "gross": gross,
                      "fees": fees, "amount": int(settlement.amount),
                      "transfer_fee": transfer_fee, "collection_ids": [c.pk for c in claims],
                      "batch_id": settlement.batch_id, "final": final},
        )
    return settlement, ""


def transfer_fee_for(amount) -> int:
    """The provider's fee, in kobo, for transferring ``amount`` to a bank account."""
    from .providers.registry import get_provider
    from .services import resolve_provider_name

    return int(get_provider(resolve_provider_name(None)).transfer_fee(int(amount)) or 0)


def _settlement_batch(settlement, bank, platform, *, actor_user=None):
    """The platform's one-line ``SETTLEMENT`` payout batch paying ``settlement`` into ``bank``.

    It is the platform's document: its books, its branch, its provider balance
    (``PROVIDER_BALANCE``) named as the account the money leaves, and its
    reference. The line pays the branch's collection account what is due less
    the transfer fee. Confirmation books no vendor payment: it books the money
    arriving in the tenant's books (:func:`book_settlement_paid`).
    """
    from .models import PayoutBatch, PayoutInstruction
    from .services import _entity_currency, _new_reference, resolve_provider_name

    entity = settlement.entity
    provider = resolve_provider_name(None)
    _held, provider_balance, _owed = ensure_platform_accounts(platform)
    lagos = _platform_branch_id(platform)
    label = _branch_label(entity.tenant, settlement.branch_id)
    title = f"Settlement to {label} ({bank.name})"
    narration = f"Online payments held by the platform, settled to {label}"
    batch = PayoutBatch.objects.create(
        entity=platform, branch_id=lagos, provider=provider,
        purpose=PayoutPurpose.SETTLEMENT, reference=_new_reference(platform),
        title=title[:200], narration=narration[:255], status=PayoutBatchStatus.DRAFT,
        total_amount=settlement.amount, item_count=1, currency=_entity_currency(platform),
        source_account=provider_balance, created_by=actor_user,
        metadata={"held_settlement_id": settlement.pk, "tenant": entity.tenant.slug},
    )
    PayoutInstruction.objects.create(
        entity=platform, branch_id=lagos, batch=batch, provider=provider,
        reference=_new_reference(platform), amount=settlement.amount, currency=batch.currency,
        beneficiary_name=(entity.name or bank.name)[:200],
        beneficiary_account_number=str(bank.account_number).strip(),
        beneficiary_bank_code=bank.settlement_bank_code, narration=narration[:255],
        source_account=provider_balance, status=PayoutStatus.PENDING, created_by=actor_user,
        metadata={"held_settlement_id": settlement.pk, "bank_account_id": bank.pk,
                  "wht_amount": 0},
    )
    return batch


def validate_settlement_destination(payout, *, lock=False):
    """Refuse a settlement transfer whose destination is no longer the branch's collection account.

    The instruction was written with the account's number and bank code; a
    change to either, or another account becoming the branch's collection
    account, after approval would otherwise send the money where nobody
    approved.
    """
    from vs_finance.models import BankAccount

    from .custody import branch_collection_account

    settlement = settlement_for(payout)
    if settlement is None:
        raise PaymentStateError("This payout line pays no settlement.")
    bank_id = (payout.metadata or {}).get("bank_account_id")
    qs = BankAccount.objects.filter(pk=bank_id, entity_id=settlement.entity_id)
    bank = (qs.select_for_update() if lock else qs).first()
    current = branch_collection_account(settlement.entity, settlement.branch_id)
    if (bank is None or current is None or current.pk != bank.pk or not bank.is_active
            or str(bank.account_number).strip() != payout.beneficiary_account_number
            or bank.settlement_bank_code != payout.beneficiary_bank_code):
        raise PaymentStateError(
            "The branch's collection account changed after this settlement was prepared. "
            "It will be prepared again in the next settlement run.")
    return bank


def _book_tenant_settlement(settlement, *, sent, paid_at, actor_user=None):
    """Book a paid settlement in the tenant's books and take its payments out of clearing.

    Dr the branch's bank what arrived, Dr bank charges the claimed payments' fees
    and the transfer fee, Cr gateway clearing all three, in the settlement's
    branch. Clearing is credited with less than the payments came to when online
    payouts spent part of their money, since each such payout already credited
    clearing for what it took.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    from .models import CollectionIntent
    from .services import _booking_date

    entity = settlement.entity
    fees = int(settlement.fees) + (int(settlement.transfer_fee) if sent else 0)
    entry = None
    if sent or fees:
        day, _dating = _booking_date(entity, paid_at, branch=settlement.branch_id)
        entry = JournalEntry.objects.create(
            entity=entity, branch_id=settlement.branch_id, date=day,
            period=resolve_period(entity, day), source=JournalSource.BANK,
            narration=(f"Platform settlement of online payments into "
                       f"{settlement.bank_account.name}")[:255],
            reference=settlement.batch.reference if settlement.batch_id else "",
            created_by=actor_user,
        )
        line_no = 0
        if sent:
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=settlement.bank_account.gl_account, debit=sent, credit=0,
                description="Platform settlement", line_no=line_no)
        if fees:
            line_no += 1
            JournalLine.objects.create(
                entry=entry, debit=fees, credit=0, line_no=line_no,
                account=resolve_mapped_account(entity, AccountMappingKey.BANK_CHARGES,
                                               label="bank charges"),
                description="Payment provider fees")
        JournalLine.objects.create(
            entry=entry, debit=0, credit=sent + fees, line_no=line_no + 1,
            account=resolve_mapped_account(entity, AccountMappingKey.GATEWAY_CLEARING,
                                           label="gateway clearing"),
            description="Gateway clearing")
        post_journal(entry, actor_user=actor_user)
    settlement.settlement_journal = entry
    settlement.status = HeldSettlementStatus.PAID
    settlement.amount = sent
    settlement.paid_at = timezone.now()
    settlement.save(update_fields=["settlement_journal", "status", "amount", "paid_at", "updated_at"])
    if entry is not None:
        CollectionIntent.objects.filter(held_settlement=settlement).update(
            settlement_entry=entry, updated_at=timezone.now())
    return entry


def book_settlement_paid(payout, *, sent, paid_at=None, actor_user=None):
    """The provider confirmed a settlement transfer: book the tenant's side of it once."""
    from .models import HeldSettlement

    settlement = (HeldSettlement.objects.select_for_update(of=("self",))
                  .select_related("bank_account__gl_account", "entity", "batch")
                  .filter(batch_id=payout.batch_id).first())
    if settlement is None or settlement.status != HeldSettlementStatus.PENDING:
        return settlement
    entry = _book_tenant_settlement(settlement, sent=int(sent), paid_at=paid_at,
                                    actor_user=actor_user)
    audit.record(
        action=PaymentAuditAction.HELD_SETTLEMENT_PAID, entity=settlement.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=(f"Settled {_naira(sent)} into {settlement.bank_account.name} for "
                 f"{_branch_label(settlement.tenant, settlement.branch_id)}."),
        metadata={"settlement_id": settlement.pk, "journal_id": getattr(entry, "pk", None),
                  "amount": int(sent), "fees": int(settlement.fees),
                  "transfer_fee": int(settlement.transfer_fee)},
    )
    return settlement


def fail_settlement(payout, *, reason, actor_user=None):
    """Mark a settlement whose transfer failed, and release its payments for the next run."""
    from .models import CollectionIntent, HeldSettlement

    settlement = (HeldSettlement.objects.select_for_update(of=("self",))
                  .filter(batch_id=payout.batch_id).first())
    if settlement is None or settlement.status != HeldSettlementStatus.PENDING:
        return settlement
    settlement.status = HeldSettlementStatus.FAILED
    settlement.failure_reason = str(reason)[:255]
    settlement.save(update_fields=["status", "failure_reason", "updated_at"])
    CollectionIntent.objects.filter(held_settlement=settlement).update(
        held_settlement=None, updated_at=timezone.now())
    audit.record(
        action=PaymentAuditAction.HELD_SETTLEMENT_FAILED, entity=settlement.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        succeeded=False, message=f"Settlement {settlement.pk} failed: {reason}"[:255],
        metadata={"settlement_id": settlement.pk},
    )
    return settlement


def run_settlements(*, final_for_tenant=None) -> dict:
    """Build every settlement that is due: the scheduled run, idempotent per branch and day.

    Visits each branch with held payments not yet claimed, whatever its tenant's
    mode today: a direct tenant still has money paid into a retired virtual
    account passed on this way. ``final_for_tenant`` restricts the run to one
    tenant and makes it final (see :func:`build_settlement`).
    """
    from vs_finance.models import LedgerEntity

    from .models import CollectionIntent

    waiting = CollectionIntent.objects.filter(_waiting_held_q())
    if final_for_tenant is not None:
        waiting = waiting.filter(entity__tenant=final_for_tenant)
    pairs = sorted(set(waiting.values_list("entity_id", "branch_id")), key=lambda p: (p[0], p[1] or 0))
    entities = {e.pk: e for e in LedgerEntity.objects.filter(
        pk__in={p[0] for p in pairs}).select_related("tenant")}
    summary = {"built": [], "notes": []}
    seen = set()
    for entity_id, branch_id in pairs:
        entity = entities[entity_id]
        branch_id = held_branch_id(entity, branch_id)
        if branch_id is None or (entity_id, branch_id) in seen:
            continue
        seen.add((entity_id, branch_id))
        try:
            settlement, note = build_settlement(
                entity, branch_id, final=final_for_tenant is not None)
        except Exception as exc:  # noqa: BLE001 - one branch must not stop the run.
            logger.exception("Settlement for branch %s failed to build.", branch_id)
            summary["notes"].append(f"{_branch_label(entity.tenant, branch_id)}: {exc}")
            continue
        if settlement is not None:
            summary["built"].append(settlement.pk)
        elif note:
            summary["notes"].append(note)
    return summary


# --------------------------------------------------------------------------- #
# Switching custody                                                            #
# --------------------------------------------------------------------------- #

def outstanding_reasons(tenant) -> list[str]:
    """What the platform still holds for ``tenant``, one line per branch; empty when nothing."""
    from .models import CollectionIntent, HeldBalance
    from .settlement import awaiting_settlement_q

    reasons = []
    for row in HeldBalance.objects.filter(tenant=tenant).exclude(balance=0).order_by("branch_id"):
        reasons.append(f"{_branch_label(None, row.branch_id)} has {_naira(row.balance)} held")
    unsettled = (CollectionIntent.objects.filter(entity__tenant=tenant, held_by_platform=True)
                 .filter(awaiting_settlement_q()).count())
    if unsettled:
        reasons.append(f"{unsettled} held online payment(s) are not yet settled")
    return reasons


def apply_custody_switch(row, *, today=None) -> dict:
    """Apply ``row``'s pending mode once its month has come, when it can be.

    Direct to held takes effect on the day. Held to direct first builds a final
    settlement for every branch the platform holds money for, and takes effect
    only once nothing is held for the tenant and every branch's collection
    account has its subaccount; until then it stays pending and ``pending_note``
    says why. Once it takes effect, every active virtual account still settling
    to the platform's balance is replaced by one against its branch's subaccount
    (:func:`reissue_virtual_accounts`).
    """
    from vs_finance.models import LedgerEntity

    from .custody import branches_without_subaccount, serialize_custody
    from .models import PaymentCustodySettings

    tenant = row.tenant
    today = today or _today(tenant)
    if not row.pending_mode or not row.pending_from or row.pending_from > today:
        return {"tenant": tenant.slug, "result": "not_due"}
    entities = list(LedgerEntity.objects.filter(tenant=tenant, is_active=True).order_by("pk"))
    reasons = []
    if row.pending_mode == CustodyMode.DIRECT:
        run_settlements(final_for_tenant=tenant)
        reasons = outstanding_reasons(tenant)
        for entity in entities:
            missing = branches_without_subaccount(entity)
            if missing:
                reasons.append(f"not set up with the payment provider: {', '.join(missing)}")
    with transaction.atomic():
        row = PaymentCustodySettings.objects.select_for_update().get(pk=row.pk)
        if not row.pending_mode:
            return {"tenant": tenant.slug, "result": "not_due"}
        entity = entities[0] if entities else None
        if reasons:
            note = ("Waiting until no online money is held for any branch and every branch "
                    "is ready: " + "; ".join(reasons) + ".")[:500]
            if note != row.pending_note:
                row.pending_note = note
                row.save(update_fields=["pending_note", "updated_at"])
                audit.record(action=PaymentAuditAction.CUSTODY_SWITCH_WAITING, entity=entity,
                             message=note[:255], metadata={"reasons": reasons})
            return {"tenant": tenant.slug, "result": "waiting", "reasons": reasons}
        before = serialize_custody(row, tenant)
        mode = row.pending_mode
        row.mode = mode
        row.effective_from = today if mode == CustodyMode.DIRECT else row.pending_from
        row.pending_mode, row.pending_from, row.pending_note = "", None, ""
        row.save()
        audit.record(action=PaymentAuditAction.CUSTODY_SWITCHED, entity=entity,
                     message=f"Online payments now {CustodyMode(mode).label.lower()}.",
                     metadata={"before": before["stored_mode"], "after": mode,
                               "effective_from": row.effective_from.isoformat()})
    reissued = {}
    if mode == CustodyMode.DIRECT:
        for entity in entities:
            reissued[entity.code] = reissue_virtual_accounts(entity)
    return {"tenant": tenant.slug, "result": "switched", "mode": mode, "reissued": reissued}


def apply_custody_switches() -> dict:
    """The daily task: apply every pending custody change whose month has come.

    Also replaces, at every direct tenant, any active virtual account still
    settling to the platform's balance that an earlier day could not replace.
    """
    from .models import PaymentCustodySettings

    results = []
    for row in PaymentCustodySettings.objects.select_related("tenant").order_by("pk"):
        try:
            if row.pending_mode:
                results.append(apply_custody_switch(row))
            elif row.mode == CustodyMode.DIRECT:
                from vs_finance.models import LedgerEntity

                for entity in LedgerEntity.objects.filter(tenant=row.tenant, is_active=True):
                    done = reissue_virtual_accounts(entity)
                    if done["reissued"] or done["failed"]:
                        results.append({"tenant": row.tenant.slug, "result": "reissued", **done})
        except Exception:  # noqa: BLE001 - one tenant must not stop the rest.
            logger.exception("Custody switch for tenant %s failed.", row.tenant_id)
            results.append({"tenant": row.tenant.slug, "result": "error"})
    return {"results": results}


def reissue_virtual_accounts(entity) -> dict:
    """Replace each active virtual account settling to the platform's balance with one against its branch's subaccount.

    The old number is retired, not switched off: a parent who still pays into it
    is credited as before, and the platform passes the money on in the next
    settlement run. One account that cannot be replaced (its branch has no
    subaccount, the provider refused) is left active and counted; the daily task
    tries again.
    """
    from vs_finance.exceptions import FinanceError

    from .models import VirtualAccount
    from .services import reissue_virtual_account

    done = failed = 0
    for account in (VirtualAccount.objects.filter(
            entity=entity, status=VirtualAccountStatus.ACTIVE, settlement_subaccount="",
            customer__isnull=False).order_by("pk")):
        try:
            reissue_virtual_account(account)
            done += 1
        except FinanceError as exc:
            failed += 1
            logger.warning("Virtual account %s not reissued: %s", account.pk, exc)
    return {"reissued": done, "failed": failed}


# --------------------------------------------------------------------------- #
# Chargebacks and provider refunds                                             #
# --------------------------------------------------------------------------- #

def record_dispute(event, intent, parsed):
    """Record a chargeback or provider refund event against its payment and raise it.

    A chargeback on a payment the platform held is booked at once
    (:func:`book_held_chargeback`): the payer's bank took the money out of the
    platform's balance, so the branch's held balance falls by it in both books.
    Anything else (a chargeback on a direct tenant's payment, whose money is in
    the branch's bank; a refund made at the provider) is recorded and not
    booked, because how that loss is borne is a decision for people. Either way
    the tenant's finance staff and the platform's operators are told
    (:func:`vs_payments.alerts.dispute_received`).

    A resolution (:data:`~vs_payments.constants.DISPUTE_RESOLUTIONS`) books no
    chargeback. It is recorded (``PROVIDER_DISPUTE_RESOLVED``) whatever the
    tenant's mode; one the merchant won on held money gives the branch its
    chargeback back (:func:`restore_held_chargeback`), and one it lost leaves
    the chargeback as booked.
    """
    from .alerts import dispute_received
    from .constants import DISPUTE_RESOLUTIONS, DisputeOutcome, PaymentDirection
    from .custody import custody_mode

    entity = intent.entity if intent is not None else None
    tenant = entity.tenant if entity is not None and entity.tenant_id else None
    mode = custody_mode(tenant) if tenant is not None else CustodyMode.HELD
    booked = restored = None
    held_money = intent is not None and intent.held_by_platform
    is_dispute = parsed.direction == PaymentDirection.DISPUTE
    if is_dispute and parsed.status in DISPUTE_RESOLUTIONS:
        if held_money and parsed.status == DisputeOutcome.WON:
            restored = restore_held_chargeback(intent)
        audit.record(
            action=PaymentAuditAction.PROVIDER_DISPUTE_RESOLVED, entity=entity,
            provider=event.provider,
            reference=intent.reference if intent is not None else parsed.reference,
            message=(f"Dispute on {intent.reference if intent is not None else 'an unknown payment'} "
                     f"{'won' if parsed.status == DisputeOutcome.WON else 'lost'}; "
                     + ("the chargeback was given back to the branch." if restored else
                        "nothing to give back." if parsed.status == DisputeOutcome.WON else
                        "the chargeback stands."))[:255],
            metadata={"event_id": event.pk, "event_type": parsed.event_type,
                      "outcome": parsed.status, "custody_mode": mode,
                      "held_by_platform": bool(held_money),
                      "held_movement_id": getattr(restored, "pk", None)},
        )
        return dispute_received(event=event, intent=intent, parsed=parsed, custody_mode=mode,
                                outcome=parsed.status, restored=restored)
    if is_dispute and held_money:
        booked = book_held_chargeback(intent, parsed.amount)
    audit.record(
        action=PaymentAuditAction.PROVIDER_DISPUTE_RECEIVED, entity=entity,
        provider=event.provider,
        reference=intent.reference if intent is not None else parsed.reference,
        message=(f"{parsed.event_type} received for "
                 f"{intent.reference if intent is not None else 'an unknown payment'}; "
                 + ("taken from the branch's held balance." if booked else "recorded, not booked."))[:255],
        metadata={"event_id": event.pk, "event_type": parsed.event_type,
                  "amount": int(parsed.amount or 0), "custody_mode": mode,
                  "held_by_platform": bool(getattr(intent, "held_by_platform", False)),
                  "held_movement_id": getattr(booked, "pk", None),
                  "owed_to_platform": owed_by_chargeback(booked)},
    )
    return dispute_received(event=event, intent=intent, parsed=parsed, custody_mode=mode,
                            chargeback=booked)


def owed_by_chargeback(movement) -> int:
    """Kobo of a chargeback the branch did not hold, and so owes the platform."""
    if movement is None:
        return 0
    before = int(movement.balance_after) - int(movement.amount)
    return max(0, -int(movement.balance_after)) - max(0, -before)


@transaction.atomic
def book_held_chargeback(intent, amount, *, actor_user=None):
    """Take a chargeback on a held payment from its branch's held balance, in both books, once.

    The amount is what the provider reports the payer's bank took, never more
    than the payment. The platform's books move the provider balance against
    what it holds for the branch, and against what the branch owes it for any
    part the branch did not hold (:func:`_post_platform_journal`). The tenant's
    books record the loss: Dr chargebacks (mapping ``CHARGEBACKS``), Cr gateway
    clearing, in the branch, since the money left the provider balance the
    branch's clearing stands for. The receipt and the invoice it paid are left
    as they are, for finance staff to pursue the payer.

    A later event for the same dispute (a reminder) books nothing again; its
    resolution is :func:`record_dispute`'s to handle. A tenant journal the books cannot take is recorded on the movement
    (``journal_error``) rather than refused: the money has already gone.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    from .models import HeldMovement

    existing = HeldMovement.objects.filter(
        collection=intent, kind=HeldMovementKind.DISPUTE).first()
    if existing is not None:
        return existing
    entity = intent.entity
    branch_id = held_branch_id(entity, intent.branch_id)
    if branch_id is None:
        return None
    taken = min(int(amount or 0) or int(intent.amount), int(intent.amount))
    if taken <= 0:
        return None
    tenant = entity.tenant
    today = _today(tenant)
    movement = _move(
        tenant=tenant, branch_id=branch_id, kind=HeldMovementKind.DISPUTE, amount=-taken,
        on=today, reference=intent.reference, collection=intent, actor_user=actor_user,
        narration=f"Chargeback on {intent.reference} for {_branch_label(tenant, branch_id)}",
    )
    try:
        with transaction.atomic():
            entry = JournalEntry.objects.create(
                entity=entity, branch_id=branch_id, date=today,
                period=resolve_period(entity, today), source=JournalSource.BANK,
                narration=f"Chargeback on online payment {intent.reference}"[:255],
                reference=intent.reference, created_by=actor_user,
            )
            JournalLine.objects.create(
                entry=entry, line_no=1, debit=taken, credit=0, description="Chargeback",
                account=resolve_mapped_account(entity, AccountMappingKey.CHARGEBACKS,
                                               label="payment chargebacks"))
            JournalLine.objects.create(
                entry=entry, line_no=2, debit=0, credit=taken, description="Gateway clearing",
                account=resolve_mapped_account(entity, AccountMappingKey.GATEWAY_CLEARING,
                                               label="gateway clearing"))
            post_journal(entry, actor_user=actor_user)
    except Exception as exc:  # noqa: BLE001 - the money has gone; record, do not refuse.
        movement.journal_error = f"Tenant books: {getattr(exc, 'message', exc)}"[:255]
        movement.save(update_fields=["journal_error", "updated_at"])
        logger.warning("Chargeback %s not booked in the tenant's books: %s",
                       intent.reference, movement.journal_error)
        return movement
    movement.tenant_journal = entry
    movement.save(update_fields=["tenant_journal", "updated_at"])
    return movement


@transaction.atomic
def restore_held_chargeback(intent, *, actor_user=None):
    """Give a branch back a chargeback the platform won, in both books, once per dispute.

    The reverse of :func:`book_held_chargeback`, for the amount it took: the
    branch's held balance rises by it, and the platform's journal splits the
    rise where the balance crosses zero, so any shortfall the chargeback left the
    branch owing is repaid first and only the rest is held for it again
    (:func:`_post_platform_journal`). The tenant's books reverse their entry: Dr
    gateway clearing, Cr payment chargebacks, in the branch. None when no
    chargeback was booked for the payment (nothing to give back) and the
    existing movement when it was already given back.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    from .models import HeldMovement

    taken = HeldMovement.objects.filter(collection=intent, kind=HeldMovementKind.DISPUTE).first()
    if taken is None:
        return None
    existing = HeldMovement.objects.filter(
        collection=intent, kind=HeldMovementKind.DISPUTE_WON).first()
    if existing is not None:
        return existing
    entity = intent.entity
    tenant = entity.tenant
    amount = -int(taken.amount)
    today = _today(tenant)
    movement = _move(
        tenant=tenant, branch_id=taken.branch_id, kind=HeldMovementKind.DISPUTE_WON,
        amount=amount, on=today, reference=intent.reference, collection=intent,
        actor_user=actor_user,
        narration=f"Chargeback on {intent.reference} won back for "
                  f"{_branch_label(tenant, taken.branch_id)}",
    )
    try:
        with transaction.atomic():
            entry = JournalEntry.objects.create(
                entity=entity, branch_id=taken.branch_id, date=today,
                period=resolve_period(entity, today), source=JournalSource.BANK,
                narration=f"Chargeback on online payment {intent.reference} won back"[:255],
                reference=intent.reference, created_by=actor_user,
            )
            JournalLine.objects.create(
                entry=entry, line_no=1, debit=amount, credit=0, description="Gateway clearing",
                account=resolve_mapped_account(entity, AccountMappingKey.GATEWAY_CLEARING,
                                               label="gateway clearing"))
            JournalLine.objects.create(
                entry=entry, line_no=2, debit=0, credit=amount, description="Chargeback won back",
                account=resolve_mapped_account(entity, AccountMappingKey.CHARGEBACKS,
                                               label="payment chargebacks"))
            post_journal(entry, actor_user=actor_user)
    except Exception as exc:  # noqa: BLE001 - the money is back; record, do not refuse.
        movement.journal_error = f"Tenant books: {getattr(exc, 'message', exc)}"[:255]
        movement.save(update_fields=["journal_error", "updated_at"])
        logger.warning("Won chargeback %s not booked in the tenant's books: %s",
                       intent.reference, movement.journal_error)
        return movement
    movement.tenant_journal = entry
    movement.save(update_fields=["tenant_journal", "updated_at"])
    return movement


def repaid_by_restore(movement) -> int:
    """Kobo of a won-back chargeback that repaid what the branch owed the platform."""
    if movement is None:
        return 0
    before = int(movement.balance_after) - int(movement.amount)
    return max(0, -before) - max(0, -int(movement.balance_after))


# --------------------------------------------------------------------------- #
# The platform's side: approving and listing settlements                       #
# --------------------------------------------------------------------------- #

def submit_settlement(settlement, *, requested_by):
    """Put a prepared settlement forward for the platform's two-person approval.

    ``requested_by`` is a platform finance operator: the workflow engine refuses
    anybody outside the platform tenant, since the batch is the platform's. The
    platform's settlement route is published first if it is missing
    (:func:`vs_payments.approvals.ensure_settlement_approval_template`). Replaying
    a submitted settlement returns its existing approval.
    """
    from .approvals import ensure_settlement_approval_template
    from .services import submit_payout_batch_for_approval

    if settlement.status != HeldSettlementStatus.PENDING or settlement.batch_id is None:
        raise PaymentStateError("Only a pending settlement with a transfer to make can be put forward.")
    ensure_settlement_approval_template(settlement.batch.entity.tenant)
    return submit_payout_batch_for_approval(settlement.batch, requested_by=requested_by)


def settlements_due(*, today=None):
    """Every settlement a platform operator has to act on: pending ones, and any built today.

    Across every tenant, newest first. "Today" is the platform's day.
    """
    from .models import HeldSettlement

    platform = platform_books()
    today = today or (_today(platform.tenant) if platform is not None else timezone.localdate())
    return (HeldSettlement.objects
            .filter(Q(status=HeldSettlementStatus.PENDING) | Q(run_on=today))
            .select_related("tenant", "branch", "entity", "bank_account", "batch")
            .order_by("-id"))
