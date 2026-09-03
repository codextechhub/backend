"""The dated contents of a school year, and the timetable that runs inside it.

Eight models. ``schools.vs_academics`` owns the year itself - the session, the
term, the class and the subject - and this module points at them and never
re-specifies them. FRD v3.0.1 section 5 is the boundary.

Every model carries its own ``tenant`` foreign key, even where its parent
already has one, because :class:`vs_rbac.managers.TenantAwareManager` filters on
a model's own ``tenant`` field and returns everything otherwise. Reaching the
tenant through a parent is not scoping.

**The branch column, and the one exception.** ``CalendarEvent`` and ``Period``
carry a nullable branch, where null means the whole school - the platform's
first-class shared value, never "no branches exist". ``Room`` carries a
**non-null** one, and it is the only such column in the schools product: a room
is a physical place and a place is at one branch, so null there would mean a
room belonging to everywhere, which no school has. FRD section 6.7 argues it and
section 6.4 gives the test to apply to any future column - a fact that can
genuinely differ by branch is nullable, a fact that is by its nature at one
branch is not.

**What is derived is never stored.** There is no teacher-timetable table: a
person's week is a query over ``TimetableSlot`` and a stored copy would go stale
the moment one slot moved. There is no clash column either, for the same reason
doubled - a clash is a relationship between two rows, so a flag on one of them
is a cache with no invalidation. FRD section 6.5.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager


class _Owned(models.Model):
    """Managers and timestamps, shared by every model here.

    The ``tenant`` column is deliberately not declared here: one base column
    would mean one ``related_name`` across eight models, so it would have to be
    ``"+"``, which disables the reverse accessor entirely. ``vs_academics``
    records the same reasoning and the same consequence.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


# ── Choices ────────────────────────────────────────────────────────────────

class EventType(models.TextChoices):
    HOLIDAY = "HOLIDAY", "Public holiday"
    MIDTERM_BREAK = "MIDTERM_BREAK", "Mid-term break"
    EXAM_PERIOD = "EXAM_PERIOD", "Exam period"
    SCHOOL_EVENT = "SCHOOL_EVENT", "School event"
    PTA = "PTA", "PTA"
    SPORTS = "SPORTS", "Sports day"


class RoomType(models.TextChoices):
    CLASSROOM = "CLASSROOM", "Classroom"
    LABORATORY = "LABORATORY", "Laboratory"
    HALL = "HALL", "Hall"
    LIBRARY = "LIBRARY", "Library"
    SPORTS = "SPORTS", "Sports"
    OTHER = "OTHER", "Other"


class PeriodType(models.TextChoices):
    LESSON = "LESSON", "Lesson"
    BREAK = "BREAK", "Break"
    LUNCH = "LUNCH", "Lunch"
    ASSEMBLY = "ASSEMBLY", "Assembly"


class DayOfWeek(models.IntegerChoices):
    """ISO-8601, so it agrees with ``date.isoweekday()`` without a conversion.

    Saturday and Sunday are here although the design's day picker offers only
    Monday to Friday. A Saturday school is a real thing and the column should
    not be the reason it cannot be recorded; which days a form offers is the
    client's choice.
    """

    MONDAY = 1, "Monday"
    TUESDAY = 2, "Tuesday"
    WEDNESDAY = 3, "Wednesday"
    THURSDAY = 4, "Thursday"
    FRIDAY = 5, "Friday"
    SATURDAY = 6, "Saturday"
    SUNDAY = 7, "Sunday"


