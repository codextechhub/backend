"""Which models keep a history, and how a row becomes a version and back.

A domain app declares its tracked models from its ``AppConfig.ready()`` with
:func:`track`, the same one-way seam Field Access and the Export Centre use:
this engine names no domain and imports no app.

What a declaration commits the app to
-------------------------------------

Every write to a tracked row has to reach :mod:`vs_history.recorder`, or the
history silently stops matching the record. ``track`` connects the save,
delete and many-to-many signals itself. The writes that fire no signal
(``QuerySet.update``, ``bulk_create`` and ``bulk_update``) are caught by
:class:`vs_history.queryset.VersionedQuerySetMixin`, so every manager of a
tracked model has to produce a queryset carrying it. ``track`` refuses a model
whose managers do not, at start-up, so the gap cannot be opened by adding a
manager later.

Raw SQL and migrations write no history. A data migration that changes a
tracked field has to call :func:`vs_history.recorder.record` for the rows it
touched.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.core.exceptions import ImproperlyConfigured
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

#: An owner of a version: the record type and id of a page that lists the row.
Owner = tuple[str, object]


@dataclass(frozen=True)
class TrackedModel:
    """One model's history declaration.

    ``fields`` are the concrete fields copied into every version, by name.
    A foreign key is stored under its attribute name (``branch_id``).
    ``m2m`` are many-to-many fields stored as sorted lists of primary keys.
    ``owners`` returns the pages a row belongs on; a record with a page of its
    own needs none, because it is found by its own id.
    """

    model: type[models.Model]
    record_type: str
    fields: tuple[str, ...]
    m2m: tuple[str, ...] = ()
    owners: Callable[[models.Model], Iterable[Owner]] | None = None
    concrete_fields: tuple[models.Field, ...] = field(default=(), compare=False)

    @property
    def tracked_names(self) -> frozenset[str]:
        """Every name a write may use for a tracked field, attname included."""
        names = set(self.fields) | set(self.m2m)
        names |= {f.attname for f in self.concrete_fields}
        return frozenset(names)

    def owner_keys(self, instance) -> list[str]:
        if self.owners is None:
            return []
        return sorted({
            owner_key(record_type, pk)
            for record_type, pk in self.owners(instance)
            if pk is not None
        })


_REGISTRY: dict[type[models.Model], TrackedModel] = {}


def owner_key(record_type: str, pk) -> str:
    """The string a version stores to say it belongs on *record_type*'s page."""
    return f"{record_type}:{pk}"


def record_type_of(model: type[models.Model]) -> str:
    return model._meta.label_lower


#: Columns no version stores: the primary key is the record id, the tenant is
#: stored beside the version, and ``updated_at`` changes on every save, so a
#: version holding it would record a change nobody made.
ALWAYS_EXCLUDED = frozenset({"id", "tenant", "updated_at"})


def track(
    model: type[models.Model],
    *,
    fields: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
    m2m: Iterable[str] = (),
    owners: Callable[[models.Model], Iterable[Owner]] | None = None,
) -> TrackedModel:
    """Keep a history of every column of *model* except *exclude*, from now on.

    Every concrete column is tracked unless it is excluded, so a column added
    to the model later is in its history from the day it exists rather than
    missing until somebody remembers to declare it. *m2m* names the
    many-to-many fields to keep as lists of primary keys.

    A model that holds credentials names its columns in *fields* instead, and
    nothing else is stored: a sign-in secret added to it later must never be
    copied into a history that everyone who may read the record can read.

    Idempotent: declaring a model again replaces the earlier declaration and
    does not connect its signals twice.
    """
    from . import recorder
    from .queryset import assert_managers_versioned

    skipped = ALWAYS_EXCLUDED | set(exclude)
    unknown = set(exclude) - {f.name for f in model._meta.get_fields()}
    if unknown:
        raise ImproperlyConfigured(
            f"History: {model._meta.label} has no field "
            f"{', '.join(sorted(unknown))} to exclude."
        )
    if fields is not None:
        fields = list(fields)
    else:
        fields = [
            f.name for f in model._meta.concrete_fields
            if f.name not in skipped and not f.primary_key
        ]
    concrete: list[models.Field] = []
    for name in fields:
        try:
            model_field = model._meta.get_field(name)
        except Exception as exc:  # FieldDoesNotExist
            raise ImproperlyConfigured(
                f"History: {model._meta.label} has no field '{name}'."
            ) from exc
        if model_field.many_to_many or not model_field.concrete:
            raise ImproperlyConfigured(
                f"History: '{name}' on {model._meta.label} is not a column. "
                f"Declare a many-to-many field under m2m."
            )
        concrete.append(model_field)
    for name in m2m:
        if not model._meta.get_field(name).many_to_many:
            raise ImproperlyConfigured(
                f"History: '{name}' on {model._meta.label} is not many-to-many."
            )

    assert_managers_versioned(model)

    spec = TrackedModel(
        model=model,
        record_type=record_type_of(model),
        fields=tuple(fields),
        m2m=tuple(m2m),
        owners=owners,
        concrete_fields=tuple(concrete),
    )
    _REGISTRY[model] = spec
    recorder.connect(spec)
    return spec


