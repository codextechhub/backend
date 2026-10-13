"""A term's sheets across many classes, read once.

The hubs and the moderation queue look at every class in the caller's branches
at the same time, so the facts they need are read for all the classes together
and in a fixed number of queries however many classes there are: the classes'
terms, the offerings of their levels, the recorded takers of optional subjects,
the rolls, the sheets, and who leads each subject. Nothing here is read per
class.

**What is due.** A class owes a sheet for every core subject its level offers
and for every optional subject at least one of its students takes. A subject
whose sheet roll is empty owes nothing: there is nobody to enter marks for.

**What is in.** A sheet is in when it was submitted and needs no moderation, or
when its moderator approved it. A returned sheet is not in, and neither is one
waiting for its department head.

**The stage of a class** is stored only for the two decisions (awaiting the
principal, published); otherwise it is read off the sheets, COMPILING when every
due sheet is in and ENTERING when any is not. A class that owes nothing is
ENTERING.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Q

from schools.vs_academics.models import ElectiveTaker, SubjectOffering
from schools.vs_calendar.models import ExamSlot, PublishState, Sitting
from schools.vs_academics.services import electives, heads
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import TeachingAssignment
from schools.vs_students.services.roster import rosters_as_at
from vs_config.clock import branch_today

from ..constants import ClassStage, SheetStatus, StoredStage
from ..models import AssessmentComponent, ClassTerm, Score, ScoreSheet


@dataclass
class DueSheet:
    """One sheet a class owes: its subject, who is on it, who leads it and what exists of it."""

    subject: object
    roll_ids: list
    sheet: ScoreSheet | None
    teacher: object | None
    head: object | None

    @property
    def status(self) -> str:
        return self.sheet.status if self.sheet is not None else SheetStatus.NOT_STARTED

    @property
    def is_in(self) -> bool:
        return is_in(self.sheet)


@dataclass
class ClassEntry:
    """One class in the term: its term row, its roll and the sheets it owes.

    ``roll`` is the class roll as ``RosterEntry`` rows in name order and
    ``roll_ids`` the same students' ids.
    """

    school_class: object
    home: object
    class_term: ClassTerm | None
    roll_ids: list
    due: list = field(default_factory=list)
    roll: list = field(default_factory=list)

    @property
    def subjects_due(self) -> int:
        return len(self.due)

    @property
    def subjects_in(self) -> int:
        return sum(1 for item in self.due if item.is_in)

    @property
    def stage(self) -> str:
        stored = self.class_term.stage if self.class_term is not None else None
        if stored in (StoredStage.AWAITING, StoredStage.PUBLISHED):
            return stored
        if self.due and self.subjects_in == self.subjects_due:
            return ClassStage.COMPILING
        return ClassStage.ENTERING

    @property
    def returned(self) -> bool:
        return self.class_term is not None and bool(self.class_term.returned_note)


def is_in(sheet) -> bool:
    """Whether *sheet* counts as in: submitted and unmoderated, or approved."""
    if sheet is None:
        return False
    if sheet.status == SheetStatus.APPROVED:
        return True
    return sheet.status == SheetStatus.SUBMITTED and not sheet.needs_moderation


def rolls_for(tenant, term, placed) -> dict:
    """``{class_id: [RosterEntry]}`` for ``[(class, branch)]``, each as at its own branch's day.

    A class's roll is as at the earlier of its branch's today and the term's
    last day, as a single sheet reads it (:func:`.resolve.sheet_roll`).
    """
    days: dict = {}
    by_day: dict = {}
    for school_class, home in placed:
        if home.pk not in days:
            days[home.pk] = min(branch_today(tenant, home), term.end_date)
        by_day.setdefault(days[home.pk], []).append(school_class)
    out: dict = {}
    for day, group in by_day.items():
        out.update(rosters_as_at(tenant, group, day))
    return out


def takers_for(tenant, term, class_ids) -> dict:
    """``{class_id: {subject_id: {student_id}}}`` for the optional subjects taken in the classes."""
    out: dict = {}
    rows = ElectiveTaker.objects.filter(
        tenant=tenant, term=term, school_class_id__in=list(class_ids),
    ).values_list("school_class_id", "subject_id", "student_id")
    for class_id, subject_id, student_id in rows:
        out.setdefault(class_id, {}).setdefault(subject_id, set()).add(student_id)
    return out


def leads_for(tenant, term, class_ids) -> dict:
    """``{(class_id, subject_id): StaffProfile}`` for the main teachers of the term's school year."""
    rows = TeachingAssignment.objects.filter(
        tenant=tenant, session_id=term.session_id, part=TeachingPart.LEAD,
        school_class_id__in=list(class_ids),
    ).select_related("staff", "staff__user")
    return {(row.school_class_id, row.subject_id): row.staff for row in rows}


