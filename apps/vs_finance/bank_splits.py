"""Cut a legacy shared bank account over to branch-owned bank ledgers.

The legacy bank record, statements, reconciliations and journal lines stay in
place as historical evidence. A cutover opens one new bank ledger per
explicitly named branch against the same physical account, carries each
branch's agreed share of the legacy balance into it, and only after every
SYSTEM journal posts inactivates the legacy records.

A branch's book balance on the legacy ledger (the sum of its own entries on it)
rarely equals the share the bursars agree it takes. The person splitting
chooses, per split, how that difference is treated
(:class:`~vs_finance.constants.BankSplitDifferenceTreatment`):

* **Debt between branches** (the default). Each difference becomes an
  inter-branch balance, booked as a ``BANK_SPLIT`` transfer
  (:func:`vs_finance.inter_branch.book_bank_split_difference`) so it shows on the
  pair balances and in the register, and the owing branch repays it later with a
  cash transfer. Retained earnings do not move at any branch.
* **Permanent move.** Each branch's legacy balance is cleared against retained
  earnings and each successor opened against retained earnings, so the
  difference becomes a permanent shift of equity between branches.

When every share equals its book balance there is no difference to treat, and
either choice posts one plain reclassification per branch from the legacy
ledger to its new one.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q, Sum

from vs_config.clock import tenant_today

from .account_mappings import resolve_mapped_account
from .audit import record
from .branch_ledger import ledger_lines
from .constants import (
    ACCOUNT_CODE_LENGTH,
    AccountMappingKey,
    AccountType,
    BankLineStatus,
    BankSplitDifferenceTreatment,
    FinanceAuditAction,
    JournalSource,
    NormalBalance,
    account_type_from_code,
)
from .exceptions import BankAccountSplitError
from .money import format_naira
from .posting import post_journal, resolve_period


MAX_SPLIT_BRANCHES = 100


@dataclass(frozen=True)
class BankAccountSplitAllocationResult:
    """One successor row safe for the cutover API response."""

    id: int
    branch_id: int
    name: str
    gl_account_id: int
    gl_account_code: str
    opening_balance: int
    is_primary: bool
    is_primary_collection: bool


@dataclass(frozen=True)
class BankAccountSplitResult:
    """The durable records produced by one completed cutover.

    ``transfers`` are the ``BANK_SPLIT`` inter-branch transfers a debt treatment
    booked, empty under a permanent move or when no share differed.
    """

    legacy_balance: int
    bank_accounts: tuple
    allocations: tuple[BankAccountSplitAllocationResult, ...]
    journals: tuple
    difference_treatment: str = BankSplitDifferenceTreatment.DEBT
    transfers: tuple = ()


def match_differences(differences):
    """Pair branches that give up cash with branches that receive it, as debts.

    ``differences`` maps each branch id to its book balance on the shared ledger
    less its agreed share. A positive difference is a surplus branch, whose
    cash another branch keeps; a negative one is a deficit branch, which keeps
    cash that is not its own. Returns ``(owed_branch_id, owing_branch_id,
    amount)`` rows, each a debt the owing branch owes the owed one.

    The largest remaining deficit is settled against the largest remaining
    surplus, repeatedly, ties to the lower branch id, so the result is the same
    on every run and never needs more than one debt fewer than the branches
    involved. Bright Star's Ikeja, Lekki, Yaba and Ajah differ by +N5,000,
    +N3,000, -N4,000 and -N4,000: Yaba (the lower id of the two largest
    deficits) owes Ikeja N4,000, leaving Ikeja N1,000 surplus; Ajah's N4,000
    then meets the largest surplus left, Lekki's N3,000, and the last N1,000 of
    it Ikeja's. Three debts: Yaba owes Ikeja N4,000, Ajah owes Lekki N3,000 and
    Ajah owes Ikeja N1,000.
    """
    surplus = {branch: amount for branch, amount in differences.items() if amount > 0}
    deficit = {branch: -amount for branch, amount in differences.items() if amount < 0}
    if sum(surplus.values()) != sum(deficit.values()):
        raise BankAccountSplitError(
            "The branch differences do not cancel, so they cannot be paired as debts."
        )
    debts = []
    while deficit:
        owing = min(deficit, key=lambda branch: (-deficit[branch], branch))
        owed = min(surplus, key=lambda branch: (-surplus[branch], branch))
        amount = min(deficit[owing], surplus[owed])
        debts.append((owed, owing, amount))
        deficit[owing] -= amount
        surplus[owed] -= amount
        if not deficit[owing]:
            del deficit[owing]
        if not surplus[owed]:
            del surplus[owed]
    return debts


def branch_book_balances(entity, ledger, split_date) -> dict:
    """Each branch's own balance on a shared bank ledger at the end of ``split_date``.

    ``{branch_id: kobo}``, debits less credits of the posted ledger lines each
    branch's entries hold on ``ledger`` dated on or before ``split_date``, in
    branch id order. A branch whose entries net to nothing keeps its key with 0.
    Lines of entries not yet given a branch sit under ``None``, last; a split
    refuses while any remain, and its preview reports them.

    This is the one reading of a branch's book balance: the split measures each
    branch's difference from its agreed share against it, and the preview shows
    it before anyone agrees a share, so the two cannot drift apart.
    """
    from .branch_ledger import ledger_lines

    rows = (
        ledger_lines(entity)
        .filter(account=ledger, entry__date__lte=split_date)
        .values("entry__branch_id")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
    )
    balances = {
        row["entry__branch_id"]: int(row["debit"] or 0) - int(row["credit"] or 0)
        for row in rows
    }
    return dict(sorted(balances.items(), key=lambda item: (item[0] is None, item[0] or 0)))


def split_preview(bank_account, *, split_date) -> dict:
    """What splitting a shared bank account on ``split_date`` would start from.

    ``legacy_balance`` is the whole ledger's balance, ``unbranched_balance`` the
    part sitting in entries not yet given a branch (the split refuses until it is
    0), and ``branches`` lists every in-service branch of the tenant with its own
    book balance (:func:`branch_book_balances`), plus any other branch whose
    entries touch the ledger, so the bursars agreeing the shares see every
    figure the split will measure them against. Bright Star's shared GTBank
    holds N4,000: Ikeja's entries come to N5,000 and Lekki's to minus N1,000, so
    the form opens on those two figures before anyone types an agreed share.

    Reads only; refuses an account that already belongs to a branch.
    """
    from vs_tenants.models import Branch

    if bank_account.branch_id is not None:
        raise BankAccountSplitError(
            f"Bank account {bank_account.name} already belongs to a branch and is not shared."
        )
    entity = bank_account.entity
    balances = branch_book_balances(entity, bank_account.gl_account, split_date)
    unbranched = balances.pop(None, 0)
    branches = Branch.all_objects.filter(tenant_id=entity.tenant_id).filter(
        Q(status__in=Branch.IN_SERVICE_STATES) | Q(pk__in=list(balances)),
    ).order_by("name", "pk").values_list("pk", "name")
    return {
        "legacy_balance": sum(balances.values()) + unbranched,
        "unbranched_balance": unbranched,
        "split_date": split_date.isoformat(),
        "branches": [
            {"branch_id": pk, "branch_name": name, "book_balance": balances.get(pk, 0)}
            for pk, name in branches
        ],
    }


def _branch_id(value):
    """Return an allocation's branch primary key without trusting its tenant."""
    return getattr(value, "pk", value)


