"""Erasing school staff 6 years after they leave, for :mod:`core.pruning`.

Mrs Adeyemi resigned from Bright Star in March 2026. In March 2032 her record
keeps her name, staff number, job, dates of service, teaching and class
records and attendance (her name is on payslips, which the books keep). Her
date of birth, photo, phone, email and gender go; her qualifications and her
documents (CV, certificates, national ID) are deleted with their files; the
written reasons on her employment changes and the notes on her leave are
blanked, while the leave's type and dates stay for the attendance registers
that read them; the bank account her pay went to is cleared (the payments
already made keep their own copy); and her staff sign-in is closed unless she
is still a parent at the school.

A member of staff has left on her exit date, or failing that the date of the
change that took her off the roll, or failing that when her record last
changed.
"""
from __future__ import annotations

from django.db.models import OuterRef, Subquery
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from core.erasure import delete_rows, erase_each, scrub_history

from .constants import OFF_ROLL_STATUSES

STAFF_BLANKS = {"date_of_birth": None, "photo": ""}


def due_staff(cutoff):
    from .models import StaffEmploymentEvent, StaffProfile

    off_roll = StaffEmploymentEvent._base_manager.filter(
        staff=OuterRef("pk"), to_status=OuterRef("employment_status"),
    ).order_by("-effective_date", "-pk")
    return StaffProfile._base_manager.annotate(
        left_on=Coalesce(
            "exit_date", Subquery(off_roll.values("effective_date")[:1]),
            TruncDate("updated_at"),
        ),
    ).filter(
        employment_status__in=OFF_ROLL_STATUSES, erased_at__isnull=True,
        left_on__lt=cutoff.date(),
    )


def erase_leavers(cutoff, *, dry_run: bool = False) -> dict:
    due = due_staff(cutoff)
    if dry_run:
        return {"staff": due.count()}
    return {"staff": erase_each(due, erase_staff)}


def erase_staff(staff) -> None:
    from vs_finance.models import StaffBankAccount
    from vs_user.erasure import erase_account

    from .models import (
        LeaveRequest, StaffDocument, StaffEmploymentEvent, StaffProfile,
        StaffQualification,
    )

    delete_rows(StaffQualification._base_manager.filter(staff=staff))
    delete_rows(StaffDocument._base_manager.filter(staff=staff))
    # The employment log refuses edits through save(); erasure is the one
    # change the law asks of it, and only its words go.
    StaffEmploymentEvent._base_manager.filter(staff=staff).update(reason="", note="")
    leave = LeaveRequest._base_manager.filter(staff=staff)
    scrub_history(LeaveRequest, list(leave.values_list("pk", flat=True)), {"note": ""})
    leave.update(note="")

    scrub_history(StaffProfile, [staff.pk], STAFF_BLANKS)
    for field, value in STAFF_BLANKS.items():
        setattr(staff, field, value)
    staff.erased_at = timezone.now()
    staff.save()

    StaffBankAccount._base_manager.filter(employee=staff.user).update(
        bank_code="", bank_name="", account_number="", resolved_account_name="",
        verification_note="",
    )
    erase_account(staff.user)
