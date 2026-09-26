"""Splitting a guardian's name typed as one line into first, middle and last.

A guardian's name was once a single ``full_name``. Every name typed that way,
before or since, is split here, and the split is always flagged for a person to
confirm (``Guardian.name_needs_review``), because no rule reads a name reliably:
"Adaeze Okafor-Bello" and "Chukwuemeka Obi Nnamdi" are both three words to a
machine, and only somebody who knows the family knows which word is the
surname.

The rule is deliberately plain. Leading honorifics are set aside ("Mrs",
"Chief", "Alhaji"), the first remaining word is the first name, the last is the
last name, and anything between is the middle name. One word is a first name
with no last name, which the review flag then asks a person to complete.
"""
from __future__ import annotations

#: Honorifics a school commonly types in front of a parent's name. Compared
#: without case or a trailing full stop, and only at the start of the name.
HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "mister", "madam", "dr", "prof", "professor",
    "engr", "arc", "barr", "rev", "revd", "pastor", "evang", "deacon",
    "deaconess", "chief", "hon", "sir", "lady", "alhaji", "alhaja", "hajia",
    "mallam", "otunba", "prince", "princess", "col", "capt", "gen", "lt",
    "maj", "sgt",
})


def split_full_name(text: str) -> tuple[str, str, str]:
    """``(first, middle, last)`` read from a name typed on one line."""
    words = (text or "").split()
    while len(words) > 1 and words[0].rstrip(".").lower() in HONORIFICS:
        words.pop(0)
    if not words:
        return "", "", ""
    if len(words) == 1:
        return words[0], "", ""
    return words[0], " ".join(words[1:-1]), words[-1]


def compose_full_name(first: str, middle: str, last: str) -> str:
    """The one-line name, from whichever parts are present."""
    return " ".join(part.strip() for part in (first, middle, last) if part and part.strip())
