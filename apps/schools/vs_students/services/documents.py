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


def checklist(student):
    """Every type, attached or not. The screen shows all five either way."""
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
            "url": _media_url(doc) if doc else "",
        })
    return rows


def missing_required(student):
    held = set(student.documents.values_list("document_type", flat=True))
    return sorted(REQUIRED_DOCUMENTS - held)


def _media_url(doc):
    """A signed, user-bound, expiring URL - never a bare /media/ path.

    An unsigned path inside its window is a bearer token, which is exactly the
    behaviour core.media exists to have removed.
    """
    from core.media import signed_url

    return signed_url(getattr(doc.file, "name", "") or "")


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
