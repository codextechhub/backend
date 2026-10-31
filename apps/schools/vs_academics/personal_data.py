"""Academic sections of a personal data copy (:mod:`core.personal_data`)."""
from __future__ import annotations

from core.personal_data import PUPIL, STAFF, register, rows


def _electives(pupil):
    from .models import ElectiveTaker

    return rows(ElectiveTaker._base_manager.filter(student=pupil).order_by("pk"), skip=("student",))


def _roles(staff):
    from .models import Department, SchoolClass

    return {
        "Departments headed": [str(d) for d in Department._base_manager.filter(head=staff)],
        "Classes as class teacher": [str(c) for c in SchoolClass._base_manager.filter(class_teacher=staff)],
    }


def register_sections() -> None:
    register(PUPIL, "electives", "Elective subjects", _electives,
             covers=("vs_academics.ElectiveTaker",))
    register(STAFF, "academic_roles", "Departments and classes led", _roles,
             covers=("vs_academics.Department", "vs_academics.SchoolClass"))
