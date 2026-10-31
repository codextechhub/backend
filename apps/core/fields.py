"""Model fields shared across apps."""
from __future__ import annotations

from django.core.exceptions import FieldError
from django.db import models
from django.db.models.lookups import Exact, IsNull

from . import encryption


class _BlankOnlyExact(Exact):
    """``field=""`` still works; any other value would never match, so it is refused."""

    def get_prep_lookup(self):
        if self.rhs not in ("", None):
            raise FieldError(
                f"{self.lhs.target.name} is encrypted, so it cannot be matched against a "
                f"value in the database. Compare in Python after loading the rows."
            )
        return super().get_prep_lookup()


class EncryptedTextField(models.TextField):
    """Text stored encrypted (:mod:`core.encryption`), read back as plain text.

    ``max_length`` limits the plain text, as it would on a ``CharField``: forms
    and serializers enforce it, the column does not, because the stored form is
    longer. Each save encrypts afresh, so the same text never looks the same
    twice in the table; the only lookups are ``isnull`` and ``=""``.

    Its name joins :data:`core.encryption.SEALED_NAMES`, which keeps the value
    out of audit entries: withheld, or with ``audit=LAST_FOUR`` reduced to its
    last four characters.
    """

    description = "Text stored encrypted"

    def __init__(self, *args, audit=encryption.WITHHOLD, **kwargs):
        self.audit = audit
        super().__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name, *args, **kwargs):
        super().contribute_to_class(cls, name, *args, **kwargs)
        # A migration's historical model knows no ``audit`` (it changes nothing
        # in the database), so only the real model says how audit shows it.
        if cls.__module__ != "__fake__":
            encryption.SEALED_NAMES[name] = self.audit

    def from_db_value(self, value, expression, connection):
        return encryption.decrypt(value)

    def to_python(self, value):
        return encryption.decrypt(super().to_python(value))

    def get_prep_value(self, value):
        return encryption.encrypt(super().get_prep_value(value))

    def get_lookup(self, lookup_name):
        if lookup_name == "exact":
            return _BlankOnlyExact
        if lookup_name == "isnull":
            return IsNull
        return None