class PublishState(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"


class Sitting(models.TextChoices):
    """Ordered by time of day, never compared as strings.

    "afternoon" sorts before "morning" lexically, which would invert every exam
    day holding both. ``SITTING_RANK`` in ``services.exams`` is the ordering.
    """

    MORNING = "MORNING", "Morning"
    AFTERNOON = "AFTERNOON", "Afternoon"


# ── The calendar half ──────────────────────────────────────────────────────

class CalendarEvent(_Owned):
    """A dated entry on the school calendar, inside one school year."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="calendar_events",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="calendar_events",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="calendar_events",
    )
    name = models.CharField(max_length=120)
    event_type = models.CharField(
        max_length=16, choices=EventType.choices, db_index=True,
    )
    start_date = models.DateField()
    end_date = models.DateField()
    #: Marks these days non-teaching on the calendar and in the teaching-day
    #: count. It does **not** touch a timetable: a school that closes for a
    #: public holiday does not delete that Tuesday's lessons, it simply does
    #: not hold them.
    closes_school = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Inclusive: a one-day holiday is the ordinary case, so the two
            # dates being equal is not an error. Contrast Period, where a
            # zero-length period is meaningless and the check is strict.
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="ck_calendar_event_dates",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "session", "start_date"]),
            models.Index(fields=["tenant", "session", "event_type"]),
        ]
        ordering = ["start_date", "name"]

    def __str__(self):
        return self.name


class CalendarEventAudience(_Owned):
    """Which levels or classes an event covers, when it covers only some.

    **No rows means the whole of the event's branch scope**, which is the common
    case and the default. Rows narrow it.

    The state this exists for: Lekki Branch holds a Speech Day for the primary
    school. Primary 4 A is off timetable and JSS1 A and JSS1 B are not. Without
    this table the event is either the whole of Lekki - so JSS1's teachers see a
    closure that is not theirs and their teaching-day count loses a day they
    actually taught - or it is not recorded and the primary teachers turn up.

    CASCADE, deliberately, and one of only two in this module: an audience row
    has no meaning without its event.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="calendar_event_audiences",
    )
    event = models.ForeignKey(
        CalendarEvent, on_delete=models.CASCADE, related_name="audience",
    )
    #: Exactly one of the two is set, enforced by the check constraint below.
    #: A level covers every class under it, which is what a school means by
    #: "the whole of JSS1" and saves it naming three arms one at a time.
    level = models.ForeignKey(
        "vs_academics.Level", on_delete=models.CASCADE,
        null=True, blank=True, related_name="calendar_event_audiences",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.CASCADE,
        null=True, blank=True, related_name="calendar_event_audiences",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(level__isnull=False, school_class__isnull=True)
                    | Q(level__isnull=True, school_class__isnull=False)
                ),
                name="ck_event_audience_exactly_one_target",
            ),
            models.UniqueConstraint(
                fields=["event", "level"],
                condition=Q(level__isnull=False),
                name="uq_event_audience_level",
            ),
            models.UniqueConstraint(
                fields=["event", "school_class"],
                condition=Q(school_class__isnull=False),
                name="uq_event_audience_class",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "event"])]

    def __str__(self):
        return f"{self.event_id}:{self.level_id or self.school_class_id}"


# ── The timetable half ─────────────────────────────────────────────────────

