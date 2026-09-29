"""Whether a lesson's teacher holds a teaching duty for it.

A teaching duty is ``vs_staff``'s record that a person teaches a subject to a
class in a session (``TeachingAssignment``, lead or assistant). The timetable
says when; the duty says who. The school's ``timetable.teacher_duty_match``
decides what a mismatch between the two means:

* **OFF**: nothing. Anyone holding the teacher role may take any lesson.
* **WARN**: a lesson saved with such a teacher stands, and the save answers
  with a ``TEACHER_HAS_NO_DUTY`` warning beside it. The grid read lists the
  same warnings, and publishing goes ahead and lists them too.
* **REFUSE**: the save is refused with ``NO_TEACHING_DUTY``, and publishing
  is refused with ``TIMETABLE_TEACHER_HAS_NO_DUTY`` while any such lesson
  remains, because a duty withdrawn after the save leaves one behind. The grid
  read still lists them as warnings, so the school can find them.

A lesson with no teacher is never a mismatch: it is incomplete, which the
publish gate refuses on its own terms.

The duties are read through ``vs_staff.services.teaching.duty_holders``, one
query per question however many lessons are asked about, so this module never
reads ``vs_staff``'s tables itself.
"""
from __future__ import annotations

from ..constants import WARN_TEACHER_HAS_NO_DUTY, DutyMatch
from ..exceptions import NoTeachingDuty
from .clashes import Warning_
from .teachers import display_name


def _holders(tenant, session, pairs):
    from schools.vs_staff.services.teaching import duty_holders

    return duty_holders(tenant, session, pairs)


def lessons_without_duty(tenant, session, slots) -> list:
    """The staffed lessons among *slots* whose teacher holds no duty for them."""
    staffed = [slot for slot in slots if slot.teacher_id]
    if not staffed:
        return []
    holders = _holders(
        tenant, session,
        {(slot.school_class_id, slot.subject_id) for slot in staffed},
    )
    return [
        slot for slot in staffed
        if slot.teacher_id not in holders.get(
            (slot.school_class_id, slot.subject_id), set(),
        )
    ]


def no_duty_sentence(teacher, school_class, subject) -> str:
    """The sentence every mismatch is told in.

    "Chioma Okafor has no teaching duty for JSS1 A Mathematics."
    """
    return (
        f"{display_name(teacher)} has no teaching duty for {school_class.name} "
        f"{subject.name}."
    )


def duty_warnings(tenant, session, slots, *, mode) -> list:
    """A ``TEACHER_HAS_NO_DUTY`` warning per lesson without a duty, unless *mode* is OFF.

    An unsaved lesson (the preview) carries no slot id.
    """
    if mode == DutyMatch.OFF:
        return []
    return [
        Warning_(
            code=WARN_TEACHER_HAS_NO_DUTY,
            detail=no_duty_sentence(slot.teacher, slot.school_class, slot.subject),
            slot_ids=[slot.pk] if slot.pk else [],
        )
        for slot in lessons_without_duty(tenant, session, slots)
    ]


def assert_teacher_has_duty(tenant, session, *, school_class, subject, teacher,
                            mode):
    """Refuse a lesson whose teacher holds no duty for it, when *mode* is REFUSE.

    The sentence names the colleagues who do hold the duty, so the person
    fixing the grid has the other way out in front of them.
    """
    if mode != DutyMatch.REFUSE or teacher is None:
        return
    holders = _holders(tenant, session, {(school_class.pk, subject.pk)}).get(
        (school_class.pk, subject.pk), set(),
    )
    if teacher.pk in holders:
        return
    raise NoTeachingDuty(
        _refusal(teacher, school_class, subject, holders - {teacher.pk}),
        field="teacher",
    )


def _refusal(teacher, school_class, subject, colleague_ids) -> str:
    from vs_user.models import User

    pronoun = {"FEMALE": "her", "MALE": "him"}.get(
        getattr(teacher, "gender", "") or "", "them",
    )
    head = (
        f"{no_duty_sentence(teacher, school_class, subject)} Give {pronoun} the "
        f"duty in Teaching duties first"
    )
    names = sorted(
        display_name(person) for person in User.objects.filter(
            pk__in=colleague_ids, status=User.Status.ACTIVE,
        )
    )
    if not names:
        return f"{head}."
    if len(names) == 1:
        return f"{head}, or choose {names[0]}, who has it."
    listed = ", ".join(names[:-1]) + f" or {names[-1]}"
    return f"{head}, or choose a colleague who has it: {listed}."
