"""Words an audit row carries that only some readers may read.

The trail is append-only: a row is written once and never changed. Some rows
carry words another module guards with a Field Access switch, typically the
free-text reason a member of staff gave for an action. Newer rows keep those
words in ``metadata`` and out of the summary, but older rows may end their
summary with them, and the trail can never be rewritten to take them out.

So the words are hidden on read. The module that writes such rows registers a
:class:`ProtectedWords` rule from its ``AppConfig.ready()``, naming the
Field Access key that governs the words and the rows that carry them. Every
audit surface (the event list and detail, the entity trail, the CSV export,
the Export Centre's audit dataset, and any module's own history screen) asks
this module what a reader may see:

* a summary that carries the words is cut where they begin (``summary_marker``);
* the words are left out of ``metadata``;
* a search over summaries matches the cut summary, never the words, so a
  reader cannot find which row the words belong to by searching for them.
  The cut is a database expression (:func:`visible_summary_expression`), so a
  search stays one query however large the trail.

A reader whose roles grant Read on the key sees the row as it was written.
Whether a reader may read is Field Access's ordinary answer, evaluated in the
reader's own tenant (:func:`vs_rbac.field_enforcement.can_read`): a school's
staff by their school roles, CodeX staff by their platform roles, and the
Vision super admin always. A CodeX support operator reading a school's trail
therefore sees that a pupil was suspended and not why, until CodeX opens the
field on that operator's own role.

This module names no domain and imports no app; a rule arrives only because
the app that writes the rows registered it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.db.models import Case, F, Q, TextField, Value, When
from django.db.models.functions import Left, StrIndex


@dataclass(frozen=True)
class ProtectedWords:
    """The rows of one entity type whose words one Field Access key governs.

    A row carries the words when its ``action_type`` is one of
    ``action_types`` and its ``metadata`` holds every key in
    ``metadata_keys``. The rule is declared as data rather than as a function
    so the same answer can be asked of one row in Python (:meth:`matches`) and
    of the whole table in SQL (:meth:`q`), which is what lets a search skip
    the words without reading the rows into Python. ``metadata_key`` is where a
    newer row keeps the words, and ``summary_marker`` is where they begin in an
    older row's summary.
    """

    field_key: str
    entity_type: str
    action_types: frozenset
    metadata_keys: frozenset = frozenset()
    metadata_key: str = "reason"
    summary_marker: str = " Reason: "

    def matches(self, event) -> bool:
        """Whether *event* carries this rule's words."""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        return (
            event.entity_type == self.entity_type
            and event.action_type in self.action_types
            and self.metadata_keys <= metadata.keys()
        )

    def q(self, prefix: str = "") -> Q:
        """The rows this rule names, as a ``Q`` over ``AuditEvent``."""
        q = Q(**{
            f"{prefix}entity_type": self.entity_type,
            f"{prefix}action_type__in": sorted(self.action_types),
        })
        if self.metadata_keys:
            q &= Q(**{f"{prefix}metadata__has_keys": sorted(self.metadata_keys)})
        return q


_RULES: dict[str, ProtectedWords] = {}


def register_protected_words(name: str, rule: ProtectedWords) -> None:
    """Register *rule* under *name*; registering a name again replaces it."""
    _RULES[name] = rule


def rule_for(event) -> ProtectedWords | None:
    """The rule whose words *event* carries, or ``None``."""
    for rule in _RULES.values():
        if rule.matches(event):
            return rule
    return None


def split_summary(event) -> tuple[str, str | None, ProtectedWords | None]:
    """*event*'s summary without the protected words, the words, and the rule.

    The words are the metadata's when the row holds them there, otherwise
    what follows the marker in the summary. For a row no rule names, the
    summary comes back whole with ``None`` for both.
    """
    summary = event.summary or ""
    rule = rule_for(event)
    if rule is None:
        return summary, None, None
    text, _, tail = summary.partition(rule.summary_marker)
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return text, metadata.get(rule.metadata_key) or tail, rule


def reader_for(request) -> Callable[[str], bool]:
    """Whether the request's caller may read a Field Access key.

    ``request=None`` reads everything, the escape hatch Field Access gives a
    render that acts for nobody.
    """
    from vs_rbac.field_enforcement import can_read

    return lambda key: can_read(request, key)


def visible_summary(event, can_read: Callable[[str], bool]) -> str:
    """The summary *event* shows a reader, cut where hidden words begin."""
    text, _, rule = split_summary(event)
    if rule is None or can_read(rule.field_key):
        return event.summary
    return text


def visible_metadata(event, can_read: Callable[[str], bool]):
    """*event*'s metadata without the words a reader may not read.

    The stored value is never touched: a reader who may not read the words
    gets a copy without the key.
    """
    rule = rule_for(event)
    if rule is None or can_read(rule.field_key) or not isinstance(event.metadata, dict):
        return event.metadata
    return {k: v for k, v in event.metadata.items() if k != rule.metadata_key}


def visible_summary_expression(can_read: Callable[[str], bool]):
    """The summary each row shows a reader, as a database expression.

    The SQL counterpart of :func:`visible_summary`, for a search or an export
    that must not match or write the words a reader may not read: every row a
    hidden rule names is cut where its marker begins, and every other row is
    its summary. A reader who may read every rule's words gets the summary
    column itself, so their search is the plain one.
    """
    hidden = [rule for rule in _RULES.values() if not can_read(rule.field_key)]
    if not hidden:
        return F("summary")
    return Case(
        *(
            When(
                rule.q() & Q(summary__contains=rule.summary_marker),
                then=Left(
                    "summary", StrIndex("summary", Value(rule.summary_marker)) - 1,
                ),
            )
            for rule in hidden
        ),
        default=F("summary"),
        output_field=TextField(),
    )


def search_summary(queryset, can_read: Callable[[str], bool], *, alias="visible_summary"):
    """*queryset* with :func:`visible_summary_expression` aliased as *alias*.

    Returns the queryset and the lookup path a search should use: the alias
    when some words are hidden from this reader, ``summary`` otherwise.
    """
    expression = visible_summary_expression(can_read)
    if isinstance(expression, F):
        return queryset, "summary"
    return queryset.alias(**{alias: expression}), alias
