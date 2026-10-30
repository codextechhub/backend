"""Erasing a person who left long ago: the parts every kind of person shares.

Six years after a pupil, a parent or a member of staff leaves, the privacy
policy promises their personal details go. Each product erases its own people
(``schools.vs_students.erasure``, ``schools.vs_staff.erasure``,
``vs_user.erasure``), run from :mod:`core.pruning`'s schedule; this module holds
what they have in common.

**Erasing is blanking, not deleting.** The person's row stays, so the records
the school must keep (results, attendance, the books) still point at someone.
What goes is everything about them that those records do not need. The owner
decided (30 Oct 2026) that pupils and staff keep their names, because results
and payslips carry them; a parent's name is replaced.

**The edit history is cleaned the same way.** ``vs_history`` keeps a copy of a
row at every change, so blanking the live row alone would leave every old
address in the copies. :func:`scrub_history` blanks the same fields in each
copy and keeps the copies themselves, so a class list rebuilt for an earlier
date still has every pupil in it. A row deleted outright (a birth certificate,
a qualification) loses its copies with it (:func:`forget_history`).

The audit trail is not touched: the database refuses every change to it.
"""
from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

#: What an erased parent is called.
FORMER_PARENT = ("Former", "parent")


def scrub_history(model, pks, blanks: dict) -> int:
    """Blank ``blanks`` in every stored copy of these rows; answer the copies changed.

    A field absent from a copy (it was not tracked yet) is left absent. Values
    must be what the history stores: ISO text for a date, a name for a file.
    """
    from vs_history.models import RecordVersion
    from vs_history.registry import spec_for

    spec = spec_for(model)
    if spec is None or not pks:
        return 0
    changed = 0
    versions = RecordVersion.objects.filter(
        record_type=spec.record_type, record_id__in=[str(pk) for pk in pks],
    )
    for version in versions.iterator():
        data = dict(version.data)
        data.update({k: v for k, v in blanks.items() if k in data})
        if data != version.data:
            RecordVersion.objects.filter(pk=version.pk).update(data=data)
            changed += 1
    return changed


def forget_history(model, pks) -> int:
    """Delete every stored copy of rows that were deleted for good."""
    from vs_history.models import RecordVersion
    from vs_history.registry import spec_for

    spec = spec_for(model)
    if spec is None or not pks:
        return 0
    deleted, _ = RecordVersion.objects.filter(
        record_type=spec.record_type, record_id__in=[str(pk) for pk in pks],
    ).delete()
    return deleted


def delete_rows(queryset) -> int:
    """Delete rows through the ORM (files revoked, holds asked), then their copies."""
    model = queryset.model
    pks = list(queryset.values_list("pk", flat=True))
    if not pks:
        return 0
    model._base_manager.filter(pk__in=pks).delete()
    forget_history(model, pks)
    return len(pks)


def erase_each(rows, erase) -> dict:
    """Erase each row in its own transaction; one that fails is logged and skipped.

    A person whose erasure fails stays due, so the next night tries again,
    and never holds back the people after them.
    """
    model = rows.model
    done = failed = 0
    for pk in list(rows.values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                erase(model._base_manager.select_for_update().get(pk=pk))
            done += 1
        except Exception:
            logger.exception("Erasing %s %s failed", model._meta.label, pk)
            failed += 1
    return {"erased": done, "failed": failed}
