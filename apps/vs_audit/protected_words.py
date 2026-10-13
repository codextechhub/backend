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

A rule may instead guard values rather than words: ``snapshot_keys`` names the
keys of a row's ``before_data`` and ``diff_data`` that repeat a guarded field,
and a reader who may not read the field gets the snapshots without them
(:func:`visible_snapshot`). Such a rule carries no words, so the summary cut
ignores it. ``metadata_hide`` does the same for the keys of ``metadata`` that
repeat a guarded field, and a rule with an empty ``metadata_key`` guards values
only. A key may be a dotted path (``before.ends_at``) to reach a value inside
a nested dictionary, which is how a configuration change keeps the old and new
state of what it changed.

A module may also declare that its rows record the act and never a value
(:class:`RecordOnly`): who did what to which document, in which branch, and
when. Those rows carry no Field Access key because they carry nothing to guard.
Rows written under the rule hold only a sentence built from the act itself
and an allow-listed ``metadata``; rows written before the rule existed may hold
a sentence and metadata copied from the module's own trail, so every reader
gets those with the sentence rebuilt and the metadata cut to the allow-list,
whatever their roles.

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

    ``snapshot_keys`` makes the rule one over values: the keys of the row's
    ``before_data`` and ``diff_data`` that copy the guarded field. Such a rule
    carries no words, so the summary cut skips it. ``metadata_hide`` names the
    keys of the row's ``metadata`` that copy the guarded field, whether or not
    the rule also carries words, and an empty ``metadata_key`` says the rule
    carries none. A key is a top-level name or a dotted path. ``metadata_match``
    narrows the rule to rows whose metadata holds each ``(key, value)`` pair,
    for an entity type that several kinds of change share.
    """

    field_key: str
    entity_type: str
    action_types: frozenset
    metadata_keys: frozenset = frozenset()
    metadata_key: str = "reason"
    summary_marker: str = " Reason: "
    snapshot_keys: frozenset = frozenset()
    metadata_hide: frozenset = frozenset()
    metadata_match: tuple = ()

    @property
    def carries_words(self) -> bool:
        """Whether the rule guards words (a reason), not only copied values."""
        return bool(self.metadata_key) and not self.snapshot_keys

    def matches(self, event) -> bool:
        """Whether *event* carries this rule's words."""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        return (
            event.entity_type == self.entity_type
            and event.action_type in self.action_types
            and self.metadata_keys <= metadata.keys()
            and all(metadata.get(key) == value for key, value in self.metadata_match)
        )

    def q(self, prefix: str = "") -> Q:
        """The rows this rule names, as a ``Q`` over ``AuditEvent``."""
        q = Q(**{
            f"{prefix}entity_type": self.entity_type,
            f"{prefix}action_type__in": sorted(self.action_types),
        })
        if self.metadata_keys:
            q &= Q(**{f"{prefix}metadata__has_keys": sorted(self.metadata_keys)})
        if self.metadata_match:
            q &= Q(**{f"{prefix}metadata__contains": dict(self.metadata_match)})
        return q


@dataclass(frozen=True)
class RecordOnly:
    """The rows of one module that name an act and never a value.

    A row is the module's when its ``module_key`` is ``module_key``, its
    ``entity_type`` starts with ``entity_prefix``, its ``action_type`` is one of
    ``action_types`` and its ``metadata`` holds every key in ``required_keys``.
    Such a row keeps in ``metadata`` only the keys in ``metadata_allow``.

    ``marker`` is the metadata key a row carries when it was written without
    values. A row without it is older: its stored summary is replaced by
    ``summary_for(event)`` (Python) and ``summary_sql()`` (the same sentence as
    a database expression, for a search or an export), so history is read
    clean without being rewritten.
    """

    module_key: str
    entity_prefix: str
    action_types: frozenset
    required_keys: frozenset
    metadata_allow: frozenset
    marker: str
    summary_for: Callable
    summary_sql: Callable

    def matches(self, event) -> bool:
        """Whether *event* is a row this rule keeps value-free."""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        return (
            getattr(event, "module_key", None) == self.module_key
            and str(event.entity_type or "").startswith(self.entity_prefix)
            and event.action_type in self.action_types
            and self.required_keys <= metadata.keys()
        )

    def q(self) -> Q:
        """The rows this rule names, as a ``Q`` over ``AuditEvent``."""
        return Q(
            module_key=self.module_key,
            entity_type__startswith=self.entity_prefix,
            action_type__in=sorted(self.action_types),
            metadata__has_keys=sorted(self.required_keys),
        )

    def legacy_q(self) -> Q:
        """The rows written before the rule, which carry no :attr:`marker`."""
        return self.q() & ~Q(metadata__has_key=self.marker)


