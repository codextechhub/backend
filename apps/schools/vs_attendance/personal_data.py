"""Attendance sections of a personal data copy (:mod:`core.personal_data`)."""
from __future__ import annotations

from core.personal_data import PUPIL, STAFF, not_exported, register, rows


def _marks(model_name, link):
    def build(person):
        from django.apps import apps

        model = apps.get_model("vs_attendance", model_name)
        return rows(model._base_manager.filter(**{link: person}).order_by("pk"), skip=(link,))

    return build


def register_sections() -> None:
    register(PUPIL, "attendance", "Daily attendance", _marks("StudentMark", "student"),
             covers=("vs_attendance.StudentMark",))
    register(PUPIL, "lesson_attendance", "Lesson attendance", _marks("LessonMark", "student"),
             covers=("vs_attendance.LessonMark",))
    register(STAFF, "attendance", "Staff attendance", _marks("StaffMark", "staff"),
             covers=("vs_attendance.StaffMark",))
    not_exported(STAFF, "vs_attendance.ClassRegister",
                 "A class's register: it is the pupils' record and only names who took it.")
