"""The student and guardian profiles as they stood at the end of an earlier day.

Every read behind a profile accepts ``?as_at=YYYY-MM-DD`` and answers in the
same shape as the live read, so a page swaps the date and changes nothing else.
The rows are rebuilt from :mod:`vs_history` and rendered through the same
serializers as the live ones, which is what keeps Field Access applying to the
past exactly as it applies to the present.

What a past view can and cannot show
------------------------------------

* The record, its guardian links, its classes and its documents are rebuilt
  from their versions. A name, a class or a status reads as it stood.
* The status log and the audit timeline are dated rows already, and are cut at
  the end of the day.
* A class, subject or branch is named as it is named today: a class renamed
  since reads under its new name. The subjects are the ones the class's level
  offers today.
* A document or photograph replaced since is shown as held, without a link:
  its file was retired when it was replaced (``services/documents.py``), and a
  link to the current file under a past date would show the wrong document.

A record whose history starts after the chosen day is refused with the day it
starts (:class:`vs_history.as_at.HistoryNotKept`) rather than shown as it is
today.

Which student a caller may open is decided by the live record and its branch,
as it is for every other read, so a past view never widens what a caller sees.
"""
from __future__ import annotations

from vs_history.as_at import (
    AsAt,
    history_starts,
    instance_at,
    instances_at,
    instances_by_id_at,
    require_history,
)
from vs_history.registry import spec_for

from .constants import DocumentType
from .history import GUARDIAN, STUDENT
from .models import ClassEnrolment, Guardian, Student, StudentDocument, StudentGuardian


def as_at_meta(as_at: AsAt, starts, **extra) -> dict:
    """The block a past response carries, naming the day it answers for."""
    return {"date": as_at.date.isoformat(), "history_starts": starts.isoformat(), **extra}


def student_history_starts(student_pk):
    return history_starts(spec_for(Student), student_pk)


def guardian_history_starts(guardian_pk):
    return history_starts(spec_for(Guardian), guardian_pk)


def enrolments_at(student_pk, as_at: AsAt) -> list:
    rows = instances_at(spec_for(ClassEnrolment), STUDENT, student_pk, as_at)
    rows.sort(key=lambda row: row.assigned_at, reverse=True)
    return rows


def active_enrolment(rows):
    return next((row for row in rows if row.is_active), None)


def _live_file_names(model, rows, attr="file") -> dict:
    """``{pk: file name}`` for the rows that still exist today."""
    ids = [row.pk for row in rows]
    if not ids:
        return {}
    return {
        pk: name or ""
        for pk, name in model.all_objects.filter(pk__in=ids).values_list("pk", attr)
    }


def documents_at(student_pk, as_at: AsAt) -> tuple[list, list]:
    """The documents held at *as_at*, split into ``(still_held, retired)``.

    A document is still held when today's row is the same row with the same
    file; its link is then the live one. Anything else was replaced or removed
    since, and its file no longer exists.
    """
    rows = instances_at(spec_for(StudentDocument), STUDENT, student_pk, as_at)
    live = _live_file_names(StudentDocument, rows)
    held, retired = [], []
    for row in rows:
        (held if live.get(row.pk) == row.file.name else retired).append(row)
    return held, retired


def checklist_at(student_pk, as_at: AsAt, *, request) -> list:
    """The document checklist as it stood, in the live checklist's shape."""
    from .constants import REQUIRED_DOCUMENTS
    from .services.documents import _media_url

    held, retired = documents_at(student_pk, as_at)
    by_type = {row.document_type: (row, True) for row in held}
    by_type.update({row.document_type: (row, False) for row in retired})
    rows = []
    for value, label in DocumentType.choices:
        doc, live = by_type.get(value, (None, False))
        rows.append({
            "document_type": value,
            "label": label,
            "required": value in REQUIRED_DOCUMENTS,
            "attached": doc is not None,
            "uploaded_at": doc.uploaded_at if doc else None,
            "id": doc.pk if doc else None,
            "url": _media_url(doc, request) if doc and live else "",
            "file_retired": bool(doc) and not live,
        })
    return rows


