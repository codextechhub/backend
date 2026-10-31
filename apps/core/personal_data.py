"""A copy of everything a school holds about one person, for a data request.

The privacy policy promises a pupil, a parent or a member of staff a copy of
their data "in a usable form to take elsewhere" (Nigeria Data Protection Act
2023, sections 34 and 38). The school is the controller and answers the
request; XVS gives it the file. One JSON document per person, grouped by area:

    {"format": "xvs.personal-data.v1", "generated_at": ..., "school": ...,
     "person": {"kind": "pupil", "id": 41, "name": "Tunde Bello"},
     "sections": {"record": {"title": "Pupil record", "data": {...}}, ...}}

**Each app gives its own sections** (:func:`register`), the way it gives its
own erasure and pruning: only the gradebook knows what a score row means.
A section says which models it ``covers``; a model holding a person's rows
that no section covers must be named, with a reason, by
:func:`not_exported`. ``core/tests_personal_data.py`` walks every model with a
link to a pupil, a guardian or a member of staff and fails on one that is
neither, so a new table about people cannot be left out of the copy by
forgetting it.

**Everything is in it**, whatever the reader's Field Access: the person asking
is entitled to all of it, and the export key (``school.personal_data.export``)
is the gate. Files are listed by name and date, not embedded; the school sends
those it is asked for. The audit trail is not exported: it records what staff
did, and a person's own sign-ins are in their account section.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable

from django.db import models
from django.utils.text import capfirst

PUPIL, GUARDIAN, STAFF = "pupil", "guardian", "staff"
KINDS = (PUPIL, GUARDIAN, STAFF)

FORMAT = "xvs.personal-data.v1"


@dataclass(frozen=True)
class Section:
    kind: str
    key: str
    title: str
    build: Callable
    covers: tuple[str, ...] = ()


_SECTIONS: dict[str, list[Section]] = {kind: [] for kind in KINDS}
#: (kind, model label) -> why that model's rows are not in the copy.
_NOT_EXPORTED: dict[tuple[str, str], str] = {}


def register(kind: str, key: str, title: str, build, *, covers=()) -> None:
    """Add a section to every *kind* export; ``build(person)`` answers its data.

    Idempotent per (kind, key), so a ``ready()`` that runs twice adds nothing.
    """
    sections = _SECTIONS[kind]
    sections[:] = [s for s in sections if s.key != key]
    sections.append(Section(kind, key, title, build, tuple(covers)))


def not_exported(kind: str, label: str, reason: str) -> None:
    """Say why rows of model *label* about a *kind* person are left out of the copy."""
    _NOT_EXPORTED[(kind, label)] = reason


def sections(kind: str) -> list[Section]:
    return list(_SECTIONS[kind])


def covered(kind: str) -> set[str]:
    """Every model label a *kind* export covers or deliberately leaves out."""
    labels = {label for section in _SECTIONS[kind] for label in section.covers}
    return labels | {label for k, label in _NOT_EXPORTED if k == kind}


def export(kind: str, person, *, school_name: str = "") -> dict:
    """The whole copy for *person*, a pupil, guardian or staff record."""
    from django.utils import timezone

    return {
        "format": FORMAT,
        "generated_at": timezone.now().isoformat(),
        "school": school_name,
        "person": {"kind": kind, "id": person.pk, "name": str(person)},
        "sections": {
            section.key: {"title": section.title, "data": section.build(person)}
            for section in sorted(_SECTIONS[kind], key=lambda s: s.key != "record")
        },
    }


# --------------------------------------------------------------------------- #
# Turning rows into plain values                                               #
# --------------------------------------------------------------------------- #

#: Columns no section shows: internal keys and bookkeeping a person cannot use.
SKIPPED = frozenset({"tenant", "updated_at", "erased_at"})


def value(field, obj):
    """One field of *obj* as a plain JSON value a person can read."""
    from vs_finance.money import MoneyField, format_naira

    raw = getattr(obj, field.name)
    if raw is None:
        return None
    if isinstance(field, MoneyField):
        return format_naira(int(raw))  # Stored in kobo; a person reads naira.
    if isinstance(field, models.FileField):
        return raw.name.rsplit("/", 1)[-1] if raw else ""
    if field.is_relation:
        return str(raw)
    if isinstance(raw, (datetime.date, datetime.datetime)):
        return raw.isoformat()
    if hasattr(field, "choices") and field.choices:
        return getattr(obj, f"get_{field.name}_display")()
    if isinstance(raw, (str, int, float, bool, list, dict)):
        return raw
    return str(raw)


def row(obj, *, skip=()) -> dict:
    """Every concrete field of *obj*, by its label, except bookkeeping and *skip*."""
    out = {}
    for field in obj._meta.concrete_fields:
        if field.primary_key or field.name in SKIPPED or field.name in skip:
            continue
        out[capfirst(str(field.verbose_name))] = value(field, obj)
    return out


def rows(queryset, *, skip=()) -> list[dict]:
    return [row(obj, skip=skip) for obj in queryset]


# --------------------------------------------------------------------------- #
# The two acts a school performs on a request                                  #
# --------------------------------------------------------------------------- #

def download(kind: str, person, *, request, tenant, entity_type: str, module_key: str):
    """The copy as a JSON file download, recorded in the audit trail."""
    import json

    from django.http import HttpResponse

    from vs_audit.models import AuditActionType
    from vs_audit.services import emit_audit_event

    data = export(kind, person, school_name=getattr(tenant, "name", ""))
    response = HttpResponse(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="personal-data-{kind}-{person.pk}.json"'
    )
    emit_audit_event(
        module_key=module_key, action_type=AuditActionType.PERSONAL_DATA_EXPORTED,
        entity_type=entity_type, entity_id=str(person.pk), entity_label=str(person),
        tenant=tenant, actor_user=request.user,
        summary=f"A copy of {person}'s personal data was downloaded.",
    )
    return response


def erasure_reason(request) -> str:
    """The reason the request carries, refused when missing.

    The reason is what the school answers to: whose request, received when. It
    is kept in the audit entry, which outlives the details erased.
    """
    from rest_framework.exceptions import ValidationError

    reason = str(request.data.get("reason") or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say whose request this is and when it came in."})
    if len(reason) > 500:
        raise ValidationError({"reason": "Keep the reason to 500 characters."})
    return reason


def record_erasure(person, *, request, tenant, entity_type: str, module_key: str,
                   label: str, reason: str) -> None:
    """The audit entry for an erasure on request; *label* is the name before erasing."""
    from vs_audit.models import AuditActionType
    from vs_audit.services import emit_audit_event

    emit_audit_event(
        module_key=module_key, action_type=AuditActionType.PERSONAL_DATA_ERASED,
        entity_type=entity_type, entity_id=str(person.pk), entity_label=label,
        tenant=tenant, actor_user=request.user, severity="WARNING",
        summary=f"{label}'s personal details were erased on request.",
        metadata={"reason": reason},
    )
