"""A staff member's sections of a personal data copy (:mod:`core.personal_data`)."""
from __future__ import annotations

from core.personal_data import STAFF, register, row, rows


def _record(staff):
    return row(staff, skip=("user",))


def _of_staff(model_name):
    def build(staff):
        from django.apps import apps

        model = apps.get_model("vs_staff", model_name)
        return rows(model._base_manager.filter(staff=staff).order_by("pk"), skip=("staff",))

    return build


SECTIONS = (
    ("employment", "Employment changes", "StaffEmploymentEvent"),
    ("positions", "Positions held", "StaffPositionAssignment"),
    ("qualifications", "Qualifications", "StaffQualification"),
    ("documents", "Documents on file", "StaffDocument"),
    ("leave", "Leave", "LeaveRequest"),
    ("teaching", "Teaching assignments", "TeachingAssignment"),
    ("class_teacher_terms", "Terms as class teacher", "ClassTeacherTerm"),
)


def register_sections() -> None:
    register(STAFF, "record", "Staff record", _record, covers=("vs_staff.StaffProfile",))
    for key, title, model_name in SECTIONS:
        register(STAFF, key, title, _of_staff(model_name), covers=(f"vs_staff.{model_name}",))
