"""The queryset writes that fire no signal, made to keep the history too.

``QuerySet.update``, ``bulk_create`` and ``bulk_update`` write rows without
calling ``save``, so no ``post_save`` reaches :mod:`vs_history.recorder`. On a
tracked model the mixin records each affected row after the write, inside the
same transaction. On any other model it does nothing beyond one dictionary
lookup, which is why it can sit on a queryset class many models share.

An ``update`` that names no tracked field (a sign-in time, a counter) records
nothing and reads nothing, so the hot paths that update such columns in bulk
pay no extra query.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction

from .registry import spec_for


class VersionedQuerySetMixin:
    """Records the rows a signal-free write changed, when the model is tracked."""

    def update(self, **kwargs):
        spec = spec_for(self.model)
        if spec is None or not (set(kwargs) & spec.tracked_names):
            return super().update(**kwargs)
        from .recorder import record_pks

        with transaction.atomic(using=self.db):
            pks = list(self.values_list("pk", flat=True))
            count = super().update(**kwargs)
            record_pks(self.model, pks)
        return count

    update.alters_data = True

    def bulk_update(self, objs, fields, *args, **kwargs):
        spec = spec_for(self.model)
        objs = list(objs)
        if spec is None or not (set(fields) & spec.tracked_names):
            return super().bulk_update(objs, fields, *args, **kwargs)
        from .recorder import record_pks

        with transaction.atomic(using=self.db):
            count = super().bulk_update(objs, fields, *args, **kwargs)
            record_pks(self.model, [obj.pk for obj in objs])
        return count

    bulk_update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        spec = spec_for(self.model)
        if spec is None:
            return super().bulk_create(objs, *args, **kwargs)
        from .recorder import record

        with transaction.atomic(using=self.db):
            created = super().bulk_create(objs, *args, **kwargs)
            for obj in created:
                record(obj)
        return created

    bulk_create.alters_data = True


class VersionedQuerySet(VersionedQuerySetMixin, models.QuerySet):
    """A plain queryset that keeps the history of a tracked model."""


#: The unscoped manager a tracked model declares where it would otherwise
#: declare ``models.Manager()``.
VersionedManager = models.Manager.from_queryset(VersionedQuerySet)


def assert_managers_versioned(model) -> None:
    """Raise unless every manager of *model* yields a versioned queryset.

    A tracked model with one plain manager has a door through which
    ``update()`` changes a row and leaves its history behind, so the
    declaration refuses it at start-up rather than letting the history drift.
    """
    for manager in model._meta.managers:
        if not isinstance(manager.get_queryset(), VersionedQuerySetMixin):
            raise ImproperlyConfigured(
                f"History: {model._meta.label}.{manager.name} returns a plain "
                f"queryset, so its update() would change rows without recording "
                f"them. Give it a queryset carrying VersionedQuerySetMixin."
            )