def _clean_allocations(entity, allocations):
    """Validate allocation shape and resolve every branch in one tenant-bounded query."""
    from vs_tenants.models import Branch

    rows = list(allocations or [])
    if not 2 <= len(rows) <= MAX_SPLIT_BRANCHES:
        raise BankAccountSplitError(
            f"Name between 2 and {MAX_SPLIT_BRANCHES} branch allocations."
        )

    branch_ids = [_branch_id(row.get("branch")) for row in rows]
    if any(value in (None, "") for value in branch_ids):
        raise BankAccountSplitError("Every allocation must name its branch.")
    if len(set(branch_ids)) != len(branch_ids):
        raise BankAccountSplitError("Every allocation must name a distinct branch.")

    branches = {
        branch.pk: branch
        for branch in Branch.all_objects.filter(
            tenant_id=entity.tenant_id,
            pk__in=branch_ids,
            status__in=Branch.IN_SERVICE_STATES,
        )
    }
    if set(branch_ids) != set(branches):
        raise BankAccountSplitError(
            "An allocation branch does not belong to these books or is not in service."
        )

    bank_names = []
    ledger_names = []
    codes = []
    cleaned = []
    for row, branch_id in zip(rows, branch_ids):
        try:
            opening_balance = int(row["opening_balance"])
        except (KeyError, TypeError, ValueError):
            raise BankAccountSplitError("Every allocation needs an integer opening_balance.")
        bank_name = str(row.get("bank_account_name") or "").strip()
        ledger_code = str(row.get("ledger_account_code") or "").strip()
        ledger_name = str(row.get("ledger_account_name") or "").strip()
        if not bank_name:
            raise BankAccountSplitError("Every successor bank account needs a name.")
        if not ledger_name:
            raise BankAccountSplitError("Every successor ledger account needs a name.")
        if len(bank_name) > 160 or len(ledger_name) > 160:
            raise BankAccountSplitError("Successor account names cannot exceed 160 characters.")
        if not ledger_code.isdigit() or len(ledger_code) != ACCOUNT_CODE_LENGTH:
            raise BankAccountSplitError(
                f"Every successor ledger code must contain exactly {ACCOUNT_CODE_LENGTH} digits."
            )
        for flag in ("is_primary", "is_primary_collection"):
            if flag not in row or type(row[flag]) is not bool:
                raise BankAccountSplitError(f"Every allocation must explicitly set {flag}.")
        bank_names.append(bank_name)
        ledger_names.append(ledger_name)
        codes.append(ledger_code)
        cleaned.append({
            "branch": branches[branch_id],
            "opening_balance": opening_balance,
            "bank_account_name": bank_name,
            "ledger_account_code": ledger_code,
            "ledger_account_name": ledger_name,
            "is_primary": row["is_primary"],
            "is_primary_collection": row["is_primary_collection"],
        })

    if len(set(bank_names)) != len(bank_names):
        raise BankAccountSplitError("Every successor bank account name must be unique.")
    if len(set(ledger_names)) != len(ledger_names):
        raise BankAccountSplitError("Every successor ledger account name must be unique.")
    if len(set(codes)) != len(codes):
        raise BankAccountSplitError("Every successor ledger account code must be unique.")
    if sum(row["is_primary"] for row in cleaned) > 1:
        raise BankAccountSplitError("Choose at most one successor as the entity's primary account.")
    return cleaned


