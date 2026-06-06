"""Control-account resolution by Chart-of-Accounts code.

Phase-4 services (payroll, depreciation, expense claims, bank charges) book to
well-known control accounts — PAYE payable, accumulated depreciation, accrued
reimbursement and so on. They look those accounts up *by code* through this single
helper rather than hard-coding ids, so an entity with a customised chart fails loudly
(a clear :class:`MissingAccountError`) instead of posting into the wrong place.
"""
from __future__ import annotations

from .exceptions import MissingAccountError


def resolve_account(entity, code: str, *, label: str = ""):
    """Return the active, postable :class:`Account` with ``code`` for ``entity``.

    Raises :class:`~vs_finance.exceptions.MissingAccountError` when the account is
    absent, inactive or a non-postable header — a misconfigured chart should never
    silently swallow a posting.
    """
    from .models import Account

    account = (
        Account.objects
        .filter(entity=entity, code=code, is_active=True, is_postable=True)
        .first()
    )
    if account is None:
        raise MissingAccountError(code, label=label)
    return account
