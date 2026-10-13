"""A student's Results tab: every term's published result and the history of their holds.

**One entry per term that has begun**, newest first, over the school years the
student has been placed in (and the school's current year, so a pupil with no
placement yet still reads a calm line for the term they are in). A term the
class published is read from the frozen snapshot, never recomputed: the overall
average, the position where that term ranked, the subjects with their total,
grade and remark, and the release state in words ("Released Tue 16 Dec 2025",
"Published, not yet released", "Held"). A term with nothing published is one
calm sentence. A held student's published term is still shown here: this is a
staff screen, and the hold is the thing being worked.

**History** lists each hold placed and each lifted, newest first, with who and
when. A placed hold carries its reason and a lifted one its lift note, as fields
of their own, so a caller whose hold-reason switch is closed loses them and the
sentence that names the event is left.
Amendments are a later build and none are listed.

Which students a caller may open is the student module's rule
(:func:`schools.vs_students.services.scoping.get_student_or_404`): another
school's student and one in a branch the caller cannot see answer 404.
"""
from __future__ import annotations

from schools.vs_academics.models import AcademicTerm
from schools.vs_attendance.services.notices import primary_guardians
from schools.vs_students.models import ClassEnrolment, Student
from schools.vs_students.services.roster import placement_in_force
from schools.vs_students.services.scoping import branch_dimension_applies, get_student_or_404
from vs_config.clock import branch_today, branch_zone

from ..constants import EmailState, HistoryKind, StoredStage
from ..models import ResultHold, StudentTermResult
from . import resolve
from .reportcards import day_label, position_label

NOTHING_PUBLISHED = (
    "Nothing has been published for this term yet. Results appear here once the "
    "principal approves and publishes the class."
)
RELEASED, NOT_RELEASED, HELD = "Released", "Published, not yet released", "Held"


def _ref(record) -> dict:
    return {"id": record.pk, "name": record.name}


def _release_label(result, tenant, branch) -> str:
    if result.released_at is not None:
        day = result.released_at.astimezone(branch_zone(tenant, branch)).date()
        return f"{RELEASED} {day_label(day)}"
    return HELD if result.email_state == EmailState.HELD else NOT_RELEASED


def _published_entry(term, result, tenant, branch) -> dict:
    snapshot = result.snapshot
    entry = {
        "term": _ref(term), "session": _ref(term.session), "published": True,
        "overall_average": snapshot.get("overall_average"),
        "released_at": result.released_at,
        "release_label": _release_label(result, tenant, branch),
        "subjects": [
            {
                "name": subject["subject"]["name"], "total": subject["total"],
                "grade": subject["grade"], "remark": subject["remark"],
            }
            for subject in snapshot["subjects"]
        ],
        "class_id": snapshot["class"]["id"],
    }
    if snapshot.get("position") is not None:
        entry["position"] = position_label(snapshot["position"], snapshot.get("roll_size"))
    return entry


def _term_entries(tenant, student, enrolments, today) -> list:
    """The tab's terms, newest first, each published or calm."""
    current = resolve.current_term(tenant, today)
    session_ids = {row.session_id for row in enrolments}
    if current is not None:
        session_ids.add(current.session_id)

    results = {}
    for result in (
        StudentTermResult.objects.filter(
            tenant=tenant, student=student, snapshot__isnull=False,
            class_term__stage=StoredStage.PUBLISHED,
        )
        .select_related("class_term", "class_term__term", "class_term__term__session")
        .order_by("class_term__published_at", "pk")
    ):
        results[result.class_term.term_id] = result

    terms = {
        term.pk: term
        for term in AcademicTerm.objects.filter(
            tenant=tenant, session_id__in=session_ids, start_date__lte=today,
        ).select_related("session")
    }
    for term_id, result in results.items():
        terms.setdefault(term_id, result.class_term.term)

    entries = []
    for term in sorted(terms.values(), key=lambda t: (t.start_date, t.order_index, t.pk), reverse=True):
        result = results.get(term.pk)
        if result is None:
            entries.append({
                "term": _ref(term), "session": _ref(term.session), "published": False,
                "message": NOTHING_PUBLISHED,
            })
        else:
            entries.append(_published_entry(term, result, tenant, student.branch))
    return entries


def _history(tenant, student) -> list:
    """The student's holds, placed and lifted, newest first."""
    entries = []
    holds = ResultHold.objects.filter(tenant=tenant, student=student).select_related(
        "term", "placed_by", "lifted_by",
    )
    for hold in holds:
        entries.append({
            "kind": HistoryKind.HOLD_PLACED,
            "text": f"Result held for {hold.term.name}", "reason": hold.reason,
            "by": hold.placed_by.full_name if hold.placed_by else None, "at": hold.placed_at,
        })
        if hold.lifted_at is not None:
            entries.append({
                "kind": HistoryKind.HOLD_LIFTED,
                "text": f"Hold lifted for {hold.term.name}", "lift_note": hold.lift_note,
                "by": hold.lifted_by.full_name if hold.lifted_by else None, "at": hold.lifted_at,
            })
    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries


def results_tab(tenant, user, student_id) -> dict:
    """The Results tab of one student, before Field Access is applied."""
    student = get_student_or_404(
        tenant, user, student_id, queryset=Student.objects.select_related("branch"),
    )
    today = branch_today(tenant, student.branch)
    enrolments = list(
        ClassEnrolment.objects.filter(tenant=tenant, student=student)
        .select_related("school_class")
    )
    current_term = resolve.current_term(tenant, today)
    placed = placement_in_force(
        [row for row in enrolments if current_term and row.session_id == current_term.session_id],
        today,
    )
    return {
        "student": {
            "id": student.pk, "name": student.full_name,
            "admission_number": student.student_number,
            "class": _ref(placed.school_class) if placed is not None else None,
            "branch": _ref(student.branch) if branch_dimension_applies(tenant) else None,
        },
        "guardian": primary_guardians(tenant, [student.pk]).get(student.pk),
        "terms": _term_entries(tenant, student, enrolments, today),
        "history": _history(tenant, student),
    }
