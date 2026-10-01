"""Moving confirmed online payments out of gateway clearing and into a bank.

A confirmed collection books its receipt to gateway clearing
(:func:`vs_payments.services._book_receipt`): the provider holds the money. The
provider pays it on to a bank later, usually as one amount for a day's payments,
less its fees. When that amount appears on an imported bank statement, the line
is matched to the collections it carries and one journal books what happened:

    Dr bank (the line: what arrived)   Dr bank charges (the fees)   Cr gateway clearing (the payments)

in the bank account's branch. The line is then reconciled to that journal's bank
line, exactly as a bank adjustment is, so finance's bank reconciliation sees it
matched and unmatching it there reverses the journal and puts the payments back
in clearing (:func:`vs_finance.banking.unmatch_line`).

Mrs Adeyemi pays N180,000 at Bright Star Lekki. Clearing is debited N180,000 and
her invoice is paid. Next day Lekki's Zenith statement shows N178,000 from
Paystack; matched to her payment it books Dr Zenith N178,000, Dr bank charges
N2,000, Cr clearing N180,000, and clearing is back to zero.

:func:`suggest_settlements` proposes which collections a line carries, and
:func:`gateway_clearing_current` is the period-close warning for payments that
have waited in clearing longer than the tenant allows.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import audit
from .constants import CollectionStatus, PaymentAuditAction
from .exceptions import PaymentStateError


def awaiting_settlement_q(prefix: str = "") -> Q:
    """The collections whose money is still in gateway clearing, as a ``Q``.

    Booked to clearing, with a receipt still posted, and no posted settlement.
    A settlement that was unmatched (its journal reversed) leaves them waiting
    again; a receipt that was voided takes them out of clearing altogether. A
    payment the platform held leaves clearing when its held settlement is paid
    (:mod:`vs_payments.held`), even one whose money online payouts had already
    spent, so the settlement booked no journal of its own for it.
    """
    from vs_finance.constants import DocumentStatus

    from .constants import HeldSettlementStatus

    return (
        Q(**{f"{prefix}status": CollectionStatus.SUCCEEDED,
             f"{prefix}clearing_account__isnull": False,
             f"{prefix}payment__status": DocumentStatus.POSTED})
        & (Q(**{f"{prefix}settlement_entry__isnull": True})
           | ~Q(**{f"{prefix}settlement_entry__status": DocumentStatus.POSTED}))
        & ~Q(**{f"{prefix}held_settlement__status": HeldSettlementStatus.PAID})
    )


def _refuse(message, **extra):
    raise PaymentStateError(message, **extra)


@transaction.atomic
def settle_collections(statement_line, collection_ids, *, actor_user=None, posting_date=None):
    """Book one bank statement line as the settlement of the given collections.

    The line must be an unmatched inflow, and every collection one of its entity's
    confirmed payments still in clearing, of the bank account's own branch: a
    Lekki payment settles only into a Lekki account. The fee is what the payments
    came to less what arrived. When the provider reported a fee for every one of
    them, those fees must add up to it, so a line that carries another day's money
    as well is refused rather than booked as a fee.

    ``posting_date`` overrides where the journal lands; omitted, it is the line's
    date, or the first open day after it when that period is closed
    (:func:`vs_finance.banking.resolve_adjustment_date`). Returns the journal.

    The gateway action it writes is filed under the first payment's reference
    rather than the line's, so the transactions log shows the settlement to
    exactly the readers who can see the payments it settled
    (:meth:`vs_payments.reach.PaymentsReach.events`).

    A payment the platform held is refused: its money reaches the branch only
    through the platform's settlement run (:mod:`vs_payments.held`), which books
    its own settlement, so a statement line naming it would book it twice.
    """
    from vs_config.display import format_date
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.audit import record as finance_record
    from vs_finance.banking import resolve_adjustment_date
    from vs_finance.constants import (
        AccountMappingKey, BankLineStatus, BankMatchSource, FinanceAuditAction, JournalSource,
    )
    from vs_finance.models import BankStatementLine, JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period
    from vs_rbac.scoping import only_branch_id, same_transaction_branch

    from .models import CollectionIntent

    line = (BankStatementLine.objects.select_for_update(of=("self",))
            .select_related("bank_account__gl_account", "bank_account__entity",
                            "bank_account__branch")
            .get(pk=statement_line.pk))
    bank = line.bank_account
    entity = bank.entity
    if line.status != BankLineStatus.UNMATCHED:
        _refuse(f"Statement line is '{line.status}'; only an unmatched line can be settled.")
    if line.amount <= 0:
        _refuse("Only money arriving in the bank can settle online payments.")

    ids = sorted({int(pk) for pk in collection_ids})
    if not ids:
        raise ValidationError({"collections": "Name at least one collection this line settles."})
    intents = list(
        CollectionIntent.objects.select_for_update(of=("self",))
        .filter(entity=entity, pk__in=ids).select_related("payment", "settlement_entry")
        .order_by("pk")
    )
    if len(intents) != len(ids):
        raise ValidationError({"collections": "A collection named is not one of these books'."})
    waiting = set(
        CollectionIntent.objects.filter(pk__in=ids).filter(awaiting_settlement_q())
        .values_list("pk", flat=True)
    )
    tenant_id = entity.tenant_id
    for intent in intents:
        if intent.held_by_platform:
            _refuse(
                f"Collection {intent.reference} was held by the platform. It reaches the bank "
                f"through the platform's settlement run, which books its settlement.",
                collection=intent.pk,
            )
        if intent.pk not in waiting:
            _refuse(f"Collection {intent.reference} is not waiting in gateway clearing.",
                    collection=intent.pk)
        if not same_transaction_branch(tenant_id, bank.branch_id, intent.branch_id):
            _refuse(
                f"Collection {intent.reference} belongs to another branch than {bank.name}. "
                f"A branch's online payments settle only into its own bank account.",
                collection=intent.pk,
            )

    gross = sum(int(intent.amount) for intent in intents)
    net = int(line.amount)
    fee = gross - net
    if fee < 0:
        _refuse(f"The line brings {net} kobo, more than the {gross} kobo of payments named.",
                gross=gross, net=net)
    reported = [intent.fee for intent in intents]
    if all(value is not None for value in reported) and sum(reported) != fee:
        _refuse(
            f"The provider's fees on these payments come to {sum(reported)} kobo, but the "
            f"line is {fee} kobo short of them. It carries other payments too, or not all "
            f"of these.",
            gross=gross, net=net, reported_fee=sum(reported),
        )

    branch_id = bank.branch_id or (only_branch_id(tenant_id) if tenant_id else None)
    book_date = resolve_adjustment_date(entity, line.txn_date, requested=posting_date)
    received = max(intent.payment.payment_date for intent in intents)
    if book_date < received:  # Money cannot leave clearing before it entered it.
        tenant = entity.tenant if entity.tenant_id else None
        _refuse(f"The settlement would be booked on {format_date(book_date, tenant)}, before a "
                f"payment it settles was received on {format_date(received, tenant)}.")
    entry = JournalEntry.objects.create(
        entity=entity, branch_id=branch_id, date=book_date,
        period=resolve_period(entity, book_date), source=JournalSource.BANK,
        narration=(f"Settlement of {len(intents)} online payment(s) into {bank.name}")[:255],
        reference=line.reference, created_by=actor_user,
    )
    line_no = 1
    cash_line = JournalLine.objects.create(
        entry=entry, account=bank.gl_account, debit=net, credit=0,
        description="Provider settlement", line_no=line_no,
    )
    if fee:
        line_no += 1
        JournalLine.objects.create(
            entry=entry, debit=fee, credit=0, line_no=line_no,
            account=resolve_mapped_account(entity, AccountMappingKey.BANK_CHARGES,
                                           label="bank charges"),
            description="Payment provider fees",
        )
    by_clearing = defaultdict(int)
    for intent in intents:
        by_clearing[intent.clearing_account_id] += int(intent.amount)
    for account_id, amount in sorted(by_clearing.items()):
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account_id=account_id, debit=0, credit=amount,
            description="Gateway clearing", line_no=line_no,
        )
    post_journal(entry, actor_user=actor_user)

    line.adjusting_journal = entry
    line.matched_line = cash_line
    line.status = BankLineStatus.MATCHED
    line.match_source = BankMatchSource.ADJUSTMENT
    line.reconciled_at = timezone.now()
    line.save(update_fields=[
        "adjusting_journal", "matched_line", "status", "match_source",
        "reconciled_at", "updated_at",
    ])
    CollectionIntent.objects.filter(pk__in=ids).update(
        settlement_entry=entry, updated_at=timezone.now())

    finance_record(
        entity=entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=bank,
        message=(f"Settled {len(intents)} online payment(s) into {bank.name}: "
                 f"{gross} kobo less {fee} kobo fees."),
        bank_account_id=bank.id, journal_id=entry.pk, gross=gross, fee=fee, net=net,
    )
    audit.record(  # Filed under a payment's reference, so it reaches that payment's readers.
        action=PaymentAuditAction.COLLECTIONS_SETTLED, entity=entity,
        reference=intents[0].reference, actor_user=actor_user,
        message=f"Settled {len(intents)} collection(s): {gross} kobo gross, {fee} kobo fees.",
        metadata={"collection_ids": ids, "journal_id": entry.pk,
                  "bank_line_id": line.pk, "gross": gross, "fee": fee, "net": net},
    )
    return entry


def suggest_settlements(entity, collections, bank_lines, *, zone):
    """Pair unmatched inflow lines with the waiting payments they most likely carry.

    Two passes, and a line or a payment is proposed at most once:

    * **By reference.** A line naming one payment's reference (ours or the
      provider's) is that payment's own settlement, when it brings no more than
      the payment and, where the provider reported a fee, exactly the payment less
      it.
    * **By day.** A provider settles a branch's payments of one day as one amount,
      so the rest are grouped by branch and by the day they were confirmed (in
      ``zone``), and a group whose payments less their reported fees equal an
      inflow dated that day or later is proposed. A group with a payment whose fee
      was not reported cannot be priced and is not proposed.

    A line is only ever proposed for payments of its own bank account's branch.
    Suggestions only: nothing is booked until a person confirms one through
    :func:`settle_collections`.
    """
    from vs_rbac.scoping import same_transaction_branch

    def same_branch(line, branch_id):
        return same_transaction_branch(entity.tenant_id, line.bank_account.branch_id, branch_id)

    def proposal(line, branch_id, members, *, basis, day=None):
        gross = sum(int(intent.amount) for intent in members)
        return {
            "bank_line_id": line.pk, "bank_account_id": line.bank_account_id,
            "txn_date": line.txn_date.isoformat(), "branch": branch_id, "basis": basis,
            "confirmed_on": day.isoformat() if day else None,
            "collection_ids": [intent.pk for intent in members],
            "gross": gross, "fee": gross - int(line.amount), "net": int(line.amount),
        }

    open_lines = sorted(
        (line for line in bank_lines if line.amount > 0), key=lambda line: (line.txn_date, line.pk))
    taken: set[int] = set()
    placed: set[int] = set()
    suggestions = []

    by_reference: dict = defaultdict(list)
    for line in open_lines:
        if line.reference:
            by_reference[line.reference.strip()].append(line)
    for intent in collections:
        keys = [key for key in (intent.reference, intent.provider_reference) if key]
        for line in (line for key in keys for line in by_reference.get(key.strip(), [])):
            fee = int(intent.amount) - int(line.amount)
            if line.pk in taken or fee < 0 or (intent.fee is not None and fee != intent.fee):
                continue
            if not same_branch(line, intent.branch_id):
                continue
            taken.add(line.pk)
            placed.add(intent.pk)
            suggestions.append(proposal(line, intent.branch_id, [intent], basis="reference"))
            break

    groups: dict = defaultdict(list)
    for intent in collections:
        if intent.confirmed_at is None or intent.pk in placed:
            continue
        groups[(intent.branch_id, intent.confirmed_at.astimezone(zone).date())].append(intent)
    for (branch_id, day), members in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0] or 0)):
        if any(intent.fee is None for intent in members):
            continue
        net = sum(int(intent.amount) - int(intent.fee) for intent in members)
        for line in open_lines:
            if line.pk in taken or line.amount != net or line.txn_date < day:
                continue
            if not same_branch(line, branch_id):
                continue
            taken.add(line.pk)
            suggestions.append(proposal(line, branch_id, members, basis="day", day=day))
            break
    return suggestions


def gateway_clearing_current(entity, period):
    """Close warning: payments waiting in gateway clearing longer than the tenant allows.

    A provider settles within a day or two, so a payment confirmed more than the
    tenant's ``clearing_stale_days`` (default 7) before the period's end and still
    in clearing means a settlement that never arrived or was never matched. Not
    blocking: a settlement can legitimately land after month end. ``None`` for
    books that have never booked a payment to clearing.
    """
    from vs_config.clock import tenant_zone
    from vs_finance.close import ChecklistItem

    from .custody import custody_row
    from .models import CollectionIntent

    waiting = CollectionIntent.objects.filter(entity=entity).filter(awaiting_settlement_q())
    if not CollectionIntent.objects.filter(entity=entity, clearing_account__isnull=False).exists():
        return None
    tenant = entity.tenant if entity.tenant_id else None
    days = custody_row(tenant).clearing_stale_days
    zone = tenant_zone(tenant) if tenant is not None else datetime.timezone.utc
    cutoff = datetime.datetime.combine(
        period.end_date - datetime.timedelta(days=days), datetime.time.max, tzinfo=zone,
    )
    stale = waiting.filter(confirmed_at__lte=cutoff)
    count = stale.count()
    total = sum(stale.values_list("amount", flat=True)) if count else 0
    return ChecklistItem(
        name="gateway_clearing_current",
        passed=count == 0,
        blocking=False,
        detail=(
            "No online payment has waited in gateway clearing too long"
            if count == 0 else
            f"{count} online payment(s), {total} kobo, confirmed {days} or more days before "
            f"the period end are still in gateway clearing: match their settlement."
        ),
    )


def register():
    """Contribute the clearing check to the finance close. Called from AppConfig.ready."""
    from vs_finance.close import register_close_check

    register_close_check(gateway_clearing_current)
