"""The school's word for a term, for every sentence the backend writes about one.

A school says Term or Semester (``academics.terms.word``, see
``academic_rules.py``), and every refusal, warning and label that names the
thing, in academics, the calendar and the fee labels alike, asks here rather
than spelling "term" itself. One helper, so a school that says Semester never
reads "term" in one corner of the product and "semester" in another.

Only the generic word changes. A term's own name ("First Term") is the school's
stored text and is printed as stored, whatever the word.
"""
from __future__ import annotations

from ..constants import TermWord

_WORDS = {
    TermWord.TERM.value: ("term", "terms"),
    TermWord.SEMESTER.value: ("semester", "semesters"),
}


def word_for(choice: str, *, plural: bool = False, capital: bool = False) -> str:
    """The word *choice* (TERM or SEMESTER) stands for, in the form asked for."""
    singular, many = _WORDS.get(choice, _WORDS[TermWord.TERM.value])
    word = many if plural else singular
    return word.capitalize() if capital else word


def term_word(tenant, *, plural: bool = False, capital: bool = False) -> str:
    """The school's word: "term", "semesters", "Term" and so on.

    Two or three queries (``academic_rules.read_term_word``). A caller that
    words many rows reads the choice once and passes it to :func:`word_for`.
    """
    from .academic_rules import read_term_word

    return word_for(read_term_word(tenant), plural=plural, capital=capital)
