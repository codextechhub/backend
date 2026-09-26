"""The student and guardian rows whose history a profile can be read at.

A student's page shows the record, its guardians, its classes and its
documents, and a guardian's page shows the guardian and their wards. Each of
those rows keeps a history here, so either page can be rebuilt as it stood on
an earlier day. The status log keeps its own dated rows and needs none.

A guardian link belongs on both pages, so it names the student and the
guardian as owners.
"""
from vs_history.registry import track

STUDENT = "vs_students.student"
GUARDIAN = "vs_students.guardian"


def register():
    """Declare the tracked student-side models to the history engine."""
    from .models import ClassEnrolment, Guardian, Student, StudentDocument, StudentGuardian

    track(Student)
    track(Guardian)
    track(
        StudentGuardian,
        owners=lambda link: [(STUDENT, link.student_id), (GUARDIAN, link.guardian_id)],
    )
    track(ClassEnrolment, owners=lambda row: [(STUDENT, row.student_id)])
    track(StudentDocument, owners=lambda row: [(STUDENT, row.student_id)])
