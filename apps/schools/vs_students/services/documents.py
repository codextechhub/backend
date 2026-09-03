"""The documents a school holds against a child.

The bytes go through ``core.StoredFile``, which is the platform's
database-backed storage and is already served with authentication - so a
document is never a public URL and a leaked link is not a leaked birth
certificate.

A missing required document never blocks anything. A school registering a child
on the day they arrive rarely has the birth certificate in hand, and a rule that
refused the enrolment would simply be worked around with a blank file.

FRD M11 v2.4 section 7.6 and FR-015.
"""
from __future__ import annotations

from django.db import transaction

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import REQUIRED_DOCUMENTS, DocumentType
from ..models import StudentDocument


def checklist(student, *, request=None):
    """Every type, attached or not. The screen shows all five either way.

    ``request`` makes the file URLs absolute - see ``_media_url``.
    """
    held = {d.document_type: d for d in student.documents.all()}
    rows = []
    for value, label in DocumentType.choices:
        doc = held.get(value)
        rows.append({
            "document_type": value,
            "label": label,
            "required": value in REQUIRED_DOCUMENTS,
            "attached": doc is not None,
            "uploaded_at": doc.uploaded_at if doc else None,
            "id": doc.pk if doc else None,
            "url": _media_url(doc, request) if doc else "",
        })
    return rows


#: The prefetch that makes ``face_url`` free on a list.
#:
#: Without it the directory asks one extra question per row - fifty students,
#: fifty queries for fifty photographs - which is the cost ``_list_queryset``
#: exists to avoid. ``to_attr`` keeps it clear of ``documents.all()``, so the
#: profile's own checklist still reads every type.
def photo_prefetch():
    from django.db.models import Prefetch

    return Prefetch(
        "documents",
        queryset=StudentDocument.objects.filter(
            document_type=DocumentType.PASSPORT_PHOTO,
        ).order_by("-uploaded_at"),
        to_attr="_passport_photo",
    )


def face_url(student, *, request=None):
    """The student's face, for an avatar.

    **This reads the passport photograph, not ``Student.photo``.** That column
    exists, is serialised as ``photo_url``, and is written by nothing at all -
    no route, no serializer, no service anywhere in the codebase sets it. So
    every school that had done what the module asked and uploaded the required
    passport photograph still saw initials on every screen, and there appeared
    to be nowhere to upload a photograph even though they already had.

    The photograph was never missing. It was in ``StudentDocument`` under
    ``PASSPORT_PHOTO`` - a required document since FR-015 - being read by
    nothing but the checklist. One source, read here.

    ``Student.photo`` still wins if a row ever carries one, so nothing that
    might populate it later is silently ignored.
    """
    from core.media import signed_url

    if student.photo:
        return signed_url(student.photo.name, absolute_for=request)

    held = getattr(student, "_passport_photo", None)
    doc = (
        (held[0] if held else None)
        if held is not None
        else student.documents.filter(
            document_type=DocumentType.PASSPORT_PHOTO,
        ).order_by("-uploaded_at").first()
    )
    return _media_url(doc, request) if doc else ""


def missing_required(student):
    held = set(student.documents.values_list("document_type", flat=True))
    return sorted(REQUIRED_DOCUMENTS - held)


def _media_url(doc, request=None):
    """A signed, user-bound, expiring URL - never a bare /media/ path.

    An unsigned path inside its window is a bearer token, which is exactly the
    behaviour core.media exists to have removed.

    **``absolute_for`` is not optional in practice.** Without it ``signed_url``
    returns a bare ``/media/...`` PATH, and the browser resolves a path against
    the page's own origin - which is the frontend, not the API. The two are
    never the same host: the app runs at ``lagoon-view.xvs.codexng.com`` and the
    API at ``api.codexng.com``. So every "View" link on this checklist opened
    the single-page app's own index.html instead of the document, and the
    passport photograph resolved to the same HTML and failed to decode, which
    is why a student who HAD a photograph still showed initials.

    Every other module that hands a media URL to a browser already passes it -
    the school logo, finance receipts, the organogram's photographs. This one
    did not.
    """
    from core.media import signed_url

    return signed_url(
        getattr(doc.file, "name", "") or "", absolute_for=request,
    )


@transaction.atomic
def attach(student, *, document_type, upload, actor):
    """Attach or replace one document.

    Replacing deletes the old StudentDocument row first, which is what
    ``core.binding``'s post_delete hook needs in order to retire the superseded
    file: without the delete the previous birth certificate would keep its own
    live URL for ever, and the school would believe it had replaced it.
    """
    StudentDocument.objects.filter(
        student=student, document_type=document_type,
    ).delete()
    doc = StudentDocument.objects.create(
        tenant=student.tenant, student=student, document_type=document_type,
        file=upload,
        original_name=getattr(upload, "name", "") or "",
        content_type=getattr(upload, "content_type", "") or "",
        size=getattr(upload, "size", 0) or 0,
        uploaded_by=actor,
    )
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_DOCUMENT_ATTACHED,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=(
            f"{DocumentType(document_type).label} attached to "
            f"{student.full_name}."
        ),
        metadata={"document_type": document_type},
    )
    return doc


@transaction.atomic
def remove(student, doc, *, actor):
    label = DocumentType(doc.document_type).label
    doc.delete()
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_DOCUMENT_REMOVED,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=f"{label} removed from {student.full_name}.",
        metadata={"document_type": doc.document_type},
    )
