"""What the pupil portal reads and writes, for the pupil already admitted.

Every function here takes the pupil record the request was admitted as
(``permissions.linked_student``), never an id from the request, and reads from
the module that owns the fact. Nothing is stored here.

**The pupil's own record holds no role switches.** Field Access switches belong
to roles, and a pupil holds none; what the portal shows a pupil is a fixed,
narrow shape of their own record, written for them, that never carries a staff
note, another pupil or a guardian's contact beyond the primary guardian's name,
relationship and phone.
"""
from __future__ import annotations

import datetime as dt
import re

from django.db import transaction

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from .exceptions import PhotoSelfEditOff, ProfileFieldInvalid

#: A phone number in any country's writing: 7 to 15 digits, with spaces,
#: dashes, brackets and a leading plus allowed between them.
_PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,24}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def active_enrolment(student):
    """The pupil's class: in the running year, else in the year being set up.

    A promotion runs once the running year's last term has ended, and moves
    the pupil into next year's class at once, while next year is still a
    draft. In the holiday between the two the pupil's class is next year's,
    and the portal shows it with that year's name rather than no class at all.
    """
    rows = student.enrolments.filter(is_active=True).select_related(
        "school_class__level", "school_class__branch", "session",
    )
    return (
        rows.filter(session__status="ACTIVE").first()
        or rows.filter(session__status="DRAFT").order_by("session__start_date").first()
    )


# ── Timetable ─────────────────────────────────────────────────────────────


def timetable(student) -> dict:
    """The pupil's class timetable for the active year, if it is published.

    A grid that has never been published, or is being changed (a published grid
    returns to draft when it is edited), answers ``published: false`` with no
    days rather than showing the pupil a draft. Each lesson names its subject,
    the teacher's display name and the room; breaks and assemblies are drawn
    with their times. ``today`` lists today's periods with a state of
    ``done``, ``now``, ``next`` or ``later`` worked out in the branch's own
    time zone, so a pupil in Lagos and the server agree on what "now" is.
    """
    from schools.vs_calendar.models import (
        DayOfWeek, Period, PeriodType, PublishState, TimetableSlot,
    )
    from schools.vs_calendar.services.bells import periods_in_force
    from schools.vs_calendar.services.calendar_rules import read_calendar_rules
    from schools.vs_calendar.constants import CFG_TEACHING_DAYS, CFG_WEEK_STARTS_ON
    from schools.vs_calendar.services.timetable import timetable_for
    from vs_config.clock import branch_now

    tenant = student.tenant
    enrolment = active_enrolment(student)
    body = {"published": False, "class_name": "", "days": [], "today": None}
    if enrolment is None:
        return body
    school_class, session = enrolment.school_class, enrolment.session
    body["class_name"] = school_class.name
    record = timetable_for(tenant, session, school_class)
    if record is None or record.status != PublishState.PUBLISHED:
        return body

    slots = {
        (s.day_of_week, s.period_id): s
        for s in TimetableSlot.objects.filter(
            session=session, school_class=school_class,
        ).select_related("subject", "teacher", "room")
    }
    periods = list(
        Period.objects.filter(tenant=tenant, session=session, is_active=True)
        .select_related("branch"),
    )
    rules = read_calendar_rules(tenant, (CFG_TEACHING_DAYS, CFG_WEEK_STARTS_ON))
    branch = school_class.branch or student.branch
    now = branch_now(tenant, branch)

    days = []
    for day in rules.grid_days(held={key[0] for key in slots}):
        cells = []
        for period in periods_in_force(
            tenant, session, day_of_week=day, branch=branch, queryset=periods,
        ):
            cell = {
                "start_time": period.start_time,
                "end_time": period.end_time,
                "kind": period.period_type,
                "label": period.label,
            }
            if period.period_type == PeriodType.LESSON:
                slot = slots.get((day, period.pk))
                cell.update({
                    "subject": slot.subject.name if slot else "",
                    "teacher": slot.teacher.full_name if slot and slot.teacher_id else "",
                    "room": slot.room.name if slot and slot.room_id else "",
                })
            else:
                cell["label"] = period.label or period.get_period_type_display()
            cells.append(cell)
        days.append({
            "day_of_week": int(day),
            "day_label": DayOfWeek(day).label,
            "is_today": int(day) == now.isoweekday(),
            "cells": cells,
        })
    body.update({"published": True, "days": days})
    today = next((d for d in days if d["is_today"]), None)
    if today is not None:
        body["today"] = {
            "date": now.date(),
            "periods": _today_states(today["cells"], now.time()),
        }
    return body


