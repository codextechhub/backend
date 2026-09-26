"""Writing versions: the one place every change to a tracked row passes through.

A version is written in the same transaction as the change it records, so a
rolled-back save leaves no version behind and a committed one never lacks its
version.

A save that changes no tracked field writes nothing. That is decided by
comparing the new copy with the latest stored one, not by trusting
``update_fields``, because a save naming a tracked field may still write the
value that was already there.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.utils import timezone

from .registry import TrackedModel, snapshot, spec_for

_CONNECTED: set[type] = set()


def _actor_id():
    """The person the audit trail would attribute this change to, if any.

    Read from the request's audit identity, so a change made during a proxy
    session is attributed to the real actor, as the audit log attributes it.
    """
    from vs_tenants.context import get_current_audit_identity

    actor, _effective, _session = get_current_audit_identity()
    return getattr(actor, "pk", None)


def _tenant_id(instance):
    return getattr(instance, "tenant_id", None)


def record(instance, *, deleted: bool = False, baseline: bool = False):
    """Write a version of *instance* if its tracked fields changed.

    Returns the new :class:`~vs_history.models.RecordVersion`, or ``None`` when
    nothing changed or the model keeps no history. A deletion always writes a
    version, unless the latest one already says the row is gone.
    """
    from .models import RecordVersion

    spec = spec_for(type(instance))
    if spec is None or instance.pk is None:
        return None
    data = snapshot(spec, instance)
    record_id = str(instance.pk)
    last = (
        RecordVersion.objects.filter(record_type=spec.record_type, record_id=record_id)
        .order_by("-recorded_at", "-id")
        .values("data", "is_deleted")
        .first()
    )
    if last is not None:
        if deleted and last["is_deleted"]:
            return None
        if not deleted and not last["is_deleted"] and last["data"] == data:
            return None
    previous = {} if last is None or last["is_deleted"] else last["data"]
    changed = sorted(name for name in data if name not in previous or previous[name] != data[name])
    return RecordVersion.objects.create(
        tenant_id=_tenant_id(instance),
        record_type=spec.record_type,
        record_id=record_id,
        owners=spec.owner_keys(instance),
        recorded_at=timezone.now(),
        is_baseline=baseline,
        is_deleted=deleted,
        data=data,
        changed=[] if deleted else changed,
        actor_id=None if baseline else _actor_id(),
    )


def record_pks(model, pks) -> None:
    """Record every row of *model* named in *pks*, read afresh from the database."""
    spec = spec_for(model)
    if spec is None or not pks:
        return
    rows = model._base_manager.filter(pk__in=list(pks))
    if spec.m2m:
        rows = rows.prefetch_related(*spec.m2m)
    for row in rows:
        record(row)


def _refuse_rebuilt(sender, instance, **kwargs):
    if getattr(instance, "_history_readonly", False):
        raise ImproperlyConfigured(
            f"A {sender._meta.label} rebuilt from its history is for reading "
            f"only. Saving it would overwrite the live record with the past."
        )


def _on_save(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    spec = spec_for(sender)
    if spec is None:
        return
    if update_fields is not None and not (set(update_fields) & spec.tracked_names):
        return
    record(instance)


def _on_delete(sender, instance, **kwargs):
    if getattr(instance, "_history_readonly", False):
        raise ImproperlyConfigured(
            f"A {sender._meta.label} rebuilt from its history cannot be deleted."
        )
    record(instance, deleted=True)


def _m2m_handler(spec: TrackedModel, name: str):
    """The m2m_changed receiver for *spec*'s field *name*.

    Written from the tracked side, the instance is the row that changed. Written
    from the other side (``branch.staff_postings.add(...)``), the tracked rows
    are the ones in ``pk_set``; a clear from that side names none, so the rows
    it is about to detach are read in ``pre_clear`` and recorded after it.
    """
    pending_attr = f"_history_pending_clear_{name}"

    def handler(sender, instance, action, reverse, model, pk_set, **kwargs):
        if not reverse:
            if action in {"post_add", "post_remove", "post_clear"}:
                record(instance)
            return
        if action == "pre_clear":
            accessor = spec.model._meta.get_field(name).remote_field.get_accessor_name()
            setattr(instance, pending_attr, list(
                getattr(instance, accessor).values_list("pk", flat=True),
            ))
        elif action == "post_clear":
            record_pks(spec.model, getattr(instance, pending_attr, ()))
            setattr(instance, pending_attr, ())
        elif action in {"post_add", "post_remove"}:
            record_pks(spec.model, pk_set or ())

    handler.__name__ = f"history_m2m_{spec.record_type}_{name}"
    return handler


def connect(spec: TrackedModel) -> None:
    """Connect the signals that keep *spec*'s history. Safe to call twice."""
    if spec.model in _CONNECTED:
        return
    uid = f"vs_history:{spec.record_type}"
    pre_save.connect(_refuse_rebuilt, sender=spec.model, dispatch_uid=f"{uid}:pre_save")
    post_save.connect(_on_save, sender=spec.model, dispatch_uid=f"{uid}:save")
    post_delete.connect(_on_delete, sender=spec.model, dispatch_uid=f"{uid}:delete")
    for name in spec.m2m:
        through = getattr(spec.model, name).through
        m2m_changed.connect(
            _m2m_handler(spec, name), sender=through, weak=False,
            dispatch_uid=f"{uid}:m2m:{name}",
        )
    _CONNECTED.add(spec.model)
