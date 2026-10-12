"""The daily class register, the lesson registers, their marks and the reasons for absence.

Every model carries its own ``tenant`` foreign key, even where its parent
already has one, because :class:`vs_rbac.managers.TenantAwareManager` filters on
a model's own ``tenant`` field and returns everything otherwise. Reaching the
tenant through a parent is not scoping.

**A register is one class on one day**, created the first time somebody marks
a student in it. A class with no row for a day has a register that has not been
started, which is a different fact from a register that was taken and found
nobody absent, so the absence of a row is never read as "everyone present".

**A register has exactly one branch, and it is never null.** It is the class's
branch (or the school's only branch where the class names none), so a register
is a transaction in the platform's branch sense: a branch-bound reader sees
their own branches' registers and no others, and a notice names the branch it
came from.

**What is derived is never stored.** There is no rate, no count and no status
column. The rate is present over present plus absent plus excused, worked out
from the marks of the registers actually taken, so a correction can never
leave a stored figure disagreeing with the marks behind it.

**History is append-only.** A mark's earlier values live in :class:`MarkChange`
and in :class:`CorrectionRequest`; the original mark is never deleted. A change
row refuses to be saved twice or deleted. A lesson mark's earlier values live in
:class:`LessonMarkChange`.

**A lesson register is one class in one period on one day** (:class:`LessonRegister`),
kept apart from the class register because the two answer different questions:
the class register is whether a child came to school, a lesson register is
whether they were in the room. Neither overwrites the other.

**The staff register is one branch on one day** (:class:`StaffDay`), with one mark
per person (:class:`StaffMark`). A person is on the register of their home
branch (``StaffProfile.branch``); an additional posting does not put them on a
second register, so a person has one mark a day however many branches they work
at. The database holds that as a unique constraint over ``(staff, date)``, not
as a service check: ``StaffMark`` carries the day's date beside its register so
the constraint can name it, and a person whose home branch changes mid-day
keeps the one mark they have, which moves with them to the register they now
belong to. A staff register records what an administrator saw and sends nothing
to anyone.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import (
    LESSON_MARK_CHOICES,
    STAFF_MARK_CHOICES,
    ChangeVia,
    CorrectionStatus,
    Mark,
    NoticeState,
)


class _Owned(models.Model):
    """Managers and timestamps, shared by every model here.

    The ``tenant`` column is deliberately not declared here: one base column
    would mean one ``related_name`` across every model, so it would have to be
    ``"+"``, which disables the reverse accessor entirely.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


class ReasonCode(_Owned):
    """One of the school's own reasons for excusing an absence.

    A code is retired rather than deleted: marks that used it keep it, and no
    new mark is offered it. ``counts_as_authorised`` is stored so the add form
    can carry it; no calculation reads it yet.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_reason_codes",
    )
    name = models.CharField(max_length=80)
    counts_as_authorised = models.BooleanField(default=True)
    #: True for a reason that explains nothing by itself, such as "Other".
    requires_note = models.BooleanField(default=False)
    retired_at = models.DateTimeField(null=True, blank=True)
    order_index = models.PositiveSmallIntegerField(default=0)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "tenant", name="uq_attendance_reason_tenant_name_ci",
            ),
        ]
        ordering = ["order_index", "id"]

    def __str__(self):
        return self.name

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None


class ClassRegister(_Owned):
    """One class's register for one day."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="registers",
    )
    date = models.DateField()
    #: Non-null: see the module docstring.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm", on_delete=models.PROTECT,
        null=True, blank=True, related_name="attendance_registers",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    #: The class teacher, set when somebody else submitted the register.
    on_behalf_of = models.ForeignKey(
        "vs_staff.StaffProfile", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "date"], name="uq_attendance_register_class_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "branch", "date"]),
        ]
        ordering = ["-date", "school_class_id"]

    def __str__(self):
        return f"{self.school_class_id} on {self.date}"

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None


