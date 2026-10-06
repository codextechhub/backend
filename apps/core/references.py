"""Reading a reference that may be a row's code or its id.

Many write endpoints accept either: ``"inventory_account": "1400"`` names the
account by its code, ``"inventory_account": 57`` by its id. The two are told
apart by the JSON type, never by trying one and then the other:

* a JSON number is an **id**, and is only ever looked up as one;
* a string is a **code**, and is read as an id only when no code matches and
  the string is all digits, so a form post or a query parameter carrying an id
  as text still resolves.

Trying the code first for a number is what goes wrong. Ledger account codes are
all digits, and ids are a sequence that every school's chart shares, so ids
climb through the same range codes live in. Once Corona's books hold an
account with id 5100 ("Alternate inventory"), an edit that sends ``5100`` to
move a stock item onto it finds the account *coded* "5100" (Office Supplies)
first: the edit is refused as "not an asset account", or, if the code it
collides with happens to be another asset, silently files the stock under the
wrong account. Whether that happens depends only on how many accounts every
tenant has created before, which is why it looks like chance.
"""
from __future__ import annotations


def names_an_id(ref) -> bool:
    """Whether *ref* is a JSON number, and so names a row by id alone.

    ``bool`` is excluded: JSON ``true`` is an ``int`` subclass in Python, and is
    a malformed reference rather than the id 1.
    """
    return isinstance(ref, int) and not isinstance(ref, bool)


def find_by_code_or_id(queryset, ref, *, code=None, code_field: str = "code"):
    """The row of *queryset* that *ref* names, or ``None`` when none matches.

    *ref* is read as the module docstring describes. ``code`` maps the string to
    the stored form of a code (``str.upper`` where codes are kept upper case);
    without it the string is matched exactly as sent. Blank references are the
    caller's to handle before calling.
    """
    if names_an_id(ref):
        return queryset.filter(pk=ref).first()
    text = str(ref)
    row = queryset.filter(**{code_field: code(text) if code else text}).first()
    if row is None and text.isdigit():
        row = queryset.filter(pk=int(text)).first()
    return row


def pick_by_code_or_id(ref, by_code, by_id, *, code=None):
    """:func:`find_by_code_or_id` over rows already loaded into two dicts.

    For a list of references resolved with one query: ``by_code`` maps each
    row's stored code to the row and ``by_id`` its id. A string reference is
    stripped of surrounding spaces before it is matched.
    """
    if names_an_id(ref):
        return by_id.get(ref)
    text = str(ref or "").strip()
    row = by_code.get(code(text) if code else text)
    if row is None and text.isdigit():
        row = by_id.get(int(text))
    return row