def spec_for(model) -> TrackedModel | None:
    """The declaration covering *model*, or ``None`` when it keeps no history."""
    if model is None:
        return None
    return _REGISTRY.get(model) or _REGISTRY.get(model._meta.concrete_model)


def spec_for_type(record_type: str) -> TrackedModel:
    for spec in _REGISTRY.values():
        if spec.record_type == record_type:
            return spec
    raise LookupError(f"No model keeps a history as '{record_type}'.")


def all_specs() -> list[TrackedModel]:
    return sorted(_REGISTRY.values(), key=lambda spec: spec.record_type)


def _to_json(value):
    """*value* in the form a JSON column stores and compares it."""
    if isinstance(value, models.fields.files.FieldFile):
        return value.name or ""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def snapshot(spec: TrackedModel, instance) -> dict:
    """The tracked fields of *instance*, as a version stores them.

    Many-to-many values are read through the prefetch cache when one is
    present, so a baseline over a prefetched queryset costs no query per row.
    """
    data = {
        model_field.attname: _to_json(model_field.value_from_object(instance))
        for model_field in spec.concrete_fields
    }
    for name in spec.m2m:
        manager = getattr(instance, name)
        cached = getattr(instance, "_prefetched_objects_cache", {}).get(name)
        rows = cached if cached is not None else manager.all()
        data[name] = sorted(_to_json(row.pk) for row in rows)
    return data


def rebuild(spec: TrackedModel, record_id, data: dict):
    """An unsaved-looking instance of the tracked model, as a version holds it.

    The instance carries the primary key and every tracked field and behaves as
    a row loaded from the database, so a serializer renders it exactly as it
    renders a live one. It is for reading only: saving it would overwrite the
    live row with the past, so ``save`` and ``delete`` refuse.

    Many-to-many values are not set here; the page composing the view installs
    the related rows it rebuilt with :func:`install_related`.
    """
    instance = spec.model()
    pk_field = spec.model._meta.pk
    setattr(instance, pk_field.attname, pk_field.to_python(record_id))
    for model_field in spec.concrete_fields:
        if model_field.attname not in data:
            continue
        value = data[model_field.attname]
        if not isinstance(model_field, models.FileField):
            value = model_field.to_python(value)
        setattr(instance, model_field.attname, value)
    instance._state.adding = False
    instance._state.db = "default"
    instance._history_readonly = True
    return instance


def install_related(instance, name: str, rows: list) -> None:
    """Make ``instance.<name>.all()`` answer *rows* without a query.

    Uses the prefetch cache, which every related manager consults first, so
    ``.all()``, ``.count()`` and iteration all read the rebuilt rows.
    """
    cache = getattr(instance, "_prefetched_objects_cache", None)
    if cache is None:
        cache = {}
        instance._prefetched_objects_cache = cache
    queryset = getattr(instance, name).get_queryset().none()
    queryset._result_cache = list(rows)
    queryset._prefetch_done = True
    cache[name] = queryset
