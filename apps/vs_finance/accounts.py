"""Control-account resolution by Chart-of-Accounts code.

Phase-4 services (payroll, depreciation, expense claims, bank charges) book to
well-known control accounts — PAYE payable, accumulated depreciation, accrued
reimbursement and so on. They look those accounts up *by code* through this single
helper rather than hard-coding ids, so an entity with a customised chart fails loudly
(a clear :class:`MissingAccountError`) instead of posting into the wrong place.
"""
from __future__ import annotations  # Defer annotation evaluation for lightweight imports.

from .exceptions import MissingAccountError  # Raised when a required posting account is unavailable.


def resolve_account(entity, code: str, *, label: str = ""):  # Resolve a configured chart account by code.
    """Return the active, postable :class:`Account` with ``code`` for ``entity``.

    Raises :class:`~vs_finance.exceptions.MissingAccountError` when the account is
    absent, inactive or a non-postable header — a misconfigured chart should never
    silently swallow a posting.
    """
    from .models import Account  # Import lazily to avoid loading finance models at module import time.

    account = (  # Query only usable accounts for the requested entity and code.
        Account.objects  # Start from the account manager.
        .filter(entity=entity, code=code, is_active=True, is_postable=True)  # Require active postable leaf account.
        .first()  # Return one matching account or None.
    )  # Close the grouped expression.
    if account is None:  # Missing or unusable accounts are configuration errors.
        raise MissingAccountError(code, label=label)  # Raise the domain error for this path.
    return account  # Return the resolved posting account.