class StudentMark(_Owned):
    """One student's mark in one register."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_marks",
    )
    register = models.ForeignKey(
        ClassRegister, on_delete=models.PROTECT, related_name="marks",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT,
        related_name="attendance_marks",
    )
    mark = models.CharField(max_length=1, choices=Mark.choices)
    #: When a late student arrived. Carried by a Late mark and by nothing else.
    arrived_at = models.TimeField(null=True, blank=True)
    #: Why an absence is excused. Carried by an Excused mark and by nothing else.
    reason_code = models.ForeignKey(
        ReasonCode, on_delete=models.PROTECT,
        null=True, blank=True, related_name="marks",
    )
    note = models.CharField(max_length=300, blank=True, default="")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    marked_at = models.DateTimeField(default=timezone.now)
    notice_state = models.CharField(
        max_length=12, choices=NoticeState.choices, default=NoticeState.NONE,
    )
    notice_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["register", "student"], name="uq_attendance_mark_register_student",
            ),
            models.CheckConstraint(
                condition=Q(mark=Mark.LATE) | Q(arrived_at__isnull=True),
                name="ck_attendance_mark_arrival_only_late",
            ),
            models.CheckConstraint(
                condition=Q(mark=Mark.EXCUSED) | Q(reason_code__isnull=True),
                name="ck_attendance_mark_reason_only_excused",
            ),
        ]
        indexes = [
            models.Index(fields=["student", "register"]),
            models.Index(fields=["tenant", "register"]),
        ]

    def __str__(self):
        return f"{self.student_id}: {self.mark}"


class MarkChange(_Owned):
    """One change to a submitted mark. Appended, never edited or deleted."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_mark_changes",
    )
    mark = models.ForeignKey(
        StudentMark, on_delete=models.PROTECT, related_name="changes",
    )
    #: Blank for a student who had no mark when the change was made.
    from_mark = models.CharField(max_length=1, choices=Mark.choices, blank=True, default="")
    to_mark = models.CharField(max_length=1, choices=Mark.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    via = models.CharField(max_length=12, choices=ChangeVia.choices)
    note = models.CharField(max_length=300, blank=True, default="")

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "mark"])]
        ordering = ["changed_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("A mark change is history and is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("A mark change is history and is never deleted.")


class CorrectionRequest(_Owned):
    """A request to change a mark after its day has closed.

    ``reason_code`` is why the correction is asked for, from the school's own
    list, and is required for every correction. When the requested mark is
    Excused it is also the reason the absence is excused. ``arrived_at`` is
    carried by a request for Late and by nothing else.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_corrections",
    )
    mark = models.ForeignKey(
        StudentMark, on_delete=models.PROTECT, related_name="corrections",
    )
    from_mark = models.CharField(max_length=1, choices=Mark.choices)
    to_mark = models.CharField(max_length=1, choices=Mark.choices)
    arrived_at = models.TimeField(null=True, blank=True)
    reason_code = models.ForeignKey(
        ReasonCode, on_delete=models.PROTECT,
        null=True, blank=True, related_name="corrections",
    )
    note = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=10, choices=CorrectionStatus.choices,
        default=CorrectionStatus.PENDING,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True, default="")

    class Meta(_Owned.Meta):
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["mark", "status"]),
        ]
        ordering = ["-requested_at", "-id"]


class RegisterReminder(_Owned):
    """A reminder sent to a class teacher about a register not yet taken."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_reminders",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="register_reminders",
    )
    date = models.DateField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "date"], name="uq_attendance_reminder_class_date",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "date"])]


class LessonRegister(_Owned):
    """One class's register for one lesson on one day.

    A lesson is a class, a period and a date. The timetable is edited all term,
    and saving a class's grid replaces its slots, so a register cannot depend on
    the slot it was taken against: ``slot`` is kept as a pointer and is cleared
    when the slot goes, and the lesson is identified by ``(school_class, period,
    date)``, which a grid save does not change. Everything a reader needs to
    describe the lesson is copied onto the row when it is created (the period's
    label and times, the subject and the teacher), so a later edit to the
    timetable never rewrites a lesson that was already taken.

    Like a class register, the row is created by the first mark and has exactly
    one branch, never null. A lesson register is never sent to anyone.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_lesson_registers",
    )
    slot = models.ForeignKey(
        "vs_calendar.TimetableSlot", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    period = models.ForeignKey(
        "vs_calendar.Period", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="lesson_registers",
    )
    date = models.DateField()
    #: Non-null: see :class:`ClassRegister`.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="attendance_lesson_registers",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="attendance_lesson_registers",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm", on_delete=models.PROTECT,
        null=True, blank=True, related_name="attendance_lesson_registers",
    )
    #: The period as it was when the register was created.
    period_label = models.CharField(max_length=40)
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT, related_name="+",
    )
    #: Who taught the lesson when the register was created.
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "period", "date"],
                condition=Q(period__isnull=False),
                name="uq_attendance_lesson_class_period_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "branch", "date"]),
            models.Index(fields=["school_class", "date"]),
        ]
        ordering = ["-date", "school_class_id", "starts_at"]

    def __str__(self):
        return f"{self.school_class_id} {self.period_label} on {self.date}"

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None


class LessonMark(_Owned):
    """One student's mark in one lesson register: Present, Late or Absent.

    There is no Excused and no reason code, because a lesson mark explains
    nothing to a family and never reaches one.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_lesson_marks",
    )
    register = models.ForeignKey(
        LessonRegister, on_delete=models.PROTECT, related_name="marks",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT,
        related_name="attendance_lesson_marks",
    )
    mark = models.CharField(max_length=1, choices=LESSON_MARK_CHOICES)
    #: When a late student arrived. Carried by a Late mark and by nothing else.
    arrived_at = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True, default="")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    marked_at = models.DateTimeField(default=timezone.now)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["register", "student"],
                name="uq_attendance_lesson_mark_register_student",
            ),
            models.CheckConstraint(
                condition=Q(mark__in=[Mark.PRESENT, Mark.LATE, Mark.ABSENT]),
                name="ck_attendance_lesson_mark_not_excused",
            ),
            models.CheckConstraint(
                condition=Q(mark=Mark.LATE) | Q(arrived_at__isnull=True),
                name="ck_attendance_lesson_mark_arrival_only_late",
            ),
        ]
        indexes = [
            models.Index(fields=["student", "register"]),
            models.Index(fields=["tenant", "register"]),
        ]

    def __str__(self):
        return f"{self.student_id}: {self.mark}"