_RULES: dict[str, ProtectedWords] = {}
_RECORD_ONLY: dict[str, RecordOnly] = {}


def register_protected_words(name: str, rule: ProtectedWords) -> None:
    """Register *rule* under *name*; registering a name again replaces it."""
    _RULES[name] = rule


def register_record_only(name: str, rule: RecordOnly) -> None:
    """Register *rule* under *name*; registering a name again replaces it."""
    _RECORD_ONLY[name] = rule


def record_only_rule_for(event) -> RecordOnly | None:
    """The :class:`RecordOnly` rule that keeps *event* value-free, or ``None``."""
    for rule in _RECORD_ONLY.values():
        if rule.matches(event):
            return rule
    return None


def rule_for(event) -> ProtectedWords | None:
    """The rule whose words *event* carries, or ``None``."""
    for rule in _RULES.values():
        if rule.carries_words and rule.matches(event):
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
    """The summary *event* shows a reader, cut where hidden words begin.

    A row a :class:`RecordOnly` rule names shows its stored summary when it was
    written without values and a rebuilt one when it is older.
    """
    record_only = record_only_rule_for(event)
    if record_only is not None:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if record_only.marker in metadata:
            return event.summary
        return record_only.summary_for(event)
    text, _, rule = split_summary(event)
    if rule is None or can_read(rule.field_key):
        return event.summary
    return text


def _without(data: dict, paths) -> dict:
    """A copy of *data* without the keys *paths* name.

    A path is a top-level key or a dotted route into nested dictionaries
    (``before.ends_at``); a route that leads nowhere removes nothing. Only the
    dictionaries on a route are copied, and *data* itself is never touched.
    """
    result = dict(data)
    for path in paths:
        node = result
        *route, leaf = path.split(".")
        for step in route:
            child = node.get(step)
            if not isinstance(child, dict):
                node = None
                break
            child = dict(child)
            node[step] = child
            node = child
        if node is not None:
            node.pop(leaf, None)
    return result


def visible_metadata(event, can_read: Callable[[str], bool]):
    """*event*'s metadata without the words and values a reader may not read.

    Every rule that names the event and guards a field the reader may not read
    removes its words' key and its ``metadata_hide`` keys. The stored value is
    never touched: a reader who may not read gets a copy without them.
    """
    metadata = event.metadata
    if not isinstance(metadata, dict):
        return metadata
    record_only = record_only_rule_for(event)
    if record_only is not None:
        return {k: v for k, v in metadata.items() if k in record_only.metadata_allow}
    hidden = set()
    for rule in _RULES.values():
        if not rule.matches(event) or can_read(rule.field_key):
            continue
        if rule.carries_words:
            hidden.add(rule.metadata_key)
        hidden |= rule.metadata_hide
    return _without(metadata, hidden) if hidden else metadata


def visible_snapshot(event, snapshot, can_read: Callable[[str], bool]):
    """*snapshot* (``before_data`` or ``diff_data``) without the values a reader may not read.

    Every snapshot rule that names *event* and guards a field the reader may
    not read removes its keys. The stored value is never touched: a reader
    gets a copy.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    if record_only_rule_for(event) is not None:
        return {}
    hidden = {
        key
        for rule in _RULES.values()
        if rule.snapshot_keys and rule.matches(event) and not can_read(rule.field_key)
        for key in rule.snapshot_keys
    }
    return _without(snapshot, hidden) if hidden else snapshot


def visible_summary_expression(can_read: Callable[[str], bool]):
    """The summary each row shows a reader, as a database expression.

    The SQL counterpart of :func:`visible_summary`, for a search or an export
    that must not match or write the words a reader may not read: every row a
    hidden rule names is cut where its marker begins, every older row a
    :class:`RecordOnly` rule names is its rebuilt sentence, and every other row
    is its summary. A reader who may read every rule's words gets the summary
    column itself, so their search is the plain one.
    """
    hidden = [
        rule for rule in _RULES.values()
        if rule.carries_words and not can_read(rule.field_key)
    ]
    if not hidden and not _RECORD_ONLY:
        return F("summary")
    return Case(
        *(
            When(rule.legacy_q(), then=rule.summary_sql())
            for rule in _RECORD_ONLY.values()
        ),
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