def _today_states(cells, now: dt.time) -> list:
    """Today's periods, each marked done, now, next or later.

    Only the first lesson still to come is ``next``; breaks are marked but never
    ``next``, because a pupil looking for their next class is not looking for
    break.
    """
    from schools.vs_calendar.models import PeriodType

    out, found_next = [], False
    for cell in cells:
        if cell["end_time"] <= now:
            state = "done"
        elif cell["start_time"] <= now:
            state = "now"
        elif not found_next and cell["kind"] == PeriodType.LESSON:
            state, found_next = "next", True
        else:
            state = "later"
        out.append({**cell, "state": state})
    return out


# ── Profile ───────────────────────────────────────────────────────────────


def photo_self_edit(tenant) -> bool:
    """Whether this school lets pupils change their own photograph."""
    from schools.vs_students.constants import CFG_PORTAL_PHOTO_SELF_EDIT
    from schools.vs_students.services.rules import resolve_many

    found = resolve_many((CFG_PORTAL_PHOTO_SELF_EDIT,), tenant=tenant)
    value, _ = found.get(CFG_PORTAL_PHOTO_SELF_EDIT, (True, None))
    return value is not False


def profile(student, *, request=None) -> dict:
    """The pupil's own record: what the school manages, and what they may edit.

    ``school_email`` is the address on the pupil's sign-in account, which the
    school set; ``email`` and ``phone`` are the pupil's own contact details on
    their record, the two things they may change. The primary guardian appears
    by name, relationship and phone only.
    """
    from schools.vs_students.services.documents import face_url
    from schools.vs_students.services.guardians import primary_for

    from schools.vs_students.models import StudentGuardian

    enrolment = active_enrolment(student)
    guardian = primary_for(student)
    link = (
        StudentGuardian.objects.filter(student=student, is_primary=True).first()
        if guardian is not None else None
    )
    return {
        "photo_url": face_url(student, request=request) or "",
        "photo_editable": photo_self_edit(student.tenant),
        "managed_by_school": {
            "full_name": student.full_name,
            "admission_number": student.student_number or "",
            "class_name": enrolment.school_class.name if enrolment else "",
            "session_name": enrolment.session.name if enrolment else "",
            "date_of_birth": student.date_of_birth,
            "school_email": (student.user.email or "") if student.user_id else "",
            "primary_guardian": (
                {
                    "name": guardian.full_name,
                    "relationship": link.get_relationship_display() if link else "",
                    "phone": guardian.phone or "",
                }
                if guardian is not None else None
            ),
        },
        "editable": {"phone": student.phone or "", "email": student.email or ""},
    }


def _clean_phone(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if not _PHONE_RE.match(text) or not 7 <= len(digits) <= 15:
        raise ProfileFieldInvalid(
            "Enter a phone number of 7 to 15 digits, such as 0803 456 7890.",
            field="phone",
        )
    return text


def _clean_email(value) -> str:
    text = str(value or "").strip().lower()
    if text and (len(text) > 254 or not _EMAIL_RE.match(text)):
        raise ProfileFieldInvalid("Enter a valid email address.", field="email")
    return text


@transaction.atomic
def update_contact(student, *, actor, phone=None, email=None):
    """Change the pupil's own phone or email; ``None`` leaves a field as it is.

    The record's email is the pupil's contact address, not their sign-in: the
    account keeps the address the school gave it. Every change is audited with
    the pupil as its actor and the old and new values, as a staff edit of the
    same fields is.
    """
    student = type(student).objects.select_for_update().get(pk=student.pk)
    changes = {}
    if phone is not None:
        cleaned = _clean_phone(phone)
        if cleaned != (student.phone or ""):
            changes["phone"] = {"from": student.phone or "", "to": cleaned}
            student.phone = cleaned
    if email is not None:
        cleaned = _clean_email(email)
        if cleaned != (student.email or ""):
            changes["email"] = {"from": student.email or "", "to": cleaned}
            student.email = cleaned
    if not changes:
        return student
    student.save(update_fields=[*changes, "updated_at"])
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.UPDATE,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=f"{student.full_name} changed their own contact details.",
        metadata={"via": "student_portal", "changed": sorted(changes)},
        diff_data=changes,
    )
    return student


def set_photo(student, *, actor, upload):
    """Replace the pupil's photograph, where the school lets pupils do that."""
    from schools.vs_students.constants import DocumentType
    from schools.vs_students.services import documents

    if not photo_self_edit(student.tenant):
        raise PhotoSelfEditOff()
    return documents.attach(
        student, document_type=DocumentType.PASSPORT_PHOTO, upload=upload, actor=actor,
    )


def remove_photo(student, *, actor):
    """Remove the pupil's photograph, where the school lets pupils do that."""
    from schools.vs_students.constants import DocumentType
    from schools.vs_students.models import StudentDocument
    from schools.vs_students.services import documents

    if not photo_self_edit(student.tenant):
        raise PhotoSelfEditOff()
    doc = StudentDocument.objects.filter(
        student=student, document_type=DocumentType.PASSPORT_PHOTO,
    ).first()
    if doc is not None:
        documents.remove(student, doc, actor=actor)
