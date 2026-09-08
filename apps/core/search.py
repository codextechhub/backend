"""Finding a person by typing roughly who you mean.

Every directory in this product had the same search and the same defect: each
field was matched against the WHOLE query, so a box that looked like it searched
for people searched for a string that had to sit inside one column. The
consequence is the thing users try first - typing somebody's full name returned
nothing at all, because "Sunday Ekpo" is in no single field.

What a person actually types is a rough handle for somebody they already have in
mind: a first name, two first names, a couple of syllables, initials. So this
matches three ways, from tightest to loosest, and RANKS by which one hit, so a
loose match never buries an exact one:

    "sunday"        contains          rank 0   the substring is simply there
    "sunday ekpo"   word prefixes     rank 1   every token starts a word, in any order
    "su ek"         word prefixes     rank 1
    "sue"           subsequence       rank 2   s-u-e appear in order, anywhere

The third is what makes a three-letter guess work and is also the one that
brings company: "sue" reaches Su-nday Ekpo, and equally Sam-u-el Ad-e-yemo and
Ibrahim S-u-l-e. All three are honest hits and none is better than the others,
so a short query returns candidates rather than an answer - which is what a
three-letter query is.

What the ranking buys is the case where one hit IS better: "sul" begins a word
in Ibrahim Sule and only threads through Samuel Adeyemo, so Sule comes first.
Without it a coincidence could sort alphabetically above the person somebody
was plainly typing.

**Postgres only.** ``\\y`` is Postgres' word boundary; this project runs Postgres
everywhere including tests. A database without it would need the word-prefix
rank rewritten, and the other two would still work.
"""
from __future__ import annotations

import re

from django.db.models import Case, CharField, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce, Concat

#: How many characters of a query are used for the subsequence pass.
#:
#: A long query is somebody typing a name, not guessing at one, and running a
#: 40-character subsequence over every row buys nothing: the word-prefix pass
#: has already answered it or there is no answer. The cap also bounds the regex
#: the database compiles.
SUBSEQUENCE_MAX = 12

#: Below this, a subsequence is not a guess, it is every row.
#:
#: One character subsequence-matches almost everybody. Two is where a query
#: starts carrying information, and the word-prefix pass still answers a single
#: letter properly - "s" finds everybody whose name starts with S.
SUBSEQUENCE_MIN = 2


def _blob(fields):
    """The fields joined into one searchable string, with separators.

    Separated by spaces so word boundaries survive the join: without them
    "Sunday" and "Ekpo" would concatenate into "SundayEkpo" and a word-prefix
    search for "ek" would miss.

    Two details the database forces and neither is cosmetic. Every field is
    COALESCED, because concatenating a NULL in Postgres yields NULL - one
    missing middle name would blank the whole blob and drop that person out of
    every search. And the output type is declared, because the fields are not
    all the same type (an email address is its own field type) and Django
    refuses to guess between them.
    """
    text = CharField()
    parts = []
    for index, field in enumerate(fields):
        if index:
            parts.append(Value(" "))
        parts.append(Coalesce(field, Value(""), output_field=text))
    return Concat(*parts, output_field=text)


def loose_match(query, *, fields, blob="search_blob"):
    """A filter and a rank for a roughly-typed query.

    Returns ``(condition, rank)`` for a queryset annotated with ``blob``, or
    ``(None, None)`` for a query not worth running - callers skip filtering
    entirely then, rather than filtering on nothing and returning everybody.

    ``fields`` is only used by :func:`annotate_blob`; it is named here so a
    caller reads the two together.
    """
    query = (query or "").strip()
    if not query:
        return None, None

    tokens = [token for token in re.split(r"\s+", query) if token]
    contains = Q(**{f"{blob}__icontains": query})

    # Every token has to start SOME word, and the words may be in any order:
    # "ekpo sunday" is the same person as "sunday ekpo", and a reader who types
    # a surname first is not wrong.
    prefixes = Q()
    for token in tokens:
        prefixes &= Q(**{f"{blob}__iregex": r"\y" + re.escape(token)})

    condition = contains | prefixes
    # Ranked in the order the docstring lists, so an exact hit is never below a
    # coincidence. Ordering by this and then by name is what makes the loosest
    # pass safe to include at all.
    whens = [
        When(contains, then=Value(0)),
        When(prefixes, then=Value(1)),
    ]

    letters = re.sub(r"\s+", "", query)[:SUBSEQUENCE_MAX]
    if len(letters) >= SUBSEQUENCE_MIN:
        subsequence = Q(**{
            f"{blob}__iregex": ".*".join(re.escape(char) for char in letters),
        })
        condition = condition | subsequence
        whens.append(When(subsequence, then=Value(2)))

    rank = Case(*whens, default=Value(3), output_field=IntegerField())
    return condition, rank


def annotate_blob(queryset, fields, *, blob="search_blob"):
    """Put the searchable string on the queryset under ``blob``."""
    return queryset.annotate(**{blob: _blob(fields)})


def search(queryset, query, *, fields, blob="search_blob", then=()):
    """Narrow and order a queryset by a roughly-typed query.

    The whole helper in one call, because the three steps are meaningless
    apart: annotating without filtering costs a concatenation nobody reads, and
    filtering without ordering puts a three-letter coincidence above the person
    whose name was typed in full.

    ``then`` is the ordering to apply after the rank - the list's own order, so
    equally-good matches still come back in the order the screen expects.
    """
    condition, rank = loose_match(query, fields=fields, blob=blob)
    if condition is None:
        return queryset
    return (
        annotate_blob(queryset, fields, blob=blob)
        .annotate(**{f"{blob}_rank": rank})
        .filter(condition)
        .order_by(f"{blob}_rank", *then)
    )