@transaction.atomic
def split_shared_bank_account(
    bank_account,
    allocations,
    *,
    split_date,
    agreement_reference,
    difference_treatment=BankSplitDifferenceTreatment.DEBT,
    actor_user=None,
):
    """Move one legacy unbranched bank ledger into explicitly agreed branch balances.

    ``split_date`` is the cutover accounting date. No posted movement may exist
    after it, so the exact balance validated here is also the full live legacy
    balance, and every branch the split names (by allocation or by history)
    must be open on it, its own branch close included.

    ``difference_treatment`` says what happens where a branch's book balance on
    the legacy ledger differs from its agreed share (see the module docstring):

    * ``DEBT``: the differences are paired as debts (:func:`match_differences`)
      and each booked as a ``BANK_SPLIT`` inter-branch transfer that moves the
      cash between the branches' sides of the legacy ledger. Each branch's side
      then equals its agreed share and is reclassified into its new ledger.
    * ``PERMANENT_MOVE``: each historical branch's legacy cash is cleared against
      retained earnings, then each successor's agreed balance is opened against
      retained earnings. That bridge cancels across the tenant.

    Either way each branch's own balance sheet receives the agreed cash or
    overdraft, the treatment is named in every journal's narration and in the
    split's audit entry, and when no share differs both treatments post the
    same plain reclassification. The transaction locks both legacy rows, and any
    failure rolls back every successor, transfer and journal.
    """
    from .inter_branch import book_bank_split_difference, ensure_branches_open
    from .models import Account, BankAccount, JournalEntry, JournalLine

    if difference_treatment not in BankSplitDifferenceTreatment.values:
        raise BankAccountSplitError(
            "Choose how a branch's difference is treated: "
            + " or ".join(BankSplitDifferenceTreatment.values) + "."
        )
    treatment = BankSplitDifferenceTreatment(difference_treatment)
    agreement_reference = str(agreement_reference or "").strip()
    if not agreement_reference:
        raise BankAccountSplitError(
            "Give the bursar's agreement reference for these opening balances."
        )
    if len(agreement_reference) > 64:
        raise BankAccountSplitError("The agreement reference cannot exceed 64 characters.")
    if split_date is None:
        raise BankAccountSplitError("Give the split date.")

    source = (
        BankAccount.objects.select_for_update()
        .select_related("entity__tenant", "gl_account")
        .get(pk=bank_account.pk)
    )
    source_before = {
        "is_active": source.is_active,
        "is_primary": source.is_primary,
        "is_primary_collection": source.is_primary_collection,
    }
    legacy = Account.objects.select_for_update().get(pk=source.gl_account_id)
    if split_date > tenant_today(source.entity.tenant):
        raise BankAccountSplitError("The split date cannot be after the tenant's current date.")
    if not source.is_active:
        raise BankAccountSplitError(f"Bank account {source.name} is already inactive.")
    if source.branch_id is not None:
        raise BankAccountSplitError(
            f"Bank account {source.name} already belongs to a branch and is not shared."
        )
    if not (legacy.is_active and legacy.is_postable):
        raise BankAccountSplitError("The legacy ledger must be active and postable at cutover.")
    if legacy.account_type != AccountType.ASSET or legacy.normal_balance != NormalBalance.DEBIT:
        raise BankAccountSplitError("A bank split requires an asset ledger with a debit balance.")

    rows = _clean_allocations(source.entity, allocations)
    names = [row["bank_account_name"] for row in rows]
    ledger_names = [row["ledger_account_name"] for row in rows]
    codes = [row["ledger_account_code"] for row in rows]
    if BankAccount.objects.filter(entity=source.entity, name__in=names).exists():
        raise BankAccountSplitError("A successor bank account name already exists in these books.")
    if Account.objects.filter(entity=source.entity, code__in=codes).exists():
        raise BankAccountSplitError("A successor ledger account code already exists in these books.")
    if Account.objects.filter(entity=source.entity, name__in=ledger_names).exists():
        raise BankAccountSplitError("A successor ledger account name already exists in these books.")
    if any(account_type_from_code(code) != legacy.account_type for code in codes):
        raise BankAccountSplitError(
            "Every successor ledger code must belong to the legacy account's account type."
        )

    selected_primary = any(row["is_primary"] for row in rows)
    if selected_primary and BankAccount.objects.filter(
        entity=source.entity,
        is_active=True,
        is_primary=True,
    ).exclude(pk=source.pk).exists():
        raise BankAccountSplitError("Another active bank account is already the entity primary.")
    collection_branch_ids = [
        row["branch"].pk for row in rows if row["is_primary_collection"]
    ]
    if collection_branch_ids and BankAccount.objects.filter(
        entity=source.entity,
        branch_id__in=collection_branch_ids,
        is_primary_collection=True,
    ).exclude(pk=source.pk).exists():
        raise BankAccountSplitError("A selected branch already has a primary collection account.")

    future_statement_line = (
        source.statement_lines.filter(txn_date__gt=split_date)
        .order_by("txn_date", "pk")
        .values_list("txn_date", flat=True)
        .first()
    )
    if future_statement_line is not None:
        raise BankAccountSplitError(
            f"The physical bank account has statement activity after {split_date}; "
            f"the first line is dated {future_statement_line}."
        )
    future_statement = (
        source.statements.filter(statement_date__gt=split_date)
        .order_by("statement_date", "pk")
        .values_list("statement_date", flat=True)
        .first()
    )
    if future_statement is not None:
        raise BankAccountSplitError(
            f"The physical bank account has a statement ending after {split_date}; "
            f"the first ends on {future_statement}."
        )
    unmatched_line = (
        source.statement_lines.filter(
            txn_date__lte=split_date,
            status=BankLineStatus.UNMATCHED,
        )
        .order_by("txn_date", "pk")
        .values_list("txn_date", flat=True)
        .first()
    )
    if unmatched_line is not None:
        raise BankAccountSplitError(
            "Reconcile or explicitly ignore every bank statement line through the "
            f"cutover date; an unmatched line remains on {unmatched_line}."
        )

    future = (
        ledger_lines(source.entity)
        .filter(account=legacy, entry__date__gt=split_date)
        .order_by("entry__date", "entry_id")
        .values_list("entry__date", flat=True)
        .first()
    )
    if future is not None:
        raise BankAccountSplitError(
            f"The legacy ledger has posted movement after {split_date}; "
            f"the first is dated {future}. Reverse or move it before cutover."
        )
    legacy_lines = ledger_lines(source.entity).filter(
        account=legacy,
        entry__date__lte=split_date,
    )
    if legacy_lines.filter(entry__branch__isnull=True).exists():
        raise BankAccountSplitError(
            "The legacy ledger still has posted movement without a branch. Run the "
            "branch backfill and place every flagged journal before this cutover."
        )
    book_balances = branch_book_balances(source.entity, legacy, split_date)
    historical_balances = [
        (branch_id, balance) for branch_id, balance in book_balances.items() if balance
    ]
    if book_balances:
        from vs_tenants.models import Branch

        historical_branch_ids = set(book_balances)
        owned_branch_ids = set(
            Branch.all_objects.filter(
                tenant_id=source.entity.tenant_id,
                pk__in=historical_branch_ids,
                status__in=Branch.IN_SERVICE_STATES,
            ).values_list("pk", flat=True)
        )
        if historical_branch_ids != owned_branch_ids:
            raise BankAccountSplitError(
                "The legacy ledger has posted movement assigned outside an in-service "
                "branch of these books. Correct its branch before cutover."
            )
    legacy_balance = sum(balance for _, balance in historical_balances)
    agreed_total = sum(row["opening_balance"] for row in rows)
    if agreed_total != legacy_balance:
        raise BankAccountSplitError(
            f"The agreed branch opening balances sum to {format_naira(agreed_total)}, but the "
            f"legacy ledger balance on {split_date} is {format_naira(legacy_balance)}."
        )

    book = dict(historical_balances)
    agreed = {row["branch"].pk: row["opening_balance"] for row in rows}
    differences = {
        branch_id: book.get(branch_id, 0) - agreed.get(branch_id, 0)
        for branch_id in sorted(set(book) | set(agreed))
    }
    has_difference = any(differences.values())
    ensure_branches_open(source.entity, split_date, *differences)
    period = resolve_period(source.entity, split_date)
    if not has_difference:
        treatment_note = "no difference between book balances and agreed shares"
    elif treatment == BankSplitDifferenceTreatment.DEBT:
        treatment_note = "differences kept as debts between branches"
    else:
        treatment_note = "differences moved permanently through retained earnings"

    def post_pair(branch_id, narration, description, debit_account, credit_account, amount):
        """Post one two-line SYSTEM journal at ``branch_id``; a negative amount swaps sides."""
        entry = JournalEntry.objects.create(
            entity=source.entity,
            branch_id=branch_id,
            date=split_date,
            period=period,
            source=JournalSource.SYSTEM,
            narration=f"{narration} ({treatment_note})"[:255],
            reference=agreement_reference,
            created_by=actor_user,
        )
        line_rows = (
            ((debit_account, amount, 0), (credit_account, 0, amount))
            if amount > 0
            else ((credit_account, -amount, 0), (debit_account, 0, -amount))
        )
        for line_no, (account, debit, credit) in enumerate(line_rows, start=1):
            JournalLine.objects.create(
                entry=entry,
                account=account,
                debit=debit,
                credit=credit,
                description=description[:255],
                line_no=line_no,
            )
        post_journal(entry, actor_user=actor_user)
        return entry

    permanent = has_difference and treatment == BankSplitDifferenceTreatment.PERMANENT_MOVE
    retained_earnings = None
    created_banks = []
    journals = []
    transfers = []
    if permanent:
        retained_earnings = resolve_mapped_account(
            source.entity,
            AccountMappingKey.RETAINED_EARNINGS,
            label="retained earnings",
        )
        for branch_id, amount in historical_balances:
            journals.append(post_pair(
                branch_id,
                f"Clear legacy branch bank balance from {source.name}",
                "Clear the legacy bank balance at branch cutover",
                retained_earnings,
                legacy,
                amount,
            ))
    elif has_difference:
        for owed, owing, amount in match_differences(differences):
            transfer = book_bank_split_difference(
                source.entity,
                from_branch=owed,
                to_branch=owing,
                amount=amount,
                split_date=split_date,
                shared_ledger=legacy,
                purpose=(
                    f"Difference on splitting shared bank account {source.name}, "
                    f"kept as a debt between branches ({agreement_reference})"
                ),
                reference=agreement_reference,
                actor_user=actor_user,
            )
            transfers.append(transfer)
            journals.extend(
                leg.journal for leg in transfer.legs.select_related("journal").order_by("role")
            )

    for row in rows:
        new_ledger = Account.objects.create(
            entity=source.entity,
            parent=legacy.parent,
            code=row["ledger_account_code"],
            name=row["ledger_account_name"],
            account_type=legacy.account_type,
            normal_balance=legacy.normal_balance,
            currency=legacy.currency,
            is_contra=legacy.is_contra,
            is_postable=True,
            is_active=True,
            subtype=legacy.subtype,
            description=f"Branch bank ledger cut over from account {legacy.code}.",
            ifrs_line=legacy.ifrs_line,
        )
        new_bank = BankAccount.objects.create(
            entity=source.entity,
            branch=row["branch"],
            gl_account=new_ledger,
            name=row["bank_account_name"],
            bank_name=source.bank_name,
            account_number=source.account_number,
            currency=source.currency,
            is_active=True,
            is_primary=row["is_primary"],
            is_primary_collection=row["is_primary_collection"],
            gateway_subaccount_code="",
            gateway_subaccount_provider="",
            settlement_bank_code=source.settlement_bank_code,
        )
        created_banks.append(new_bank)
        amount = row["opening_balance"]
        if amount == 0:
            continue
        journals.append(post_pair(
            row["branch"].pk,
            f"Branch bank cutover from {source.name}",
            f"Agreed opening balance for {row['branch'].name}",
            new_ledger,
            retained_earnings if permanent else legacy,
            amount,
        ))

    remaining = legacy_lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    remaining_balance = int(remaining["debit"] or 0) - int(remaining["credit"] or 0)
    if remaining_balance != 0:
        raise BankAccountSplitError(
            f"The legacy ledger still carries {format_naira(remaining_balance)} after cutover."
        )

    source.is_active = False
    source.is_primary = False
    source.is_primary_collection = False
    source.save(update_fields=[
        "is_active", "is_primary", "is_primary_collection", "updated_at",
    ])
    legacy.is_active = False
    legacy.save(update_fields=["is_active", "updated_at"])
    audit_allocations = [
        {
            "branch_id": row["branch"].pk,
            "bank_account_id": bank.pk,
            "ledger_account_id": bank.gl_account_id,
            "ledger_account_code": row["ledger_account_code"],
            "opening_balance": row["opening_balance"],
            "is_primary": row["is_primary"],
            "is_primary_collection": row["is_primary_collection"],
        }
        for row, bank in zip(rows, created_banks)
    ]
    record(
        entity=source.entity,
        action=FinanceAuditAction.ACCOUNT_UPDATED,
        actor_user=actor_user,
        target=source,
        message=(
            f"Split legacy bank account {source.name} into {len(created_banks)} "
            f"branch accounts on {split_date}, {treatment_note}."
        ),
        before=source_before,
        after={"is_active": False, "is_primary": False, "is_primary_collection": False},
        operation="SHARED_BANK_ACCOUNT_SPLIT",
        split_date=str(split_date),
        legacy_balance=legacy_balance,
        legacy_ledger_account_id=legacy.pk,
        difference_treatment=treatment.value,
        retained_earnings_account_id=getattr(retained_earnings, "pk", None),
        branch_differences=[
            {
                "branch_id": branch_id,
                "book_balance": book.get(branch_id, 0),
                "agreed_balance": agreed.get(branch_id, 0),
                "difference": difference,
            }
            for branch_id, difference in differences.items()
        ],
        inter_branch_transfer_ids=[transfer.pk for transfer in transfers],
        historical_branch_balances=[
            {"branch_id": branch_id, "balance": balance}
            for branch_id, balance in historical_balances
        ],
        agreement_reference=agreement_reference,
        new_bank_account_ids=[bank.pk for bank in created_banks],
        journal_ids=[entry.pk for entry in journals],
        allocations=audit_allocations,
    )
    return BankAccountSplitResult(
        legacy_balance=legacy_balance,
        bank_accounts=tuple(created_banks),
        allocations=tuple(
            BankAccountSplitAllocationResult(
                id=bank.pk,
                branch_id=row["branch"].pk,
                name=bank.name,
                gl_account_id=bank.gl_account_id,
                gl_account_code=row["ledger_account_code"],
                opening_balance=row["opening_balance"],
                is_primary=row["is_primary"],
                is_primary_collection=row["is_primary_collection"],
            )
            for row, bank in zip(rows, created_banks)
        ),
        journals=tuple(journals),
        difference_treatment=treatment.value,
        transfers=tuple(transfers),
    )