class Room(_Owned):
    """A place a lesson or an examination happens in.

    Nothing room-shaped existed anywhere in the platform. ``vs_tenants.Branch``
    is a whole site, so a timetable that could name only a branch could not
    detect a room clash at all - which is one of the three rules this half
    exists to provide. ``vs_procurement.StockLocation`` is a storeroom on a
    domain-neutral engine and means something else. A free-text room column on
    the slot is what makes room clash detection unreliable: "Block A Room 1" and
    "Block A Rm 1" are two rooms to a string comparison and one room to the
    school.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="rooms",
    )
    #: Non-null, and the only such branch column in the schools product. See
    #: the module docstring. Every school has at least one branch, so a room can
    #: always be given one; in a single-branch school it is filled from the only
    #: branch and the question is never asked on screen.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="rooms",
    )
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20, blank=True, default="")
    #: A label, not a rule. Nothing refuses a Physics lesson in a classroom,
    #: because nothing records which subject needs which kind of room.
    room_type = models.CharField(
        max_length=20, choices=RoomType.choices, db_index=True,
    )
    #: Advisory in the strict sense: nothing anywhere compares it with anything.
    #: This module has no student count at all, so a class of forty fits in a
    #: room of twenty-five as far as the server is concerned.
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta(_Owned.Meta):
        constraints = [
            # One constraint, not the usual pair. Uniqueness across a nullable
            # branch needs two partial constraints because NULL is not equal to
            # NULL in PostgreSQL; this column can never be null, so it does not.
            # The name repeats across branches freely - "Block A Room 1" at both
            # Lekki and Ikeja is normal - and never within one.
            models.UniqueConstraint(
                Lower("name"), "branch", name="uq_room_branch_name",
            ),
            models.UniqueConstraint(
                Lower("code"), "tenant",
                condition=~Q(code=""),
                name="uq_room_tenant_code",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "is_active"])]
        ordering = ["branch", "name"]

    def __str__(self):
        return self.name


class Period(_Owned):
    """One row of the daily bell schedule.

    The vertical axis of every timetable grid, and the only model here that
    depends on nothing outside this module, which is why it ships first.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="periods",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="periods",
    )
    #: Null means the school's bell schedule and is the default. A set branch
    #: means that branch rings its own bell, which a school whose Lekki branch
    #: starts at 08:00 and whose Ikeja branch starts at 07:45 genuinely needs.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="periods",
    )
    #: Null means every teaching day, which is the common case. A set value is
    #: that day's own schedule, and it **replaces** the everyday one rather than
    #: adding to it - see ``services.bells.periods_in_force``.
    day_of_week = models.PositiveSmallIntegerField(
        null=True, blank=True, choices=DayOfWeek.choices, db_index=True,
    )
    #: The row's position in the day. Assigned from the times by the service,
    #: never typed: the design's period form has no order field, so a caller
    #: cannot supply one. The constraint stays for rows that arrive by import,
    #: by fixture or by migration.
    order_index = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=40)
    #: Only a LESSON row may hold a timetable slot.
    period_type = models.CharField(
        max_length=16, choices=PeriodType.choices, db_index=True,
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta(_Owned.Meta):
        constraints = [
            # Strict, unlike CalendarEvent's: a zero-length period is not an
            # ordinary case the way a one-day holiday is.
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="ck_period_times",
            ),
            # Two partial constraints rather than one, because branch and
            # day_of_week are both nullable and NULL is not equal to NULL:
            # a single index over the four columns would not stop two
            # school-wide everyday periods sharing an order.
            models.UniqueConstraint(
                fields=["tenant", "session", "branch", "day_of_week", "order_index"],
                condition=Q(branch__isnull=False, day_of_week__isnull=False),
                name="uq_period_order_branch_day",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session", "order_index"],
                condition=Q(branch__isnull=True, day_of_week__isnull=True),
                name="uq_period_order_shared_everyday",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session", "branch", "order_index"],
                condition=Q(branch__isnull=False, day_of_week__isnull=True),
                name="uq_period_order_branch_everyday",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session", "day_of_week", "order_index"],
                condition=Q(branch__isnull=True, day_of_week__isnull=False),
                name="uq_period_order_shared_day",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "session", "branch", "day_of_week", "order_index"],
            ),
        ]
        ordering = ["day_of_week", "order_index"]

    def __str__(self):
        return self.label


class ClassTimetable(_Owned):
    """One class's publication state for one school year.

    **Not in FRD v3.0.1, and the gap this closes is the sharpest one in it.**
    FR-017 publishes a class timetable, "sets a state and stamps a time" and
    audits it against ``SchoolClass`` - but section 6 declares five timetable
    models and only ``Exam`` carries ``status`` and ``published_at``. As
    specified, the publish endpoint has nothing to write to.

    The state cannot live on ``SchoolClass``: that is ``vs_academics``' model,
    and a class
    outlives a session while its timetable does not - the same class is a draft
    again next year.

    An absent row means "Not started", which is the design's third status and is
    otherwise unrepresentable. This carries publication state only; section
    6.11's argument that a grid must have no *branch* column is untouched.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="class_timetables",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="class_timetables",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="timetables",
    )
    status = models.CharField(
        max_length=12, choices=PublishState.choices,
        default=PublishState.DRAFT, db_index=True,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["session", "school_class"],
                name="uq_class_timetable_session_class",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "session", "status"])]
        ordering = ["school_class"]

    def __str__(self):
        return f"{self.school_class_id}@{self.session_id}"


class TimetableSlot(_Owned):
    """One cell of one class's weekly grid.

    This class, on this day, in this period, is taught this subject by this
    person in this room.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="timetable_slots",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="timetable_slots",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="timetable_slots",
    )
    day_of_week = models.PositiveSmallIntegerField(choices=DayOfWeek.choices)
    period = models.ForeignKey(
        Period, on_delete=models.PROTECT, related_name="slots",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT,
        related_name="timetable_slots",
    )
    #: Nullable because a school builds a grid over several sittings and fills
    #: the subjects before it fills the people - and because duplicating another
    #: class's week without its teachers is a supported path that must save.
    #: The publish gate is what refuses an unstaffed grid, not the write.
    #: PROTECT, so a user holding slots cannot be deleted out from under them.
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, null=True, blank=True,
        related_name="slots",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Exactly one, and there must not be a second.
            #
            # This one says a class has at most one thing happening in a given
            # period on a given day, which is a fact about the class and never
            # something a school wants to override.
            #
            # The two a reviewer will ask for - over (session, teacher, day,
            # period) and (session, room, day, period) - would break the
            # product. A clash must be STORABLE to be shown: a school that
            # discovers at Period 5 that Mrs Adeyemi is already booked needs to
            # save the grid, see both cells in red, and resolve them when the
            # head of department is back on Monday. The refusal belongs at
            # publication, which is the one moment the school asserts the grid
            # is finished. ``tests/test_clashes.py`` asserts the absence of both
            # against the migration state, so adding one is a failing test
            # rather than a silent product change.
            models.UniqueConstraint(
                fields=["session", "school_class", "day_of_week", "period"],
                name="uq_slot_class_day_period",
            ),
        ]
        indexes = [
            # The three reads this half actually makes. Note what the first two
            # do not carry: a branch. Both clash queries deliberately span the
            # tenant, and an index encouraging a narrowed one would be actively
            # harmful - see services/clashes.py.
            models.Index(fields=["tenant", "session", "teacher", "day_of_week"]),
            models.Index(fields=["tenant", "session", "room", "day_of_week"]),
            models.Index(fields=["tenant", "session", "school_class"]),
        ]
        ordering = ["day_of_week", "period"]

    def __str__(self):
        return f"{self.school_class_id} {self.day_of_week} {self.period_id}"


