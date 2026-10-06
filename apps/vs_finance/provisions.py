"""The doubtful-debt provision: an allowance sized by how long debts have been owed.

Direct write-off alone carries every debt at full value until somebody concedes
it, so a six-year-old debt sits on the balance sheet beside last month's. A
provision run ages what is owed on ``as_of`` and requires an allowance of the
policy's rate on each overdue balance (by default 25% past 180 days, 50% past 365
and 100% past 730; :mod:`vs_finance.receivables_policy`). It posts the difference
between that and the allowance already held, per branch:
``Dr bad debts, Cr allowance for doubtful debts`` to raise it, the reverse to
release it. One journal per branch, each naming its branch, and the run itself
names none: it is raised by a caller whose reach is the whole tenant, and is
approved like the other receivable adjustments before it posts.

A write-off then uses the allowance before it touches expense
(:func:`vs_finance.credit_notes.allowance_available`), so the loss is recognised
once, when the debt ages, not again when it is conceded.

Greenfield's December run finds N850,000 of Mr Obi's fees 400 days overdue at
Ikeja: the allowance must hold N425,000 there. Ikeja holds N100,000 from last
year, so the run posts N325,000 to bad debts for Ikeja alone.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
)
from .exceptions import FinanceError, PostingError
from .money import format_naira
from .wording import state_word


def _rate_for(days_overdue: int, bands) -> int:
    """The basis-point rate for a balance ``days_overdue`` past due: the highest band passed."""
    rate = 0
    for over_days, band_rate in bands:
        if days_overdue > over_days:
            rate = band_rate
    return rate


def _share(amount: int, rate_bps: int) -> int:
    value = Decimal(int(amount)) * Decimal(int(rate_bps)) / Decimal(10_000)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _branch_key(only_branch, branch_id):
    """A transaction's branch, with an unbranched one folded into a lone branch."""
    return only_branch if branch_id is None and only_branch is not None else branch_id


def required_allowance(entity, as_of, *, bands=None) -> dict:
    """The allowance each branch must hold on ``as_of``: ``{branch_id: {required, bands}}``.

    Every posted bill and debit note still owed on ``as_of`` is aged from its due
    date (or its own date when it has none), as the ageing report does, and
    provided for at the rate of the highest band it has passed. ``bands`` defaults
    to the entity's policy.
    """
    from vs_rbac.scoping import only_branch_id

    from .receivables_policy import provision_bands, resolve_receivables_policy
    from .reports import _ar_snapshot

    bands = bands if bands is not None else provision_bands(resolve_receivables_policy(entity))
    invoices, debit_notes, settled_by_invoice, settled_by_note, _credit = _ar_snapshot(
        entity, as_of=as_of)
    only = only_branch_id(entity.tenant_id)
    result: dict = defaultdict(lambda: {"required": 0, "bands": defaultdict(lambda: [0, 0])})
    items = [
        (inv.branch_id, int(inv.total) - settled_by_invoice.get(inv.pk, 0),
         inv.due_date or inv.invoice_date)
        for inv in invoices
    ] + [
        (note.branch_id, int(note.total) - settled_by_note.get(note.pk, 0), note.note_date)
        for note in debit_notes
    ]
    for branch_id, owed, since in items:
        if owed <= 0:
            continue
        days = (as_of - since).days
        rate = _rate_for(days, bands)
        if not rate:
            continue
        need = _share(owed, rate)
        row = result[_branch_key(only, branch_id)]
        row["required"] += need
        band = row["bands"][str(max(d for d, r in bands if days > d))]
        band[0] += owed
        band[1] += need
    return {
        key: {"required": row["required"],
              "bands": {k: {"owed": v[0], "required": v[1]} for k, v in row["bands"].items()}}
        for key, row in result.items()
    }


def held_allowance(entity, as_of) -> dict:
    """The allowance each branch holds on ``as_of``, from the ledger: ``{branch_id: kobo}``."""
    from vs_rbac.scoping import only_branch_id

    from .branch_ledger import ledger_lines

    account = resolve_mapped_account(entity, AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE,
                                     label="allowance for doubtful debts")
    only = only_branch_id(entity.tenant_id)
    held: dict = defaultdict(int)
    for row in (ledger_lines(entity).filter(account=account, entry__date__lte=as_of)
                .values("entry__branch_id").annotate(debit=Sum("debit"), credit=Sum("credit"))):
        held[_branch_key(only, row["entry__branch_id"])] += (
            int(row["credit"] or 0) - int(row["debit"] or 0))
    return dict(held)


