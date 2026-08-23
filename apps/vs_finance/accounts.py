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