def load(tenant, term, placed) -> list[ClassEntry]:
    """A :class:`ClassEntry` for each ``(class, branch)`` in *placed*, with its due sheets."""
    classes = [school_class for school_class, _ in placed]
    class_ids = [school_class.pk for school_class in classes]
    class_terms = {
        row.school_class_id: row
        for row in ClassTerm.objects.filter(
            tenant=tenant, term=term, school_class_id__in=class_ids,
        )
    }
    offerings: dict = {}
    for offering in (
        SubjectOffering.objects.filter(
            tenant=tenant, level_id__in={c.level_id for c in classes},
            subject__is_active=True,
        )
        .select_related(
            "subject", "subject__department", "subject__department__head",
            "subject__department__head__user",
        )
        .order_by("subject__name", "pk")
    ):
        offerings.setdefault(offering.level_id, []).append(offering)

    rolls = rolls_for(tenant, term, placed)
    takers = takers_for(tenant, term, class_ids)
    leads = leads_for(tenant, term, class_ids)

    by_term_id = {row.pk: class_id for class_id, row in class_terms.items()}
    sheets: dict = {}
    for sheet in ScoreSheet.objects.filter(
        tenant=tenant, class_term_id__in=list(by_term_id),
    ).select_related("returned_by"):
        sheets[(by_term_id[sheet.class_term_id], sheet.subject_id)] = sheet

    entries = []
    for school_class, home in placed:
        roll_ids = [entry.student.pk for entry in rolls.get(school_class.pk, [])]
        on_roll = set(roll_ids)
        entry = ClassEntry(
            school_class=school_class, home=home,
            class_term=class_terms.get(school_class.pk), roll_ids=roll_ids,
            roll=list(rolls.get(school_class.pk, [])),
        )
        for offering in offerings.get(school_class.level_id, []):
            subject = offering.subject
            if electives.effective_is_core(offering):
                students = roll_ids
            else:
                taking = takers.get(school_class.pk, {}).get(subject.pk, set())
                students = [pk for pk in roll_ids if pk in taking and pk in on_roll]
            if not students:
                continue
            department = subject.department
            entry.due.append(DueSheet(
                subject=subject, roll_ids=students,
                sheet=sheets.get((school_class.pk, subject.pk)),
                teacher=leads.get((school_class.pk, subject.pk)),
                head=heads.head_of(department) if department is not None else None,
            ))
        entries.append(entry)
    return entries


def entered_counts(tenant, entries) -> dict:
    """``{(class_id, subject_id): students whose row is filled}`` for the sheets not yet in.

    A row is filled when every component of the class's fixed structure holds a
    mark or a marker. A sheet that does not exist, or whose class has no
    structure fixed yet, has nothing entered. Scores of students who have left
    the roll are not counted.
    """
    wanted = {
        item.sheet.pk: (entry, item)
        for entry in entries
        for item in entry.due
        if item.sheet is not None and not item.is_in and entry.class_term is not None
        and entry.class_term.structure_id is not None
    }
    out = {
        (entry.school_class.pk, item.subject.pk): 0
        for entry in entries for item in entry.due
    }
    if not wanted:
        return out
    structure_ids = {entry.class_term.structure_id for entry, _ in wanted.values()}
    size = {
        row["structure_id"]: row["n"]
        for row in AssessmentComponent.objects.filter(
            tenant=tenant, structure_id__in=structure_ids,
        ).values("structure_id").annotate(n=Count("id"))
    }
    per_student: dict = {}
    filled = Score.objects.filter(tenant=tenant, sheet_id__in=list(wanted)).filter(
        Q(mark__isnull=False) | Q(marker__isnull=False),
    )
    for row in filled.values("sheet_id", "student_id").annotate(n=Count("id")):
        per_student.setdefault(row["sheet_id"], {})[row["student_id"]] = row["n"]
    for sheet_id, (entry, item) in wanted.items():
        needed = size.get(entry.class_term.structure_id, 0)
        counts = per_student.get(sheet_id, {})
        out[(entry.school_class.pk, item.subject.pk)] = sum(
            1 for student_id in item.roll_ids if needed and counts.get(student_id, 0) >= needed
        )
    return out


def exam_papers(tenant, term, class_ids, subject_ids) -> dict:
    """``{(class_id, subject_id): {date, sitting, sitting_label}}`` from the published papers in the term.

    Only a paper of a published exam counts, and only one whose date falls
    inside the term. Where a class sits a subject more than once the earliest
    is the one named, the morning sitting before the afternoon.
    """
    slots = ExamSlot.objects.filter(
        tenant=tenant, school_class_id__in=list(class_ids), subject_id__in=list(subject_ids),
        exam__status=PublishState.PUBLISHED,
        exam_date__gte=term.start_date, exam_date__lte=term.end_date,
    )
    best: dict = {}
    for slot in slots:
        key = (slot.school_class_id, slot.subject_id)
        rank = (slot.exam_date, 0 if slot.sitting == Sitting.MORNING else 1)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, slot)
    return {
        key: {
            "date": slot.exam_date, "sitting": slot.sitting,
            "sitting_label": Sitting(slot.sitting).label,
        }
        for key, (_, slot) in best.items()
    }
