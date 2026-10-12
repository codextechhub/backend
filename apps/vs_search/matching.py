"""How a typed query is matched against places, and how the matches are ordered.

Pure functions over :class:`~vs_search.registry.Place`; nothing here touches the
database, so the rules are tested without one.

A place matches when **every** word of the query is the start of some word in
its label, its keywords or its description. "due date" therefore finds "Days
until an invoice is due" through its keyword "due date", and "inv due" finds it
through its label, while "invoice xyz" finds nothing.

A match is ranked by the weakest field any query word needed: a place whose
label alone carries every word ranks above one that needed a keyword, which
ranks above one that needed its description. Ties read alphabetically by label,
so the order is stable from one keystroke to the next.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .registry import Place

#: What a word is: letters and digits, case folded.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

LABEL_RANK = 0
KEYWORD_RANK = 1
DESCRIPTION_RANK = 2


def tokenise(text: str) -> list[str]:
    """The words in *text*, lower-cased; punctuation and underscores separate words."""
    return _WORD.findall((text or "").casefold())


def _prefixes_any(token: str, words: Iterable[str]) -> bool:
    return any(word.startswith(token) for word in words)


def match_rank(place: Place, query_tokens: list[str]) -> int | None:
    """The rank of *place* for these query words, or None when it does not match."""
    if not query_tokens:
        return None
    label = tokenise(place.label)
    keywords = tokenise(" ".join(place.keywords))
    description = tokenise(place.description)
    worst = LABEL_RANK
    for token in query_tokens:
        if _prefixes_any(token, label):
            continue
        if _prefixes_any(token, keywords):
            worst = max(worst, KEYWORD_RANK)
        elif _prefixes_any(token, description):
            worst = max(worst, DESCRIPTION_RANK)
        else:
            return None
    return worst


def rank_places(places: Iterable[Place], query: str) -> list[Place]:
    """The places matching *query*, best first."""
    tokens = tokenise(query)
    scored = []
    for place in places:
        rank = match_rank(place, tokens)
        if rank is not None:
            scored.append((rank, place.label.casefold(), place.key, place))
    scored.sort(key=lambda row: row[:3])
    return [row[3] for row in scored]