def _work_out(provision):
    """Recompute ``provision``'s per-branch lines from today's books. Returns the lines."""
    from .models import DoubtfulDebtProvisionLine
    from .receivables_policy import provision_bands, resolve_receivables_policy

    bands = provision_bands(resolve_receivables_policy(provision.entity))
    required = required_allowance(provision.entity, provision.as_of, bands=bands)
    held = held_allowance(provision.entity, provision.as_of)
    provision.lines.all().delete()
    lines = []
    for branch_id in sorted(set(required) | set(held), key=lambda b: (b is None, b or 0)):
        need = required.get(branch_id, {}).get("required", 0)
        have = held.get(branch_id, 0)
        if need == 0 and have == 0:
            continue
        lines.append(DoubtfulDebtProvisionLine.objects.create(
            provision=provision, branch_id=branch_id, required=need, current=have,
            movement=need - have, bands=required.get(branch_id, {}).get("bands", {}),
        ))
    provision.required_total = sum(line.required for line in lines)
    provision.movement_total = sum(abs(line.movement) for line in lines)
    provision.policy_snapshot = [{"over_days": d, "rate_bps": r} for d, r in bands]
    provision.save(update_fields=[
        "required_total", "movement_total", "policy_snapshot", "updated_at"])
    return lines


@transaction.atomic
def prepare_provision(entity, *, as_of, narration="", actor_user=None):
    """Raise a draft provision run with its figures worked out for ``as_of``."""
    from .models import DoubtfulDebtProvision
    from .posting import resolve_period

    resolve_period(entity, as_of)  # A run dated where nothing can post is refused now.
    provision = DoubtfulDebtProvision.objects.create(
        entity=entity, branch=None, as_of=as_of, narration=narration or "",
        created_by=actor_user,
    )
    _work_out(provision)
    return provision


def check_provision(provision) -> None:
    """The posting guards, without writing: a period that takes the journals, and the accounts.

    The run posts one journal per branch whose allowance moves, so its date must be
    open at each of those branches: a run dated in a September Ikeja has closed is
    refused while it still moves Ikeja's allowance, whatever Lekki's September says.
    """
    from .posting import ensure_date_open

    branch_ids = sorted({
        line.branch_id for line in provision.lines.all() if line.movement
    }) or [None]
    for branch_id in branch_ids:
        ensure_date_open(provision.entity, provision.as_of, branch=branch_id)
    resolve_mapped_account(provision.entity, AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE,
                           label="allowance for doubtful debts")
    resolve_mapped_account(provision.entity, AccountMappingKey.BAD_DEBT_EXPENSE,
                           label="bad-debt expense")


def post_provision(provision, *, actor_user=None):
    """Post a draft or approved provision run; records a rejection on failure."""
    try:
        return _post_provision_atomic(provision, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=provision.entity, action=FinanceAuditAction.PROVISION_POSTED,
            exc=exc, actor_user=actor_user, target=provision,
        )
        raise


@transaction.atomic
def _post_provision_atomic(provision, *, actor_user=None):
    """Work the figures out again and post one journal per branch whose allowance moves."""
    from .models import DoubtfulDebtProvision, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period

    provision = DoubtfulDebtProvision.objects.select_for_update(of=("self",)).get(pk=provision.pk)
    if provision.status not in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):
        raise PostingError(
            f"Provision {provision.document_number} is {state_word(provision)}; only a draft "
            f"or approved run can be posted.",
        )
    entity = provision.entity
    allowance = resolve_mapped_account(entity, AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE,
                                       label="allowance for doubtful debts")
    expense = resolve_mapped_account(entity, AccountMappingKey.BAD_DEBT_EXPENSE,
                                     label="bad-debt expense")
    period = resolve_period(entity, provision.as_of)
    lines = _work_out(provision)
    for line in lines:
        if line.movement == 0:
            continue
        amount = abs(int(line.movement))
        raising = line.movement > 0
        journal = JournalEntry.objects.create(
            entity=entity, branch_id=line.branch_id, date=provision.as_of, period=period,
            source=JournalSource.CLOSING, created_by=actor_user,
            narration=provision.narration or f"Doubtful-debt provision {provision.document_number}",
            reference=provision.document_number,
        )
        JournalLine.objects.create(
            entry=journal, account=expense if raising else allowance, debit=amount, credit=0,
            description="Bad debts provided" if raising else "Allowance released", line_no=1,
        )
        JournalLine.objects.create(
            entry=journal, account=allowance if raising else expense, debit=0, credit=amount,
            description="Allowance for doubtful debts" if raising else "Bad debts released",
            line_no=2,
        )
        post_journal(journal, actor_user=actor_user)
        line.journal = journal
        line.save(update_fields=["journal", "updated_at"])
    provision.status = DocumentStatus.POSTED
    provision.save(update_fields=["status", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PROVISION_POSTED,
        actor_user=actor_user, target=provision,
        message=(f"Posted doubtful-debt provision {provision.document_number}: allowance "
                 f"required {format_naira(provision.required_total)}."),
        as_of=str(provision.as_of), required=provision.required_total,
        lines=[{"branch_id": line.branch_id, "required": line.required,
                "current": line.current, "movement": line.movement,
                "journal_id": line.journal_id} for line in lines],
        bands=provision.policy_snapshot,
    )
    return provision
