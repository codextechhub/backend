"""Put registers behind the attendance screens, so every state has a row to look at.

A screen cannot be checked against an endpoint that returns nothing, and the
attendance states cannot all be reached by hand in an afternoon. This command
builds them in the two live schools the sibling seeders leave behind:

    sunrise-academy   One branch. The recede case: no branch switcher, and the
                      branch column of every table reads Level.
    lagoon-view       Two branches. The branch-filter case: a second branch with
                      its own classes, registers and its own closed day.

Both are live, which matters: every attendance route answers 403
TENANT_NOT_LIVE to a school still onboarding.

**What each school is given**, on the last open days up to today at the school:

* a class (C1) whose teacher took eight registers in a row: an absent child
  whose primary guardian has an email (notice SENT), an absent child whose
  household has none (NO_EMAIL), an Excused child (Sick, with a note), a Late
  child with an arrival time, one child absent often enough to sit below the
  chronic threshold, and a register today in which an absent child was
  changed to Late the same day (a MarkChange and a correction notice);
* a class (C2) whose register an administrator took on the class teacher's
  behalf, with a suspended pupil locked on it, and which has not been taken
  today but whose teacher has been reminded;
* a class (C3) not taken today and not reminded;
* at a school with two branches, a second branch with a class that took today's
  register and one that did not;
* a closed day (a school-wide closure) and, at two branches, a closure of the
  second branch alone;
* three corrections on closed days in C1: one waiting, one approved, one
  rejected;
* one retired reason code;
* at the school with two branches, lesson marking switched on for C1's level
  and C1 given a published timetable (two lessons a teaching day, the class
  teachers of C1 and C2 teaching them, a room, and a bell schedule if the school
  has none): C1's first lesson was taken on its last two school days and today,
  with one child who was in school in the morning absent from it (a missed
  lesson), and today's second lesson is left unmarked;
* a staff register: today's, with one person Present, one Late (with the time
  they arrived), one Absent and the rest not yet marked, so it is neither new nor
  finished; a submitted one for the school's latest open day before today
  (Present, one Late, one Absent); somebody on approved leave covering today, whom
  the register fills in and will not let anyone mark over; somebody who has
  resigned with a last working day still to come, listed with a note saying so;
  and, at the school with two branches, somebody with no home branch, locked in
  a group of their own. At the school with one branch every person is on the
  one register, including anyone with no branch.

Classes are chosen by size, the largest of each branch first, from the roll
``seed_student_scenarios`` builds; classes outside the cast have taken no
register this term and so read as "not taken" on every day of it.

**Everything goes through the real services.** Marks come from
``marks.set_mark``, ``fill_present`` and ``submit``, lesson marks from the same
three in ``lesson_marks``, corrections from ``corrections.request_correction``
and ``decide``, reminders from ``reminders.remind``, the suspension from
``status.transition``, the lesson-marking switch from
``configuration.write_settings``, the lessons from ``validate_slot`` and the
timetable's publication from ``publish_class_timetable``, the teachers from the
staff module's own invitation path. A state that cannot be reached
honestly fails loudly here instead of being written as a row and believed.
Staff marks come from ``staff_marks``, the colleague who leaves from
``employment.change_status``, and the person with no home branch from the staff
module's own invitation path. The deliberate exceptions are the closure, which is
written as a calendar row exactly as ``seed_timetable_scenarios`` writes its
events (the calendar has no service to call), the bell schedule and the room,
which are written as rows exactly as that seeder writes them, and the approved
leave, which is written as a row exactly as ``seed_staff_scenarios`` writes its
own: approving honestly needs a second person to vote through the workflow
engine, and nothing in the staff module's services approves a request.

**Idempotent.** Every step looks before it acts: a register already submitted,
a teacher already invited, a pupil already enrolled, a correction already filed
for that mark, a reminder already sent, a code already retired and an event
already named are left alone, and a step that finds its work done prints
nothing. Run twice on one day, the second run creates nothing and prints
``Created this run: 0``. The cast and the days are read relative to today at the
school, so a run on a later day tops the school up with the newer days and
leaves the older ones as they were; the closures keep the dates they were
given. Nothing is wrapped in one transaction, so each step commits on its own,
the notices it triggers go out in order, and a run that fails part way is
simply run again.

**Different people.** A scenario that needs two parties never leans on the whole-school
administrator being somebody other than a class teacher: the staff seeder may
have made that very account the class teacher of C1 or C2. The administrator who
takes C2's register on the teacher's behalf, the one who decides C1's
corrections and the one who reminds C2's teacher are each chosen as a holder of
the permission the action needs who is not the person on the other side of it
(the whole-school administrator first, then the branch administrator, then
anyone else holding it). A school with nobody else to do it fails with a
``CommandError`` that names what is missing.

**Email.** The run forces Django's in-memory mail backend, so no seeded
address is ever mailed and the console is not flooded with message bodies. The
in-app notifications the notices produce are written as usual.

    python manage.py seed_attendance_scenarios
    python manage.py seed_attendance_scenarios --only sunrise-academy

Run ``seed_all_permissions``, ``seed_onboarding_scenarios``,
``seed_academic_scenarios``, ``seed_student_scenarios`` and
``seed_staff_scenarios`` first. Never run against production.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone

from schools.vs_academics.models import AcademicSession, SchoolClass, SessionStatus, Subject
from schools.vs_calendar.models import (
    CalendarEvent,
    ClassTimetable,
    EventType,
    Period,
    PeriodType,
    PublishState,
    Room,
    RoomType,
    TimetableSlot,
)
from schools.vs_calendar.services.calendar import term_of
from schools.vs_calendar.services.calendar_rules import read_teaching_days
from schools.vs_calendar.services.publishing import publish_class_timetable
from schools.vs_calendar.services.timetable import validate_slot
from schools.vs_staff.constants import EmploymentStatus, LeaveStatus, OFF_ROLL_STATUSES
from schools.vs_staff.management.commands.seed_staff_scenarios import (
    Command as StaffSeeder,
)
from schools.vs_staff.models import LeaveRequest, StaffProfile
from schools.vs_staff.services import employment, teaching
from schools.vs_staff.services import leave as leave_service
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import Student
from schools.vs_students.services import enrolment as enrolment_service
from schools.vs_students.services.status import transition
from vs_audit.models import AuditEvent
from vs_config.clock import branch_today, tenant_today
from vs_rbac.evaluator import resolve_users_with_permission
from vs_rbac.scoping import WHOLE_TENANT

from schools.vs_attendance.constants import (
    PERM_CORRECTION_APPROVE,
    PERM_REGISTER_MARK_ANY,
    PERM_REGISTER_REMIND,
    ChangeVia,
    CorrectionStatus,
    Mark,
    NoticeState,
)
from schools.vs_attendance.models import (
    ClassRegister,
    CorrectionRequest,
    LessonRegister,
    MarkChange,
    ReasonCode,
    RegisterReminder,
    StaffDay,
    StaffMark,
    StudentMark,
)
from schools.vs_attendance.services import (
    configuration,
    corrections,
    lesson_marks,
    lessons,
    marks,
    reasons,
    reminders,
    staff_marks,
    staff_register,
)
from schools.vs_attendance.services.days import DayCalendar
from schools.vs_attendance.services.registers import load_context
from schools.vs_attendance.services.report import build_report
from schools.vs_students.services.roster import roster_as_at
from schools.vs_attendance.services.rules import read_attendance_rules

CAST = ("sunrise-academy", "lagoon-view")

#: Registers C1's teacher takes, newest first, and the other classes' share.
C1_DAYS = 8
C2_DAYS = 4
C3_DAYS = (1, 2)

#: A class needs this many children on the roll to carry a scenario.
MIN_ROSTER = 5

#: Who teaches the cast classes that have nobody, in cast order.
TEACHERS = (
    ("Kunle", "Adebayo", "MALE"),
    ("Amaka", "Nwosu", "FEMALE"),
    ("Bisi", "Olanrewaju", "FEMALE"),
    ("Emeka", "Okonkwo", "MALE"),
)

#: The household with no email address: the child the NO_EMAIL notice is for.
NO_EMAIL_CHILD = ("Temitope", "Adegoke")

#: The small bell schedule a school with none is given: ``(label, start, end, type)``.
BELLS = (
    ("Period 1", dt.time(8, 0), dt.time(8, 45), PeriodType.LESSON),
    ("Period 2", dt.time(8, 45), dt.time(9, 30), PeriodType.LESSON),
    ("Break", dt.time(9, 30), dt.time(10, 0), PeriodType.BREAK),
    ("Period 3", dt.time(10, 0), dt.time(10, 45), PeriodType.LESSON),
)
LESSON_ROOM = "Lesson Room 1"

#: The colleagues the staff register needs beyond the class teachers, in the
#: order they are invited: ``(first, last, gender, job title, staff number)``.
#: The first is on leave, the second is leaving.
STAFF_EXTRAS = (
    ("Ifeoma", "Adewale", "FEMALE", "Bursar", 960),
    ("Segun", "Balogun", "MALE", "Teacher", 961),
)

#: The registrar a school with two branches needs when nobody there is based nowhere.
REGISTRAR = ("Adaeze", "Nwankwo", "FEMALE", "Registrar", 962)

#: Days from today to a leaving colleague's last working day, and the length of the leave.
LEAVING_IN_DAYS = 14
LEAVE_BEFORE, LEAVE_AFTER = 1, 3

CLOSURE_NAME = "Staff Development Day"
BRANCH_CLOSURE_NAME = "Second Branch Open Day"
RETIRED_REASON = "Travel"


class Cast:
    """One class in the scenario and everything the steps need to know about it."""

    def __init__(self, label, school_class, home):
        self.label = label
        self.school_class = school_class
        self.home = home
        self.teacher = None
        self.past = []

    @property
    def name(self):
        return self.school_class.name

    @property
    def teacher_user(self):
        return self.school_class.class_teacher.user


class Command(BaseCommand):
    help = "Seed class registers, notices, reminders and corrections for two live schools."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="One school slug from the cast.")

    def handle(self, *args, **options):
        only = options.get("only")
        if only and only not in CAST:
            raise CommandError(
                f"{only!r} is not in the cast. Pick one of: {', '.join(CAST)}",
            )
        self.created = 0
        logins = []
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            for slug in ([only] if only else list(CAST)):
                logins.extend(self._seed(slug))
        self.stdout.write(self.style.SUCCESS(f"\nCreated this run: {self.created}"))
        self.stdout.write(
            "\nLogins. Each school is reached at <slug>.localhost:5174. Admin "
            "accounts take the dev fixture password (DEFAULT_PASSWORD in "
            "schools/vs_schools/dev/fixtures.py); teachers take the staff "
            "seeder's (SEED_PASSWORD in seed_staff_scenarios).",
        )
        for line in logins:
            self.stdout.write(f"  {line}")

    # ── plumbing ──────────────────────────────────────────────────────────

    def _made(self, text):
        self.created += 1
        self.stdout.write(f"  + {text}")

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios, "
                f"seed_academic_scenarios and seed_student_scenarios first.",
            )
        return school

    def _user(self, tenant, email):
        from vs_user.models import User

        user = User.objects.filter(tenant=tenant, email=email).first()
        if user is None:
            raise CommandError(
                f"{tenant.slug} has no account {email}. Run "
                f"seed_onboarding_scenarios first.",
            )
        return user

    def _holder_besides(self, tenant, admin, permission, branch, *, besides, doing):
        """A user holding *permission* at *branch* who is not *besides*.

        The whole-school administrator is preferred, then the branch
        administrator the dev school is built with, then the holder with the
        lowest id, so the choice is the same on every run. Raises
        ``CommandError`` when nobody qualifies: the scenario needs two people,
        and one cannot be invented here.
        """
        holders = list(
            resolve_users_with_permission(tenant, branch, permission)
            .exclude(pk=besides.pk).order_by("pk"),
        )
        for user in holders:
            if user.pk == admin.pk:
                return user
        for user in holders:
            if user.email.startswith("branch.admin@"):
                return user
        if holders:
            return holders[0]
        raise CommandError(
            f"{tenant.slug} has nobody besides {besides.full_name} who holds "
            f"{permission} at {branch.name}, so {doing} cannot be seeded. Give "
            f"another administrator a role with that permission.",
        )

    # ── the school ────────────────────────────────────────────────────────

    def _seed(self, slug):
        school = self._school(slug)
        tenant = school.tenant
        admin = self._user(tenant, f"admin@{slug}.example.com")
        branches = list(tenant.branches.order_by("-is_main", "code"))
        year = AcademicSession.objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        if year is None:
            raise CommandError(f"{slug} has no active year. Run seed_academic_scenarios.")
        today = tenant_today(tenant)
        if term_of(year, today) is None:
            raise CommandError(
                f"{today} is outside every term of {year}, so there are no school "
                f"days to take a register on.",
            )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{school.name} ({slug}), {len(branches)} "
            f"branch{'es' if len(branches) > 1 else ''}, today is {today}",
        ))
        cast = self._pick_classes(tenant, year, branches, today)
        self._teach(tenant, admin, slug, cast)
        reasons.ensure_default_reason_codes(tenant)
        self._closures(tenant, year, branches, cast, today)
        calendar = DayCalendar(tenant, [year], year.start_date, today)
        for entry in cast:
            entry.past = self._open_days(tenant, calendar, entry, today)
        c1 = cast[0]
        no_email = self._no_email_child(tenant, admin, c1)
        suspended = self._suspension(tenant, admin, cast[1], today)

        self._registers(tenant, admin, cast, today, no_email, suspended)
        self._same_day_change(tenant, c1, today)
        self._corrections(tenant, admin, c1, no_email)
        self._reminders(tenant, admin, cast, today)
        self._retire_a_reason(tenant, admin)
        if len(branches) > 1:
            self._lessons(tenant, admin, year, cast, today)
        self._staff_registers(tenant, admin, slug, branches, today)
        self._summary(tenant, admin, cast, today, branches)

        branch_admin = f"branch.admin@{slug}.example.com"
        lines = [f"{slug}: admin@{slug}.example.com (whole school)"]
        lines.append(f"{slug}: {branch_admin} (main branch)")
        for entry in cast:
            if entry.school_class.class_teacher_id:
                lines.append(
                    f"{slug}: {entry.teacher_user.email} "
                    f"(teaches {entry.name}, {entry.home.name})",
                )
        return lines

    # ── the cast ──────────────────────────────────────────────────────────

    def _pick_classes(self, tenant, year, branches, today):
        """The classes that carry the scenarios: three in the main branch, two in the second.

        The largest classes of each branch, by everyone ever placed in them, so
        a suspension later does not reorder the cast.
        """
        several = len(branches) > 1
        rows = []
        for school_class in SchoolClass.objects.filter(
            tenant=tenant, session=year, is_active=True,
        ).select_related("branch", "class_teacher", "class_teacher__user", "level"):
            home = school_class.branch or (None if several else branches[0])
            if home is None:
                continue
            size = len(roster_as_at(tenant, school_class, today))
            if size >= MIN_ROSTER:
                rows.append((home, size, school_class))
        rows.sort(key=lambda r: (-r[1], r[2].name, r[2].pk))

        def take(branch, limit):
            return [r for r in rows if r[0].pk == branch.pk][:limit]

        picked = take(branches[0], 3)
        if len(picked) < 3:
            raise CommandError(
                f"{tenant.slug} needs three classes with at least {MIN_ROSTER} "
                f"children in the main branch. Run seed_student_scenarios.",
            )
        if several:
            picked += take(branches[1], 2)
        return [
            Cast(f"C{index}", school_class, home)
            for index, (home, _size, school_class) in enumerate(picked, start=1)
        ]

    def _teach(self, tenant, admin, slug, cast):
        """Give every cast class a class teacher, inviting the ones it lacks.

        The invitation is the staff seeder's own, so a teacher is a real
        account that accepted it, holding the Teacher role at their branch.
        """
        staff_seeder = StaffSeeder()
        staff_seeder.stdout = self.stdout
        for index, entry in enumerate(cast):
            if entry.school_class.class_teacher_id is not None:
                continue
            if entry.label == "C5":
                # The last class keeps no teacher: its hub row has no one to remind.
                continue
            first, last, gender = TEACHERS[index % len(TEACHERS)]
            email = f"{first}.{last}@{slug}.example.com".lower()
            profile = StaffProfile.objects.filter(tenant=tenant, user__email=email).first()
            if profile is None:
                profile = staff_seeder._invite(
                    tenant=tenant, actor=admin, first=first, last=last, gender=gender,
                    job_title="Class Teacher", branch=entry.home, email=email,
                    number=900 + index, slug=slug,
                )
                if profile is None:
                    raise CommandError(f"Could not invite {email}.")
                staff_seeder._drive_to(profile, EmploymentStatus.ACTIVE, admin)
                self._made(f"teacher {first} {last} ({email})")
            teaching.set_class_teacher(
                entry.school_class, profile, actor=admin, tenant=tenant,
            )
            entry.school_class.refresh_from_db()
            self._made(f"class teacher of {entry.name}: {first} {last}")

    # ── the days ──────────────────────────────────────────────────────────

    def _closures(self, tenant, year, branches, cast, today):
        """A school-wide closure and, at two branches, one for the second branch alone.

        Dated on open days from the last few weeks the first time they are
        made, and left where they are after.
        """
        c1 = cast[0]
        calendar = DayCalendar(tenant, [year], year.start_date, today)
        recent = self._open_days(tenant, calendar, c1, today, limit=10)
        wanted = [(CLOSURE_NAME, None, 4, "The school was closed for staff training.")]
        if len(branches) > 1:
            wanted.append((
                BRANCH_CLOSURE_NAME, branches[1], 7,
                f"{branches[1].name} only.",
            ))
        for name, branch, back, text in wanted:
            if CalendarEvent.all_objects.filter(
                tenant=tenant, session=year, name=name,
            ).exists():
                continue
            if len(recent) <= back:
                raise CommandError(f"{tenant.slug} has too few school days to close one.")
            day = recent[back]
            CalendarEvent.all_objects.create(
                tenant=tenant, session=year, name=name, branch=branch,
                event_type=EventType.SCHOOL_EVENT, start_date=day, end_date=day,
                closes_school=True, description=text,
            )
            self._made(
                f"closed day {name} on {day}"
                f"{f' ({branch.name} only)' if branch else ''}",
            )

    def _open_days(self, tenant, calendar, entry, today, *, limit=C1_DAYS):
        """The *limit* most recent days before today the class owed a register, newest first.

        A day before anyone was placed in the class is left out: it has no
        roster to register.
        """
        placed = self._first_placement(entry.school_class, today)
        days, day = [], today - dt.timedelta(days=1)
        while len(days) < limit and day >= placed:
            if calendar.status(entry.school_class, entry.home, day).is_open:
                days.append(day)
            day -= dt.timedelta(days=1)
        return days

    @staticmethod
    def _first_placement(school_class, default):
        """The day the first child was placed in the class, superseded placements included."""
        return min(
            (row.effective_date for row in school_class.enrolments.all()), default=default,
        )

    # ── people ────────────────────────────────────────────────────────────

    def _pupils(self, tenant, entry, today, *, leave_out=()):
        """The unsuspended children on the class's roll today, oldest record first."""
        skip = {student.pk for student in leave_out}
        return [
            e.student for e in sorted(
                roster_as_at(tenant, entry.school_class, today), key=lambda e: e.student.pk,
            )
            if not e.suspended and e.student.pk not in skip
        ]

    def _no_email_child(self, tenant, admin, c1):
        """A child in C1 whose household gave no email address."""
        first, last = NO_EMAIL_CHILD
        child = Student.all_objects.filter(
            tenant=tenant, first_name=first, last_name=last,
        ).first()
        if child is not None:
            return child
        placed = self._first_placement(c1.school_class, dt.date.max)
        child = enrolment_service.enrol(
            tenant=tenant, actor=admin, branch=c1.home,
            data={
                "first_name": first, "middle_name": "", "last_name": last,
                "date_of_birth": dt.date(2012, 5, 4),
                "gender": Gender.MALE, "nationality": "Nigerian",
                "state_of_origin": "Ogun", "address": "4 Isale Road, Lagos",
                "phone": "", "email": "", "previous_school": "",
                "blood_group": "", "allergies": "", "conditions": "",
                "emergency_contact_name": f"Mrs. {last}",
                "emergency_contact_phone": "08035550111",
                "student_number": "", "enrolment_date": placed,
            },
            guardian_rows=[{
                "full_name": f"Mrs. B. {last}", "phone": "08065550111", "email": "",
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
            school_class=c1.school_class, allow_over_capacity=True,
            confirm_duplicate=True,
        )
        self._made(f"pupil {child.full_name} in {c1.name}, household with no email")
        return child

    def _suspension(self, tenant, admin, c2, today):
        """A suspended pupil in C2: the one already there, else the last on its roll.

        Suspended from the third register back, so the earlier registers show
        the pupil marked and the later ones show the row locked.
        """
        for entry in roster_as_at(tenant, c2.school_class, today):
            if entry.suspended:
                return entry.student
        pupils = self._pupils(tenant, c2, today)
        child = pupils[-1]
        since = c2.past[2] if len(c2.past) > 2 else today
        transition(
            child, StudentStatus.SUSPENDED, actor=admin,
            reason="Repeated absence without notice.", effective_date=since,
        )
        self._made(f"suspension of {child.full_name} ({c2.name}) from {since}")
        return child

    # ── registers ─────────────────────────────────────────────────────────

    def _take(self, tenant, entry, day, taker, exceptions=None):
        """Take one register through the services. Returns whether it was taken now.

        A register already submitted is left as it is. One only partly marked
        by an earlier run that failed is finished.
        """
        existing = ClassRegister.objects.filter(
            tenant=tenant, school_class=entry.school_class, date=day,
        ).first()
        if existing is not None and existing.is_submitted:
            return False
        for student_id, spec in (exceptions or {}).items():
            ctx = load_context(tenant, taker, entry.school_class.pk, day)
            marks.set_mark(ctx, taker, student_id, **spec)
        marks.fill_present(load_context(tenant, taker, entry.school_class.pk, day), taker)
        marks.submit(load_context(tenant, taker, entry.school_class.pk, day), taker)
        self._made(
            f"register {entry.name} {day} taken by {taker.full_name}"
            f"{' on the class teacher behalf' if self._behalf(entry, taker) else ''}",
        )
        return True

    @staticmethod
    def _behalf(entry, taker):
        teacher = entry.school_class.class_teacher
        return teacher is not None and teacher.user_id != taker.pk

    def _registers(self, tenant, admin, cast, today, no_email, suspended):
        reason = {
            name: ReasonCode.objects.get(tenant=tenant, name=name)
            for name in ("Sick", "Family matter", "Other")
        }
        absent = {"mark": Mark.ABSENT}
        sick = {
            "mark": Mark.EXCUSED, "reason_code_id": reason["Sick"].pk,
            "note": "Fever, seen by the school nurse.",
        }
        late = {"mark": Mark.LATE, "arrived_at": dt.time(8, 40)}

        c1 = cast[0]
        s1, s2, s3, s5 = self._pupils(tenant, c1, today, leave_out=[no_email])[:4]
        c1_days = {
            0: {s1.pk: absent, s2.pk: sick, s3.pk: late, no_email.pk: absent},
            2: {s1.pk: absent},
            3: {s1.pk: absent, no_email.pk: absent},
        }
        teacher = c1.teacher_user
        for index, day in enumerate(c1.past[:C1_DAYS]):
            self._take(tenant, c1, day, teacher, c1_days.get(index))

        if len(cast) > 1:
            c2 = cast[1]
            behalf = self._holder_besides(
                tenant, admin, PERM_REGISTER_MARK_ANY, c2.home, besides=c2.teacher_user,
                doing="a register taken on the class teacher's behalf",
            )
            for index, day in enumerate(c2.past[:C2_DAYS]):
                taker = behalf if index == 0 else c2.teacher_user
                self._take(tenant, c2, day, taker)

        if len(cast) > 2:
            c3 = cast[2]
            for index in C3_DAYS:
                if index < len(c3.past):
                    self._take(tenant, c3, c3.past[index], c3.teacher_user)

        if len(cast) > 3:
            c4 = cast[3]
            kids = self._pupils(tenant, c4, today)
            for index, day in enumerate(c4.past[:2]):
                spec = {kids[0].pk: absent} if index == 0 else None
                self._take(tenant, c4, day, c4.teacher_user, spec)

        # Today. C1 and the second branch's C4 took theirs; C2, C3 and C5 did not.
        if self._owed_today(tenant, c1, today):
            self._take(tenant, c1, today, teacher, {s1.pk: absent, s5.pk: absent})
        if len(cast) > 3 and self._owed_today(tenant, cast[3], today):
            self._take(tenant, cast[3], today, cast[3].teacher_user)

    def _owed_today(self, tenant, entry, today):
        if branch_today(tenant, entry.home) != today:
            return False
        day = DayCalendar(tenant, [entry.school_class.session], today, today)
        if day.status(entry.school_class, entry.home, today).is_open:
            return True
        self.stdout.write(
            f"  today ({today}) is not a school day for {entry.name}; "
            f"its today states are skipped.",
        )
        return False

    def _same_day_change(self, tenant, c1, today):
        """C1's first absent child, noticed today, arrives late: Absent becomes Late.

        The register is already submitted and today's window is open, so the
        teacher changes the row in place. That writes a MarkChange and sends
        the correction notice to the guardian who received the absence notice.
        """
        register = ClassRegister.objects.filter(
            tenant=tenant, school_class=c1.school_class, date=today,
        ).first()
        if register is None:
            return
        mark = (
            StudentMark.objects.filter(register=register, mark=Mark.ABSENT)
            .select_related("student").order_by("student_id").first()
        )
        already = MarkChange.objects.filter(
            mark__register=register, via=ChangeVia.SAME_DAY, from_mark=Mark.ABSENT,
        ).exists()
        if mark is None or already:
            return
        teacher = c1.teacher_user
        marks.set_mark(
            load_context(tenant, teacher, c1.school_class.pk, today), teacher,
            mark.student_id, mark=Mark.LATE, arrived_at=dt.time(9, 10),
        )
        self._made(f"same-day change in {c1.name}: {mark.student.full_name} Absent to Late")

    # ── corrections, reminders, reasons ───────────────────────────────────

    def _corrections(self, tenant, admin, c1, no_email):
        """Three corrections on C1's closed days: one waiting, one approved, one rejected.

        The days are counted back from today, so on a later day they land on
        different dates. Each correction is therefore recognised by its note
        before its day and pupil are worked out: one already filed is kept, and
        only one never filed is looked for on today's days.
        """
        if len(c1.past) < 4:
            return
        teacher = c1.teacher_user
        sick = ReasonCode.objects.get(tenant=tenant, name="Sick")
        family = ReasonCode.objects.get(tenant=tenant, name="Family matter")
        other = ReasonCode.objects.get(tenant=tenant, name="Other")
        first_absent = self._first_absent(tenant, c1, c1.past[2])
        late_child = self._late_child(tenant, c1, c1.past[0])
        plan = [
            (c1.past[3], no_email.pk, Mark.EXCUSED, family, "Away for a family funeral.",
             None, None),
            (c1.past[2], first_absent, Mark.EXCUSED, sick, "Medical report received.",
             True, "Report seen by the office."),
            (c1.past[0], late_child, Mark.PRESENT, other, "Arrived before the bell.",
             False, "The gate log shows 08:40."),
        ]
        for day, student_id, to_mark, code, note, approve, decision_note in plan:
            request = (
                CorrectionRequest.objects.filter(
                    tenant=tenant, mark__register__school_class=c1.school_class, note=note,
                )
                .select_related("mark__student").first()
            )
            mark = request.mark if request is not None else (
                StudentMark.objects.filter(
                    register__school_class=c1.school_class, register__date=day,
                    student_id=student_id,
                ).first()
            )
            if mark is None:
                raise CommandError(f"No mark for pupil {student_id} in {c1.name} on {day}.")
            if request is None:
                ctx = load_context(tenant, teacher, c1.school_class.pk, day)
                request = corrections.request_correction(
                    ctx, teacher, student_id, to_mark=to_mark,
                    reason_code_id=code.pk, note=note,
                )
                self._made(f"correction filed: {mark.student.full_name} on {day}")
            if approve is not None and request.status == CorrectionStatus.PENDING:
                decider = self._holder_besides(
                    tenant, admin, PERM_CORRECTION_APPROVE, c1.home,
                    besides=request.requested_by, doing="a decided correction",
                )
                corrections.decide(
                    tenant, decider, request.pk, approve=approve,
                    decision_note=decision_note,
                )
                self._made(
                    f"correction {'approved' if approve else 'rejected'}: "
                    f"{mark.student.full_name} on {day}",
                )

    @staticmethod
    def _first_absent(tenant, c1, day):
        return StudentMark.objects.filter(
            tenant=tenant, register__school_class=c1.school_class,
            register__date=day, mark__in=[Mark.ABSENT, Mark.EXCUSED],
        ).order_by("student_id").values_list("student_id", flat=True).first()

    @staticmethod
    def _late_child(tenant, c1, day):
        return StudentMark.objects.filter(
            tenant=tenant, register__school_class=c1.school_class,
            register__date=day, mark=Mark.LATE,
        ).order_by("student_id").values_list("student_id", flat=True).first()

    def _reminders(self, tenant, admin, cast, today):
        """Remind C2's teacher about today's register. C3 and C5 are left unreminded."""
        if len(cast) < 2 or not self._owed_today(tenant, cast[1], today):
            return
        c2 = cast[1]
        if RegisterReminder.objects.filter(
            tenant=tenant, school_class=c2.school_class, date=today,
        ).exists():
            return
        sender = self._holder_besides(
            tenant, admin, PERM_REGISTER_REMIND, c2.home, besides=c2.teacher_user,
            doing="a reminder to the class teacher",
        )
        reminders.remind(load_context(tenant, sender, c2.school_class.pk, today), sender)
        self._made(f"reminder to {c2.teacher_user.full_name} for {c2.name} {today}")

    def _retire_a_reason(self, tenant, admin):
        code = ReasonCode.objects.get(tenant=tenant, name=RETIRED_REASON)
        if code.retired_at is not None:
            return
        reasons.retire_reason_code(tenant, admin, code)
        self._made(f"reason code {RETIRED_REASON} retired")

    # ── lessons ───────────────────────────────────────────────────────────

    def _lessons(self, tenant, admin, year, cast, today):
        """Lesson marking for C1's level, a published timetable for C1, and lessons taken.

        C1's class teacher teaches the first lesson of each school day and C2's
        class teacher the second, in the same room. The lessons are validated as
        the timetable validates them and the week is published through the
        publishing service, so a state the timetable would refuse fails here
        loudly. On C1's last two school days, and today when it is a school day,
        its first lesson is taken with one child who was in school in the
        morning marked absent from it; today's second lesson is left unmarked.
        """
        c1, c2 = cast[0], cast[1]
        if c1.school_class.class_teacher_id is None or c2.school_class.class_teacher_id is None:
            return
        self._switch_lessons_on(tenant, admin, c1)
        first, second = self._bell_schedule(tenant, year)
        subjects = list(Subject.all_objects.filter(tenant=tenant).order_by("name")[:2])
        if len(subjects) < 2:
            raise CommandError(f"{tenant.slug} needs two subjects. Run seed_academic_scenarios.")
        room = self._room(tenant, c1)
        self._grid(tenant, admin, year, c1, c2, (first, second), subjects, room)
        self._take_lessons(tenant, c1, today)

    def _switch_lessons_on(self, tenant, admin, c1):
        code = c1.school_class.level.code
        current = list(read_attendance_rules(tenant).lesson_marking_levels)
        if code.lower() in {c.lower() for c in current}:
            return
        configuration.write_settings(
            tenant, admin, {"lesson_marking_levels": current + [code]},
        )
        self._made(f"lesson marking switched on for {c1.school_class.level.name}")

    def _bell_schedule(self, tenant, year):
        """The school's first two teaching periods, creating the small bell schedule if it has none."""
        if not Period.all_objects.filter(tenant=tenant, session=year).exists():
            for order, (label, start, end, kind) in enumerate(BELLS, start=1):
                Period.all_objects.create(
                    tenant=tenant, session=year, branch=None, day_of_week=None,
                    order_index=order, label=label, period_type=kind,
                    start_time=start, end_time=end,
                )
            self._made("bell schedule: Period 1, Period 2, Break, Period 3")
        periods = list(
            Period.all_objects.filter(
                tenant=tenant, session=year, is_active=True, period_type=PeriodType.LESSON,
                branch__isnull=True, day_of_week__isnull=True,
            ).order_by("start_time", "order_index")[:2],
        )
        if len(periods) < 2:
            raise CommandError(
                f"{tenant.slug} needs two teaching periods in its everyday bell schedule "
                f"to carry the lesson scenario.",
            )
        return periods

    def _room(self, tenant, c1):
        room, created = Room.all_objects.get_or_create(
            tenant=tenant, branch=c1.home, name=LESSON_ROOM,
            defaults={"code": "LR-1", "room_type": RoomType.CLASSROOM, "capacity": 40},
        )
        if created:
            self._made(f"room {LESSON_ROOM} at {c1.home.name}")
        return room

    def _grid(self, tenant, admin, year, c1, c2, periods, subjects, room):
        """C1's week: two lessons every teaching day, validated and published."""
        teachers = (c1.teacher_user, c2.teacher_user)
        made = 0
        for day in read_teaching_days(tenant):
            for period, subject, teacher in zip(periods, subjects, teachers):
                if TimetableSlot.all_objects.filter(
                    tenant=tenant, session=year, school_class=c1.school_class,
                    day_of_week=day, period=period,
                ).exists():
                    continue
                validate_slot(
                    tenant, year, school_class=c1.school_class, day_of_week=day,
                    period=period, subject=subject, teacher=teacher, room=room,
                )
                TimetableSlot.all_objects.create(
                    tenant=tenant, session=year, school_class=c1.school_class,
                    day_of_week=day, period=period, subject=subject, teacher=teacher,
                    room=room, created_by=admin,
                )
                made += 1
        if made:
            self._made(f"{made} lessons on {c1.name}'s timetable")
        record = ClassTimetable.all_objects.filter(
            tenant=tenant, session=year, school_class=c1.school_class,
        ).first()
        if record is None or record.status != PublishState.PUBLISHED:
            publish_class_timetable(
                tenant, year, c1.school_class, actor=admin, visible=WHOLE_TENANT,
            )
            self._made(f"{c1.name}'s timetable published")

    def _take_lessons(self, tenant, c1, today):
        """Take C1's lessons on its last two school days and today, as described above."""
        days = [(day, True) for day in c1.past[:2]]
        if self._owed_today(tenant, c1, today):
            days.insert(0, (today, False))
        for day, both in days:
            todays = lessons.day_lessons(
                tenant, [(c1.school_class, c1.home)], {c1.school_class.pk: self._day(
                    tenant, c1, day,
                )}, day, lessons.marking_levels(tenant),
            )
            if not todays:
                continue
            absent = self._present_in_the_morning(tenant, c1, day)
            for index, lesson in enumerate(todays[:2]):
                if index == 1 and not both:
                    continue
                taker = lesson.slot.teacher
                spec = {absent: Mark.ABSENT} if index == 0 and absent is not None else {}
                self._take_lesson(tenant, lesson, day, taker, spec)

    @staticmethod
    def _day(tenant, c1, day):
        return DayCalendar(tenant, [c1.school_class.session], day, day).status(
            c1.school_class, c1.home, day,
        )

    @staticmethod
    def _present_in_the_morning(tenant, c1, day):
        """The first child of C1 whose submitted class register of *day* has them Present or Late."""
        return (
            StudentMark.objects.filter(
                tenant=tenant, register__school_class=c1.school_class, register__date=day,
                register__submitted_at__isnull=False, mark__in=[Mark.PRESENT, Mark.LATE],
            ).order_by("student_id").values_list("student_id", flat=True).first()
        )

    def _take_lesson(self, tenant, lesson, day, taker, marked):
        """Take one lesson register through the services. A register already submitted is left alone."""
        slot = lesson.slot
        existing = LessonRegister.objects.filter(
            tenant=tenant, school_class=lesson.school_class, period_id=slot.period_id,
            date=day,
        ).first()
        if existing is not None and existing.is_submitted:
            return
        for student_id, mark in marked.items():
            ctx = lessons.load_lesson(tenant, taker, slot.pk, day)
            lesson_marks.set_mark(ctx, taker, student_id, mark=mark)
        lesson_marks.fill_present(lessons.load_lesson(tenant, taker, slot.pk, day), taker)
        lesson_marks.submit(lessons.load_lesson(tenant, taker, slot.pk, day), taker)
        self._made(
            f"lesson register {lesson.school_class.name} {slot.period.label} "
            f"{slot.subject.name} {day} taken by {taker.full_name}"
            f"{', one child absent after a morning present' if marked else ''}",
        )

    # ── what now exists ───────────────────────────────────────────────────

    # ── the staff register ────────────────────────────────────────────────

    def _staff_registers(self, tenant, admin, slug, branches, today):
        """Staff to look at, then today's register and the latest finished one."""
        several = len(branches) > 1
        people = self._staff_people(tenant, admin, slug, branches)
        self._someone_leaving(tenant, admin, people["leaving"], today)
        self._someone_on_leave(tenant, admin, people["on_leave"], today)
        if several:
            self._someone_without_a_branch(tenant, admin, slug)
        self._finished_staff_day(tenant, admin, branches[0], today)
        for branch in branches:
            self._staff_day_today(tenant, admin, branch, today)

    def _staff_people(self, tenant, admin, slug, branches):
        """The colleague who goes on leave and the one who is leaving, invited if missing.

        The first is based at the main branch and the second at the second
        branch where there is one, so each branch's register has someone worth
        looking at.
        """
        staff_seeder = StaffSeeder()
        staff_seeder.stdout = self.stdout
        found = {}
        for (first, last, gender, title, number), key, branch in zip(
            STAFF_EXTRAS, ("on_leave", "leaving"), (branches[0], branches[-1]),
        ):
            email = f"{first}.{last}@{slug}.example.com".lower()
            profile = StaffProfile.objects.filter(tenant=tenant, user__email=email).first()
            if profile is None:
                profile = staff_seeder._invite(
                    tenant=tenant, actor=admin, first=first, last=last, gender=gender,
                    job_title=title, branch=branch, email=email, number=number, slug=slug,
                )
                if profile is None:
                    raise CommandError(f"Could not invite {email}.")
                staff_seeder._drive_to(profile, EmploymentStatus.ACTIVE, admin)
                self._made(f"colleague {first} {last} ({email}), {branch.name}")
            found[key] = profile
        return found

    def _someone_leaving(self, tenant, admin, profile, today):
        """Resign the colleague with a last working day still to come."""
        if profile.employment_status in OFF_ROLL_STATUSES:
            return
        last_day = today + dt.timedelta(days=LEAVING_IN_DAYS)
        employment.change_status(
            profile, to_status=EmploymentStatus.RESIGNED, actor=admin,
            reason="Taking up a new post.", last_working_day=last_day,
        )
        self._made(f"{profile.user.full_name} resigned, last working day {last_day}")

    def _someone_on_leave(self, tenant, admin, profile, today):
        """Somebody on approved leave covering today, if nobody on a register is.

        A school whose staff seeder already put a colleague on leave is left as
        it is.
        """
        covering = LeaveRequest.objects.filter(
            tenant=tenant, status=LeaveStatus.APPROVED,
            start_date__lte=today, end_date__gte=today,
            staff__employment_status__in=[
                EmploymentStatus.ACTIVE, EmploymentStatus.INVITED,
                EmploymentStatus.AWAITING_GO_LIVE, EmploymentStatus.SUSPENDED,
            ],
        ).exists()
        if covering:
            return
        start = today - dt.timedelta(days=LEAVE_BEFORE)
        end = today + dt.timedelta(days=LEAVE_AFTER)
        LeaveRequest.objects.create(
            tenant=tenant, staff=profile, leave_type="SICK", start_date=start,
            end_date=end, days=leave_service.count_days(profile, start, end) or 1,
            status=LeaveStatus.APPROVED, decided_at=timezone.now(), requested_by=admin,
            note="Recovering from a minor operation.",
        )
        self._made(f"{profile.user.full_name} on approved leave until {end}")

    def _someone_without_a_branch(self, tenant, admin, slug):
        """A person based nowhere, if a school with several branches has none.

        The whole-school account that opened the school is already such a
        person, so most schools need nothing here.
        """
        if StaffProfile.objects.filter(
            tenant=tenant, branch__isnull=True,
        ).exclude(employment_status__in=list(OFF_ROLL_STATUSES)).exists():
            return
        staff_seeder = StaffSeeder()
        staff_seeder.stdout = self.stdout
        first, last, gender, title, number = REGISTRAR
        email = f"{first}.{last}@{slug}.example.com".lower()
        profile = staff_seeder._invite(
            tenant=tenant, actor=admin, first=first, last=last, gender=gender,
            job_title=title, branch=None, email=email, number=number, slug=slug,
        )
        if profile is None:
            raise CommandError(f"Could not invite {email}.")
        staff_seeder._drive_to(profile, EmploymentStatus.ACTIVE, admin)
        self._made(f"{first} {last}, {title}, with no home branch")

    def _staff_context(self, tenant, admin, branch, day):
        return staff_register.load_context(tenant, admin, branch.pk, day)

    def _open_staff_day(self, tenant, branch, day) -> bool:
        return staff_register.branch_days(tenant, [branch], day)[branch.pk].is_open

    def _finished_staff_day(self, tenant, admin, branch, today):
        """The latest open day before today, everyone Present but one Late and one Absent, submitted."""
        day = today - dt.timedelta(days=1)
        floor = today - dt.timedelta(days=21)
        while day >= floor and not self._open_staff_day(tenant, branch, day):
            day -= dt.timedelta(days=1)
        if day < floor:
            return
        if StaffDay.objects.filter(
            tenant=tenant, branch=branch, date=day, submitted_at__isnull=False,
        ).exists():
            return
        if not self._has_staff_marks(tenant, branch, day):
            self._mark_a_few(tenant, admin, branch, day, late=1, absent=1)
        ctx = self._staff_context(tenant, admin, branch, day)
        staff_marks.fill_present(ctx, admin)
        staff_marks.submit(self._staff_context(tenant, admin, branch, day), admin)
        self._made(f"staff register {branch.name} {day} submitted")

    def _staff_day_today(self, tenant, admin, branch, today):
        """Today: one Present, one Late, one Absent, and the rest left for the screen."""
        if branch_today(tenant, branch) != today:
            return
        if not self._open_staff_day(tenant, branch, today):
            self.stdout.write(
                f"  today ({today}) is not a staff day at {branch.name}; "
                f"its staff register is skipped.",
            )
            return
        if StaffDay.objects.filter(
            tenant=tenant, branch=branch, date=today, submitted_at__isnull=False,
        ).exists():
            return
        if not self._has_staff_marks(tenant, branch, today):
            self._mark_a_few(tenant, admin, branch, today, present=1, late=1, absent=1)

    @staticmethod
    def _has_staff_marks(tenant, branch, day) -> bool:
        return StaffMark.objects.filter(
            tenant=tenant, staff_day__branch=branch, date=day,
        ).exists()

    def _mark_a_few(self, tenant, admin, branch, day, *, present=0, late=0, absent=0):
        """Mark the first unmarked people on the register: Present, then Late, then Absent.

        A person already marked is left alone, so a second run marks nobody.
        """
        plan = [(Mark.PRESENT, {})] * present
        plan += [(Mark.LATE, {"arrived_at": dt.time(8, 25)})] * late
        plan += [(Mark.ABSENT, {"note": "Did not report for duty."})] * absent
        ctx = self._staff_context(tenant, admin, branch, day)
        free = [e for e in ctx.entries if e.lock is None and e.mark is None]
        for entry, (kind, extra) in zip(free, plan):
            staff_marks.set_mark(
                self._staff_context(tenant, admin, branch, day), admin, entry.staff.pk,
                mark=kind, **extra,
            )
            self._made(f"staff mark {entry.name} {Mark(kind).label} on {day}")

    def _staff_summary(self, tenant, today):
        marks_ = StaffMark.objects.filter(tenant=tenant)
        submitted = StaffDay.objects.filter(tenant=tenant, submitted_at__isnull=False).count()
        self.stdout.write(
            f"    staff registers: {StaffDay.objects.filter(tenant=tenant).count()} "
            f"({submitted} submitted); marks: "
            + ", ".join(f"{Mark(code).label.lower()} {marks_.filter(mark=code).count()}"
                        for code in (Mark.PRESENT, Mark.LATE, Mark.ABSENT)),
        )
        on_leave = LeaveRequest.objects.filter(
            tenant=tenant, status=LeaveStatus.APPROVED,
            start_date__lte=today, end_date__gte=today,
        ).select_related("staff__user")
        self.stdout.write(
            "    on approved leave today: "
            + (", ".join(f"{l.staff.user.full_name} until {l.end_date}" for l in on_leave) or "none"),
        )
        leaving = StaffProfile.objects.filter(
            tenant=tenant, exit_date__gte=today,
        ).select_related("user")
        self.stdout.write(
            "    leaving: "
            + (", ".join(f"{p.user.full_name} on {p.exit_date}" for p in leaving) or "none"),
        )
        unhomed = StaffProfile.objects.filter(
            tenant=tenant, branch__isnull=True,
        ).exclude(employment_status__in=list(OFF_ROLL_STATUSES)).select_related("user")
        self.stdout.write(
            "    with no home branch: "
            + (", ".join(p.user.full_name for p in unhomed) or "none"),
        )

    def _summary(self, tenant, admin, cast, today, branches):
        several = len(branches) > 1
        self.stdout.write("\n  What exists")
        for entry in cast:
            taken = ClassRegister.objects.filter(
                tenant=tenant, school_class=entry.school_class, submitted_at__isnull=False,
            ).count()
            teacher = (
                entry.teacher_user.full_name if entry.school_class.class_teacher_id else "nobody"
            )
            where = f", {entry.home.name}" if several else ""
            self.stdout.write(
                f"    {entry.label} {entry.name}{where}: led by {teacher}, "
                f"{taken} registers submitted",
            )
        states = StudentMark.objects.filter(
            tenant=tenant, register__school_class__in=[c.school_class for c in cast],
        )
        for label, notice in (
            ("guardian emailed (SENT)", NoticeState.SENT),
            ("no email on file (NO_EMAIL)", NoticeState.NO_EMAIL),
        ):
            self.stdout.write(f"    {label}: {states.filter(notice_state=notice).count()} marks")
        self.stdout.write(
            f"    excused: {states.filter(mark=Mark.EXCUSED).count()} marks, "
            f"late: {states.filter(mark=Mark.LATE).count()} marks",
        )
        behalf = ClassRegister.objects.filter(
            tenant=tenant, on_behalf_of__isnull=False,
        ).select_related("school_class")
        self.stdout.write(
            "    taken on the teacher's behalf: "
            + (", ".join(f"{r.school_class.name} {r.date}" for r in behalf) or "none"),
        )
        self.stdout.write(
            f"    same-day changes: "
            f"{MarkChange.objects.filter(tenant=tenant, via=ChangeVia.SAME_DAY).count()}",
        )
        by_status = {
            status: CorrectionRequest.objects.filter(tenant=tenant, status=status).count()
            for status in CorrectionStatus.values
        }
        self.stdout.write(
            f"    corrections: {by_status['PENDING']} waiting, "
            f"{by_status['APPROVED']} approved, {by_status['REJECTED']} rejected",
        )
        self.stdout.write(
            "    closed days: " + ", ".join(
                f"{e.name} {e.start_date}"
                for e in CalendarEvent.all_objects.filter(
                    tenant=tenant, name__in=[CLOSURE_NAME, BRANCH_CLOSURE_NAME],
                )
            ),
        )
        retired = ReasonCode.objects.filter(
            tenant=tenant, retired_at__isnull=False,
        ).values_list("name", flat=True)
        self.stdout.write(f"    retired reason codes: {', '.join(retired) or 'none'}")
        if len(branches) > 1:
            taken = LessonRegister.objects.filter(
                tenant=tenant, submitted_at__isnull=False,
            ).count()
            drafts = LessonRegister.objects.filter(
                tenant=tenant, submitted_at__isnull=True,
            ).count()
            self.stdout.write(
                f"    lesson registers: {taken} submitted, {drafts} unfinished; "
                f"lesson marking on for "
                f"{', '.join(read_attendance_rules(tenant).lesson_marking_levels) or 'no level'}",
            )
        self._staff_summary(tenant, today)
        report = build_report(tenant, admin)
        self.stdout.write(
            f"    below the {report['threshold']}% line this term: "
            + ", ".join(f"{r['student']['name']} {r['rate']}%" for r in report["chronic"][:6]),
        )
        self.stdout.write(f"    audit events at this school: "
                          f"{AuditEvent.objects.filter(tenant=tenant).count()}")