class Exam(_Owned):
    """An examination sitting inside an exam period on the calendar.

    Anchored to the calendar rather than floating beside it: the school says in
    its calendar that it is examining in the first week of December, and the
    schedule hangs off that statement rather than repeating it. The session, the
    dates and the branch scope are read from the event and none is copied.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="exams",
    )
    calendar_event = models.ForeignKey(
        CalendarEvent, on_delete=models.PROTECT, related_name="exams",
    )
    name = models.CharField(max_length=120)
    status = models.CharField(
        max_length=12, choices=PublishState.choices,
        default=PublishState.DRAFT, db_index=True,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "calendar_event"])]
        ordering = ["calendar_event", "name"]

    def __str__(self):
        return self.name


class ExamSlot(_Owned):
    """One paper: which class sits which subject, when, where, supervised by whom."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="exam_slots",
    )
    #: CASCADE, and the second of the two in this module: a paper has no
    #: meaning without its exam.
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="slots")
    #: There are no students, so the unit that sits an examination is a class.
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="exam_slots",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT, related_name="exam_slots",
    )
    exam_date = models.DateField()
    #: Deliberately not a Period foreign key: an examination day does not run
    #: the ordinary bell schedule, and forcing one would make a two-hour paper
    #: occupy three rows of a grid it has nothing to do with.
    sitting = models.CharField(max_length=12, choices=Sitting.choices)
    #: A school that publishes exact times has them; one that publishes only
    #: morning and afternoon does not, and is not made to invent them.
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, null=True, blank=True,
        related_name="exam_slots",
    )
    invigilator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Note which exam clash is a constraint and which is a warning: the
            # split is the opposite way round from the class timetable and it is
            # deliberate. A class sitting two papers in one sitting is
            # physically impossible and a school never means it. A room used
            # twice, and an invigilator in two rooms, are things a school
            # legitimately does - two classes really do sit in the Main Hall
            # together, and one person really does float between adjacent rooms
            # - so those warn.
            models.UniqueConstraint(
                fields=["exam", "school_class", "exam_date", "sitting"],
                name="uq_examslot_class_sitting",
            ),
            models.CheckConstraint(
                condition=(
                    Q(start_time__isnull=True)
                    | Q(end_time__isnull=True)
                    | Q(end_time__gt=F("start_time"))
                ),
                name="ck_examslot_times",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "exam", "exam_date"]),
            models.Index(fields=["tenant", "exam", "room", "exam_date"]),
        ]
        ordering = ["exam_date", "sitting"]

    def __str__(self):
        return f"{self.school_class_id} {self.subject_id} {self.exam_date}"