class LessonMarkChange(_Owned):
    """One change to a submitted lesson mark. Appended, never edited or deleted.

    A separate table from :class:`MarkChange` rather than a second foreign key
    on it: a class mark's history points at a class mark, and a column that is
    sometimes one thing and sometimes another would leave every reader of the
    history guarding against the wrong half. A lesson mark changes only in
    place (there is no correction request for a lesson), so there is no ``via``.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_lesson_mark_changes",
    )
    mark = models.ForeignKey(
        LessonMark, on_delete=models.PROTECT, related_name="changes",
    )
    #: Blank for a student who had no mark when the change was made.
    from_mark = models.CharField(
        max_length=1, choices=LESSON_MARK_CHOICES, blank=True, default="",
    )
    to_mark = models.CharField(max_length=1, choices=LESSON_MARK_CHOICES)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True, default="")

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "mark"])]
        ordering = ["changed_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("A lesson mark change is history and is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("A lesson mark change is history and is never deleted.")


class StaffDay(_Owned):
    """One branch's staff register for one day.

    Created by the first mark, with exactly one branch, never null: the branch of
    the people on it. A branch-bound reader sees their own branches' staff
    registers and no others.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_staff_days",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="attendance_staff_days",
    )
    date = models.DateField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "date"], name="uq_attendance_staff_day_branch_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "branch", "date"]),
        ]
        ordering = ["-date", "branch_id"]

    def __str__(self):
        return f"{self.branch_id} staff on {self.date}"

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None


class StaffMark(_Owned):
    """One member of staff's mark for one day: Present, Late or Absent.

    ``date`` repeats the register's date so that "one mark per person per day" is
    a unique constraint the database enforces, whichever register the mark sits
    on. There is no Excused and no reason code: a member of staff away with
    permission is on leave, which is read from Staff Management and never marked.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_staff_marks",
    )
    staff_day = models.ForeignKey(
        StaffDay, on_delete=models.PROTECT, related_name="marks",
    )
    staff = models.ForeignKey(
        "vs_staff.StaffProfile", on_delete=models.PROTECT,
        related_name="attendance_marks",
    )
    date = models.DateField()
    mark = models.CharField(max_length=1, choices=STAFF_MARK_CHOICES)
    #: When a late member of staff arrived. Carried by a Late mark and by nothing else.
    arrived_at = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True, default="")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    marked_at = models.DateTimeField(default=timezone.now)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "date"], name="uq_attendance_staff_mark_staff_date",
            ),
            models.CheckConstraint(
                condition=Q(mark__in=[Mark.PRESENT, Mark.LATE, Mark.ABSENT]),
                name="ck_attendance_staff_mark_not_excused",
            ),
            models.CheckConstraint(
                condition=Q(mark=Mark.LATE) | Q(arrived_at__isnull=True),
                name="ck_attendance_staff_mark_arrival_only_late",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "staff_day"]),
            models.Index(fields=["tenant", "date"]),
        ]

    def __str__(self):
        return f"{self.staff_id}: {self.mark} on {self.date}"


class StaffMarkChange(_Owned):
    """One change to a submitted staff mark. Appended, never edited or deleted."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="attendance_staff_mark_changes",
    )
    mark = models.ForeignKey(
        StaffMark, on_delete=models.PROTECT, related_name="changes",
    )
    #: Blank for a person who had no mark when the change was made.
    from_mark = models.CharField(
        max_length=1, choices=STAFF_MARK_CHOICES, blank=True, default="",
    )
    to_mark = models.CharField(max_length=1, choices=STAFF_MARK_CHOICES)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True, default="")

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "mark"])]
        ordering = ["changed_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("A staff mark change is history and is never edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("A staff mark change is history and is never deleted.")
