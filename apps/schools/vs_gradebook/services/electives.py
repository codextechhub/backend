"""The gradebook's say in who takes an optional subject.

Who takes what belongs to academic structure
(:mod:`schools.vs_academics.services.electives`), which cannot know about the
scores that rest on a choice. The gradebook registers a removal guard with it
(from ``AppConfig.ready``): taking a student out of a subject is refused while
the subject's sheet for that class and term holds a mark or marker for them,
because the scores would be orphaned and a student with entered marks would
silently drop off the sheet.
"""
from __future__ import annotations

from django.db.models import Q

from schools.vs_students.models import Student

from ..models import Score


def refuse_removal_with_scores(tenant, school_class, term, subject, student_id):
    """The sentence refusing the removal, or None when the student has no scores to lose."""
    entered = Score.objects.filter(
        tenant=tenant, student_id=student_id,
        sheet__class_term__school_class=school_class, sheet__class_term__term=term,
        sheet__subject=subject,
    ).filter(Q(mark__isnull=False) | Q(marker__isnull=False)).exists()
    if not entered:
        return None
    student = Student.objects.filter(tenant=tenant, pk=student_id).first()
    name = student.full_name if student is not None else "This student"
    return (
        f"{name} already has scores in {subject.name} for {term.name}, so "
        f"{subject.name} cannot be taken off them. Clear their scores on the "
        f"{school_class.name} {subject.name} sheet first, or leave the subject on."
    )
