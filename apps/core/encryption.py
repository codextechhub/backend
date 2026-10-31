"""Encryption of the few columns too sensitive to keep as plain text.

A child's blood group, allergies and conditions, a member of staff's tax ID
and pension PIN, and the bank account numbers money is paid into are stored
encrypted (:class:`core.fields.EncryptedTextField`). The disk under the
database is encrypted by the host too; this layer is for everything that is
not the disk: a database dump, a backup copy, a console session, a read-only
replica handed to someone for a report.

**Keys.** ``settings.FIELD_ENCRYPTION_KEYS`` is a list of Fernet keys. The
first encrypts; every one of them decrypts, so a key is rotated by putting the
new one first, re-saving the rows (``manage.py reencrypt_fields``), then
removing the old one. Local, test and CI runs use :data:`DEV_KEY`; a deployed
server refuses to start with it or with none (``apps/settings/staging.py``).
A lost key is lost data: the key lives in the host's environment settings and
must be kept somewhere else as well.

**Stored form.** ``enc1:`` followed by the Fernet token. A blank value stays
blank, so "has this child any allergies on file" is still a plain query and an
empty field costs nothing. A value without the prefix is read as it is, which
is what lets the migration that turns encryption on find the rows written
before it.

**Copies.** The record history keeps the encrypted form
(:mod:`vs_history.registry`), and audit entries never carry these values
whole (:func:`withhold`): a medical value is withheld outright, and an account
number, tax ID or pension PIN keeps its last four characters, so a trail can
still show that a bank account was changed and to which one.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

#: The key every non-deployed run uses. Public on purpose: it protects nothing,
#: and a deployed server refuses to start with it.
DEV_KEY = "eHZzLWRldmVsb3BtZW50LWtleS1ub3QtYS1zZWNyZXQ="

PREFIX = "enc1:"

#: What an audit entry holds in place of a withheld value.
WITHHELD = "[withheld]"

#: How an audit entry shows a sealed value: not at all, or by its last four.
WITHHOLD, LAST_FOUR = "withhold", "last_four"

#: Field name -> how audit entries show its values; filled by
#: :class:`core.fields.EncryptedTextField` as models load.
SEALED_NAMES: dict[str, str] = {}


class DecryptionError(Exception):
    """A stored value none of the configured keys opens."""


@lru_cache(maxsize=4)
def _cipher(keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(key.encode()) for key in keys])


def cipher() -> MultiFernet:
    keys = tuple(key.strip() for key in settings.FIELD_ENCRYPTION_KEYS if key.strip())
    if not keys:
        raise DecryptionError("FIELD_ENCRYPTION_KEYS names no key.")
    return _cipher(keys)


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    """The stored form of *value*: blanks and already-encrypted values pass through."""
    if value in (None, "") or is_encrypted(value):
        return value
    return PREFIX + cipher().encrypt(str(value).encode()).decode()


def decrypt(value):
    """The plain text of a stored *value*; a value without the prefix is returned as is."""
    if not is_encrypted(value):
        return value
    try:
        return cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "A stored value could not be decrypted with any configured key."
        ) from exc


def withhold(data):
    """*data* with every value under a sealed field name hidden.

    Walks nested dicts and lists, so a diff of ``{"allergies": {"from": "nuts",
    "to": ""}}`` keeps its shape and says only which side was set. Blanks stay
    blank, so "added" and "removed" still read as such.
    """
    if isinstance(data, list):
        return [withhold(item) for item in data]
    if not isinstance(data, dict):
        return data
    out = {}
    for key, value in data.items():
        if key in SEALED_NAMES:
            out[key] = _hide(value, SEALED_NAMES[key])
        else:
            out[key] = withhold(value)
    return out


def _hide(value, how):
    if isinstance(value, dict):
        return {key: _hide(inner, how) for key, inner in value.items()}
    if isinstance(value, list):
        return [_hide(item, how) for item in value]
    if value in (None, "") or value == WITHHELD:
        return value
    text = str(value)
    if how == LAST_FOUR and len(text) > 4 and not text.startswith("*"):
        return "****" + text[-4:]
    return WITHHELD


# --------------------------------------------------------------------------- #
# Migrations that turn encryption on for a column                              #
# --------------------------------------------------------------------------- #

def encrypt_rows(model, fields) -> None:
    """Re-save each non-blank value of *fields* so it is stored encrypted.

    For a migration's historical *model* whose *fields* are already
    :class:`~core.fields.EncryptedTextField`: a row written before reads back as
    it was (no prefix), and writing it again encrypts it.
    """
    for row in model._base_manager.values("pk", *fields).iterator():
        values = {name: row[name] for name in fields if row[name] not in (None, "")}
        if values:
            model._base_manager.filter(pk=row["pk"]).update(**values)


def decrypt_rows(model, fields) -> None:
    """The reverse of :func:`encrypt_rows`: write each value back as plain text."""
    from django.db.models import TextField, Value

    for row in model._base_manager.values("pk", *fields).iterator():
        values = {
            name: Value(row[name], output_field=TextField())
            for name in fields if row[name] not in (None, "")
        }
        if values:
            model._base_manager.filter(pk=row["pk"]).update(**values)


def encrypt_history(record_version, record_type, fields, *, reverse=False) -> None:
    """Encrypt (or, reversed, decrypt) *fields* in every stored version of *record_type*."""
    step = decrypt if reverse else encrypt
    versions = record_version._base_manager.filter(record_type=record_type)
    for version in versions.iterator():
        data = dict(version.data)
        for name in fields:
            if data.get(name) not in (None, ""):
                data[name] = step(data[name])
        if data != version.data:
            record_version._base_manager.filter(pk=version.pk).update(data=data)
