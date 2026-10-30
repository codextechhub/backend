"""Erasing pupils and parents 6 years after they leave, for :mod:`core.pruning`.

Tunde left Bright Star in July 2026. In July 2032 his record keeps his name,
admission number, status, branch and the year he was born, and his results,
attendance and enrolments stay as they are (the owner's decision: results
carry his name). Everything else about him goes: address, phone, email, photo,
previous school, health notes and emergency contact. His birth certificate and
other documents are deleted with their files, the written reason and new
school on his status changes are blanked, any conversation about him is
deleted, the billing contact on his fees account is cleared (the name on his
invoices stays, because the books must show who was billed), and his pupil
sign-in is closed.

His mother is erased once every child of hers has been: her name becomes
"Former parent" and her contact details, occupation and photo go. A parent
linked to no pupil at all goes 6 years after her record last changed.

A pupil has left on the date of the status change that made him WITHDRAWN,
GRADUATED, TRANSFERRED or REJECTED; a pupil with no such change on record
(an import) is dated by when his record last changed.
"""
from __future__ import annotations

import datetime

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from core.erasure import FORMER_PARENT, delete_rows, erase_each, scrub_history

from .constants import StudentStatus

LEFT = (
    StudentStatus.WITHDRAWN, StudentStatus.GRADUATED,
    StudentStatus.TRANSFERRED, StudentStatus.REJECTED,
)

PUPIL_BLANKS = {
    "gender": "", "nationality": "", "state_of_origin": "", "address": "",
    "phone": "", "email": "", "previous_school": "", "photo": "",
    "blood_group": "", "allergies": "", "conditions": "",
    "emergency_contact_name": "", "emergency_contact_phone": "",
}

PARENT_BLANKS = {
    "full_name": " ".join(FORMER_PARENT), "first_name": FORMER_PARENT[0],
    "middle_name": "", "last_name": FORMER_PARENT[1], "name_needs_review": False,
    "phone": "", "alt_phone": "", "email": "", "occupation": "", "address": "",
    "photo": "",
}


def due_pupils(cutoff):
    from .models import Student, StudentStatusLog

    left = StudentStatusLog._base_manager.filter(
        student=OuterRef("pk"), to_status=OuterRef("status"),
    ).order_by("-effective_date", "-changed_at")
    return Student._base_manager.annotate(
        left_on=Coalesce(
            Subquery(left.values("effective_date")[:1]), TruncDate("updated_at"),
        ),
    ).filter(status__in=LEFT, erased_at__isnull=True, left_on__lt=cutoff.date())


def due_parents(cutoff):
    from .models import Guardian, StudentGuardian

    links = StudentGuardian._base_manager.filter(guardian=OuterRef("pk"))
    return Guardian._base_manager.filter(erased_at__isnull=True).exclude(
        Exists(links.filter(student__erased_at__isnull=True)),
    ).filter(Q(Exists(links)) | Q(updated_at__lt=cutoff))


def erase_leavers(cutoff, *, dry_run: bool = False) -> dict:
    if dry_run:
        # The parents due once tonight's pupils are erased cannot be counted
        # without erasing them; this counts the ones due already.
        return {"pupils": due_pupils(cutoff).count(), "parents": due_parents(cutoff).count()}
    pupils = erase_each(due_pupils(cutoff), erase_pupil)
    parents = erase_each(due_parents(cutoff), erase_parent)
    return {"pupils": pupils, "parents": parents}


def erase_pupil(pupil) -> None:
    from schools.core.fal.contracts import SOURCE_TYPE_STUDENT
    from schools.vs_messages.models import Conversation
    from vs_finance.models import Customer
    from vs_user.erasure import erase_account

    from .models import Student, StudentDocument, StudentStatusLog

    delete_rows(StudentDocument._base_manager.filter(student=pupil))
    delete_rows(Conversation._base_manager.filter(student=pupil))
    StudentStatusLog._base_manager.filter(student=pupil).update(reason="", destination_school="")

    born = datetime.date(pupil.date_of_birth.year, 1, 1)
    scrub_history(Student, [pupil.pk], {**PUPIL_BLANKS, "date_of_birth": born.isoformat()})
    for field, value in PUPIL_BLANKS.items():
        setattr(pupil, field, value)
    pupil.date_of_birth = born
    pupil.erased_at = timezone.now()
    pupil.save()

    Customer._base_manager.filter(
        source_type=SOURCE_TYPE_STUDENT, source_id=str(pupil.pk),
    ).update(billing_email="", billing_phone="", billing_address="")
    erase_account(pupil.user)


def erase_parent(parent) -> None:
    from schools.vs_messages.models import Conversation
    from vs_user.erasure import erase_account

    from .models import Guardian

    delete_rows(Conversation._base_manager.filter(guardian=parent))
    scrub_history(Guardian, [parent.pk], PARENT_BLANKS)
    for field, value in PARENT_BLANKS.items():
        setattr(parent, field, value)
    parent.erased_at = timezone.now()
    parent.save()
    erase_account(parent.user, names=FORMER_PARENT)
