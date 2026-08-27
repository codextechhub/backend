"""
Tying stored bytes back to the record they are evidence for.

Django's storage API is handed a name and a file object, never the model
instance, and a brand-new record has no primary key at all while its file is
being written. Neither end of the write knows enough to bind the row, so the
binding happens one step later: after the owning record is saved, when its
primary key exists and its file field's final storage name is known.

Doing it here rather than in each producer is what makes the guarantee hold.
The alternative - asking every upload path to remember to bind - fails the
first time somebody adds a ``FileField`` and forgets, and the failure is
silent: the file simply becomes unreadable, or worse, readable to anyone.
Walking the model registry means a new ``FileField`` is bound the day it is
added, with no ceremony.

The same hook retires superseded files. When Corona replaces its logo, the
previous upload keeps its own row and its own name, and nothing else in the
system will ever mention it again - so if the URL stayed live it would be a
permanent, unrevocable copy of a file the school believes it has replaced.
"""
from __future__ import annotations

from django.apps import apps as django_apps
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.utils import timezone


def _file_fields(model) -> list[str]:
    return [
        f.name for f in model._meta.get_fields()
        if isinstance(f, models.FileField)
    ]


def bind_instance(instance, field_names, *, update_fields=None, created=False) -> None:
    """Point each named file field's stored row at ``instance``, retire the rest.

    ``created`` skips the retire sweep. A row that has just been inserted cannot
    have an earlier file to supersede, and expense claims are entered a line at a
    time, so the sweep would otherwise cost one query per line to find nothing.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import StoredFile

    targets = (
        field_names if update_fields is None
        else [f for f in field_names if f in set(update_fields)]
    )
    if not targets or instance.pk is None:
        return

    ct = ContentType.objects.get_for_model(type(instance))
    owner_id = str(instance.pk)

    for field_name in targets:
        file = getattr(instance, field_name, None)
        current = getattr(file, "name", "") or ""
        if current:
            StoredFile.objects.filter(name=current).update(
                owner_content_type=ct, owner_object_id=owner_id,
                owner_field=field_name,
            )
        if created:
            continue
        # Anything else this field ever pointed at is no longer current.
        superseded = StoredFile.objects.filter(
            owner_content_type=ct, owner_object_id=owner_id,
            owner_field=field_name, revoked_at__isnull=True,
        )
        if current:
            superseded = superseded.exclude(name=current)
        superseded.update(revoked_at=timezone.now(), content=b"", size=0)


def _make_post_save(field_names):
    def _receiver(sender, instance, created=False, update_fields=None, **kwargs):
        bind_instance(
            instance, field_names, update_fields=update_fields, created=created,
        )
    return _receiver


def _make_post_delete(field_names):
    def _receiver(sender, instance, **kwargs):
        from .media import revoke

        revoke([
            getattr(getattr(instance, name, None), "name", "") or ""
            for name in field_names
        ])
    return _receiver


def connect_all() -> None:
    """Wire binding and revocation onto every model that stores files."""
    for model in django_apps.get_models():
        field_names = _file_fields(model)
        if not field_names:
            continue
        key = f"core.binding.{model._meta.label_lower}"
        post_save.connect(
            _make_post_save(field_names), sender=model, weak=False,
            dispatch_uid=f"{key}.save",
        )
        post_delete.connect(
            _make_post_delete(field_names), sender=model, weak=False,
            dispatch_uid=f"{key}.delete",
        )
