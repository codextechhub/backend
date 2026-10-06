"""A list's word filters: each value selects exactly the rows that wear that word.

A list screen offers a filter whose options are the words its rows show
("Draft", "Awaiting approval", "Part-paid"), and the server reads the chosen
word. Two failures follow when each list reads its own way, and both widen the
list without a word:

* a value the list does not know is ignored, so a typo or a client that sends
  "draft" where the list expects "DRAFT" gets every row back;
* a word is read as the nearest stored status rather than the derived one the
  row wears, so "Approved" also lists part-paid claims.

:func:`filter_by_word` is the one reader. A list declares each word it offers
and the rows that word means (a ``Q`` or a function of the queryset), the value
is matched in any case, and anything else is refused with a 400 on the
parameter naming the words it takes. The export behind a list translates the
same words into its own filters, so the file holds the rows the list shows.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError


def word_value(value, words, *, param: str = "status"):
    """``value`` as the matching word of ``words``, or ``None`` when none is given.

    Matched ignoring case and surrounding spaces. A value that is not one of
    ``words`` is refused (400 on ``param``), listing the words in the order
    given.
    """
    if value is None or not str(value).strip():
        return None
    wanted = str(value).strip().upper()
    for word in words:
        if str(word).upper() == wanted:
            return word
    raise ValidationError({param: f"Use one of {', '.join(str(w) for w in words)}."})


def filter_by_word(queryset, value, rules: dict, *, param: str = "status"):
    """``queryset`` narrowed to the rows the word ``value`` names; unchanged when none is given.

    ``rules`` maps each word the list offers to the rows it means: a ``Q``, or a
    function taking the queryset and returning it narrowed. A word outside
    ``rules`` is refused (:func:`word_value`).
    """
    word = word_value(value, list(rules), param=param)
    if word is None:
        return queryset
    rule = rules[word]
    return rule(queryset) if callable(rule) else queryset.filter(rule)
