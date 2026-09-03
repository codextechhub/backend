"""email_normalization.py
The one place an email address is folded to the form the database stores.

Why this is a module of its own
Django's ``BaseUserManager.normalize_email`` lowercases only the DOMAIN, so
``Ada@Gmail.com`` comes out of it as ``Ada@gmail.com`` with its capital
intact. PostgreSQL unique indexes are case sensitive, so two rows differing
only in case can sit side by side, while every lookup in this codebase asks
for ``email__iexact`` and takes ``.first()``. Half-normalising is therefore
worse than not normalising at all: it lets a pair exist that no lookup can
tell apart.

Before this module there were two spellings of "already exists" - one using
``iexact`` and one using ``=`` - so one creation path forbade a duplicate the
other happily created. Both now normalise their input here and compare
exactly, which is only sound because ``User.save``/``full_clean`` and the
``ck_user_email_lowercase`` database constraint guarantee every stored
address is already in this form.

This module imports nothing from the project on purpose: the data migration
that repairs historical rows imports it too, and a migration must not drag in
the current models.
"""
from __future__ import annotations


def normalize_email(value: str | None) -> str:
    """Fold an address to its stored form: surrounding space off, all lowercase.

    ``None`` and non-strings fold to ``''`` rather than raising, because every
    caller here is normalising untrusted request data and wants "nothing was
    supplied" to look the same as an empty string.

    Lowercasing the local part is a product decision, not a standards one: RFC
    5321 permits ``Ada@`` and ``ada@`` to be different mailboxes, and no mail
    provider this platform serves actually treats them so. A parent locked out
    of her account because she capitalised her own first name when signing up
    is the failure that would really happen.
    """
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return value.strip().lower()
