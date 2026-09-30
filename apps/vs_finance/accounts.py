"""Control-account resolution by Chart-of-Accounts code.

Phase-4 services (payroll, depreciation, expense claims, bank charges) book to
well-known control accounts - PAYE payable, accumulated depreciation, accrued
reimbursement and so on. They look those accounts up *by code* through this single
helper rather than hard-coding ids, so an entity with a customised chart fails loudly
(a clear :class:`MissingAccountError`) instead of posting into the wrong place.
"""
from __future__ import annotations

from .exceptions import MissingAccountError


def account_subtree_ids(account) -> set[int]:
    """Return an account id and every descendant id in the same entity."""
    from .models import Account

    children_by_parent: dict[int | None, list[int]] = {}
    for account_id, parent_id in (
        Account.objects.filter(entity=account.entity)
        .values_list("id", "parent_id")
    ):
        children_by_parent.setdefault(parent_id, []).append(account_id)

    subtree = {account.id}
    pending = [account.id]
    while pending:
        child_ids = [
            child_id
            for child_id in children_by_parent.get(pending.pop(), [])
            if child_id not in subtree
        ]
        subtree.update(child_ids)
        pending.extend(child_ids)
    return subtree


# Resolve a configured chart account by code.
def resolve_account(entity, code: str, *, label: str = ""):
    """Return the active, postable :class:`Account` with ``code`` for ``entity``.

    Raises :class:`~vs_finance.exceptions.MissingAccountError` when the account is
    absent, inactive or a non-postable header - a misconfigured chart should never
    silently swallow a posting.
    """
    from .models import Account

    account = (  # Query only usable accounts for the requested entity and code.
        Account.objects
        .filter(entity=entity, code=code, is_active=True, is_postable=True)
        .first()
    )
    if account is None:  # Missing or unusable accounts are configuration errors.
        raise MissingAccountError(code, label=label)
    return account  # Return the resolved posting account.


#: Role -> (account types accepted, contra INCOME accepted, what the role is, e.g.).
_ACCOUNT_ROLES = {
    "receivable": ({"ASSET"}, False, "a receivable (asset) account", "1200 Accounts receivable"),
    "allowance": ({"INCOME", "EXPENSE"}, True,
                  "a contra-revenue or expense account", "4910 Discounts & concessions"),
    "write_off": ({"EXPENSE"}, True, "an expense account", "5300 Bad debts"),
}


def require_account_kind(account, role, *, entity=None):
    """Refuse an account of the wrong kind for the part it plays in an AR posting.

    Receivable postings trust the accounts they are handed. A customer whose
    receivable account is bank account 1100 would have their next bill post
    ``Dr bank, Cr revenue``: income backed by cash that never arrived. A concession
    booked to a liability, or a write-off booked to an asset, is the same mistake
    on the other side. So each role accepts only its own kind: a receivable is a
    (non-contra) asset that holds no money (not the cash-and-bank account, a bank's
    or a petty cash fund's), a concession's allowance is revenue (usually a contra
    account) or expense, and a bad-debt write-off is an expense or a contra-revenue
    allowance. The account must also belong to ``entity`` when one is given.
    """
    from .exceptions import PostingError

    if account is None:
        return
    types, contra_income, what, example = _ACCOUNT_ROLES[role]
    acceptable = (
        (account.account_type in types and not (account.account_type == "ASSET" and account.is_contra))
        or (contra_income and account.account_type == "INCOME" and account.is_contra)
    )
    if entity is not None and account.entity_id != entity.pk:
        acceptable = False
    if acceptable and role == "receivable" and _holds_money(account):
        acceptable = False
    if not acceptable:
        raise PostingError(
            f"Account {account.code} {account.name} cannot be used here; choose {what}, "
            f"for example {example}.",
        )


def _holds_money(account) -> bool:
    """Whether ``account`` is cash: the cash-and-bank mapping, a bank's or a petty cash fund's."""
    from .account_mappings import resolve_mapped_account
    from .constants import AccountMappingKey
    from .models import BankAccount, PettyCashFund

    if BankAccount.objects.filter(gl_account=account).exists():
        return True
    if PettyCashFund.objects.filter(gl_account=account).exists():
        return True
    try:
        cash = resolve_mapped_account(account.entity, AccountMappingKey.CASH_BANK)
    except MissingAccountError:
        return False
    return cash.pk == account.pk


def accounts_a_caller_may_name(request, qs):
    """Narrow an :class:`Account` queryset to the accounts ``request``'s caller may name on a write.

    A ledger account that backs a bank account is that bank's money under another
    name. Naming it as a deposit, credit, counter or journal-line account moves the
    bank's money exactly as naming the bank account would, so it answers to the
    bank list's rule: the caller's own branches' accounts only, because a bank
    account is a transaction container (:func:`vs_rbac.scoping.transaction_branch_scope`).
    Ikeja's bursar who types the code of Lekki's collection ledger gets the same
    answer as for a code that does not exist. A ledger account behind no bank
    account is untouched, and an unbound caller is never narrowed.

    Reads are not narrowed here: a report or list filtered by account already
    returns only the reader's own rows, and the chart lists every account.
    """
    from django.db.models import Q
    from vs_rbac.scoping import transaction_branch_scope

    from .models import BankAccount

    scope = transaction_branch_scope(request)
    if not scope.is_narrowed:
        return qs
    reachable = BankAccount.objects.filter(scope.q())
    return qs.filter(Q(bank_account__isnull=True) | Q(bank_account__in=reachable))
