"""A pupil's and a guardian's sections of a personal data copy (:mod:`core.personal_data`)."""
from __future__ import annotations

from core.personal_data import GUARDIAN, PUPIL, not_exported, register, row, rows


def _pupil_record(pupil):
    return row(pupil, skip=("user",))


def _pupil_guardians(pupil):
    from .models import StudentGuardian

    links = StudentGuardian._base_manager.filter(student=pupil).select_related("guardian")
    # The guardian's own contact details are theirs, not the pupil's: name and tie only.
    return [
        {"Guardian": str(link.guardian), "Relationship": link.get_relationship_display(),
         "Primary contact": link.is_primary}
        for link in links
    ]


def _of_pupil(model_name):
    def build(pupil):
        from django.apps import apps

        model = apps.get_model("vs_students", model_name)
        return rows(model._base_manager.filter(student=pupil).order_by("pk"), skip=("student",))

    return build


def _guardian_record(guardian):
    return row(guardian, skip=("user",))


def _guardian_children(guardian):
    from .models import StudentGuardian

    links = StudentGuardian._base_manager.filter(guardian=guardian).select_related("student")
    return [
        {"Child": str(link.student), "Relationship": link.get_relationship_display(),
         "Primary contact": link.is_primary}
        for link in links
    ]


def register_sections() -> None:
    register(PUPIL, "record", "Pupil record", _pupil_record, covers=("vs_students.Student",))
    register(PUPIL, "guardians", "Parents and guardians", _pupil_guardians,
             covers=("vs_students.StudentGuardian",))
    register(PUPIL, "enrolments", "Classes", _of_pupil("ClassEnrolment"),
             covers=("vs_students.ClassEnrolment",))
    register(PUPIL, "status_changes", "Status changes", _of_pupil("StudentStatusLog"),
             covers=("vs_students.StudentStatusLog",))
    register(PUPIL, "branch_moves", "Branch moves", _of_pupil("StudentBranchMove"),
             covers=("vs_students.StudentBranchMove",))
    register(PUPIL, "documents", "Documents on file", _of_pupil("StudentDocument"),
             covers=("vs_students.StudentDocument",))

    register(GUARDIAN, "record", "Guardian record", _guardian_record,
             covers=("vs_students.Guardian",))
    register(GUARDIAN, "children", "Children", _guardian_children,
             covers=("vs_students.StudentGuardian",))