def student_at(student, as_at: AsAt):
    """*student* rebuilt as at *as_at*, ready for the profile's serializer.

    Raises :class:`HistoryNotKept` when the record's history starts later.
    The class, the passport photograph and ``updated_at`` are installed where
    the serializers read them, so no serializer reaches the live tables for a
    fact that has a past value.
    """
    spec = spec_for(Student)
    starts = require_history(spec, student.pk, as_at, noun=f"{student.full_name}'s record")
    past = instance_at(spec, student.pk, as_at)
    past.updated_at = past._history_version.recorded_at
    enrolments = enrolments_at(student.pk, as_at)
    past._active_enrolments = [row for row in enrolments if row.is_active]
    held, retired = documents_at(student.pk, as_at)
    past._passport_photo = [row for row in held if row.document_type == DocumentType.PASSPORT_PHOTO]
    photo_retired = any(row.document_type == DocumentType.PASSPORT_PHOTO for row in retired)
    return past, as_at_meta(as_at, starts, photo_retired=photo_retired)


def guardian_links_at(student_pk, as_at: AsAt) -> list:
    """The student's guardian links at *as_at*, each carrying its guardian then."""
    links = instances_at(spec_for(StudentGuardian), STUDENT, student_pk, as_at)
    guardians = instances_by_id_at(spec_for(Guardian), [link.guardian_id for link in links], as_at)
    live_photos = _live_file_names(Guardian, guardians.values(), attr="photo")
    for guardian in guardians.values():
        if guardian.photo.name and live_photos.get(guardian.pk) != guardian.photo.name:
            guardian.photo = ""
    kept = []
    for link in links:
        guardian = guardians.get(link.guardian_id)
        if guardian is None:
            continue
        link.guardian = guardian
        kept.append(link)
    kept.sort(key=lambda link: (not link.is_primary, link.pk))
    return kept


def siblings_at(student_pk, guardian_ids, visible_ids, as_at: AsAt) -> dict:
    """``{guardian_id: [student]}`` of the other children each guardian had then."""
    spec = spec_for(StudentGuardian)
    pairs = []
    for guardian_id in guardian_ids:
        for link in instances_at(spec, GUARDIAN, guardian_id, as_at):
            if link.student_id != student_pk and link.student_id in visible_ids:
                pairs.append((guardian_id, link.student_id))
    students = instances_by_id_at(spec_for(Student), {pk for _, pk in pairs}, as_at)
    out: dict[int, list] = {}
    for guardian_id, pk in pairs:
        if pk in students:
            out.setdefault(guardian_id, []).append(students[pk])
    return out


def class_names_at(student_ids, as_at: AsAt) -> dict:
    """``{student_id: class name}`` for the class each child was in then."""
    names = {}
    for pk in student_ids:
        row = active_enrolment(enrolments_at(pk, as_at))
        if row is not None:
            names[pk] = row.school_class.name
    return names


def guardian_at(guardian, as_at: AsAt):
    """*guardian* rebuilt as at *as_at*, with whether their photograph was replaced."""
    spec = spec_for(Guardian)
    starts = require_history(spec, guardian.pk, as_at, noun=f"{guardian.full_name}'s record")
    past = instance_at(spec, guardian.pk, as_at)
    photo_retired = bool(past.photo.name) and past.photo.name != (guardian.photo.name or "")
    if photo_retired:
        past.photo = ""
    return past, as_at_meta(as_at, starts, photo_retired=photo_retired)


def wards_at(guardian_pk, visible_ids, as_at: AsAt) -> list[dict]:
    """The guardian's wards at *as_at*, in the live detail response's shape."""
    links = {
        link.student_id: link
        for link in instances_at(spec_for(StudentGuardian), GUARDIAN, guardian_pk, as_at)
        if link.student_id in visible_ids
    }
    students = instances_by_id_at(spec_for(Student), links, as_at)
    classes = class_names_at(students, as_at)
    rows = []
    for pk, student in sorted(students.items(), key=lambda item: item[1].full_name):
        link = links[pk]
        rows.append({
            "id": pk, "name": student.full_name,
            "student_number": student.student_number,
            "status": student.status, "status_label": student.get_status_display(),
            "class_name": classes.get(pk, ""),
            "relationship": link.relationship,
            "is_primary": link.is_primary,
        })
    return rows
