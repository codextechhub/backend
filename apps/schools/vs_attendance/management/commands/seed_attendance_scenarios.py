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
* one retired reason code.

Classes are chosen by size, the largest of each branch first, from the roll
``seed_student_scenarios`` builds; classes outside the cast have taken no
register this term and so read as "not taken" on every day of it.

**Everything goes through the real services.** Marks come from
``marks.set_mark``, ``fill_present`` and ``submit``, corrections from
``corrections.request_correction`` and ``decide``, reminders from
``reminders.remind``, the suspension from ``status.transition``, the teachers
from the staff module's own invitation path. A state that cannot be reached
honestly fails loudly here instead of being written as a row and believed.
The one deliberate exception is the closure, which is written as a calendar row
exactly as ``seed_timetable_scenarios`` writes its events: the calendar has no
service to call.

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

from schools.vs_academics.models import AcademicSession, SchoolClass, SessionStatus
from schools.vs_calendar.models import CalendarEvent, EventType
from schools.vs_calendar.services.calendar import term_of
from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.management.commands.seed_staff_scenarios import (
    Command as StaffSeeder,
)
from schools.vs_staff.models import StaffProfile
from schools.vs_staff.services import teaching
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import Student
from schools.vs_students.services import enrolment as enrolment_service
from schools.vs_students.services.status import transition
from vs_audit.models import AuditEvent
from vs_config.clock import branch_today, tenant_today

from schools.vs_attendance.constants import ChangeVia, CorrectionStatus, Mark, NoticeState
from schools.vs_attendance.models import (
    ClassRegister,
    CorrectionRequest,
    MarkChange,
    ReasonCode,
    RegisterReminder,
    StudentMark,
)
from schools.vs_attendance.services import corrections, marks, reasons, reminders
from schools.vs_attendance.services.days import DayCalendar
from schools.vs_attendance.services.registers import load_context
from schools.vs_attendance.services.report import build_report
from schools.vs_attendance.services.roster import roster_as_at

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
            "\nLogins. Each school is reached at <slug>.localhost:5199. Admin "
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
            for index, day in enumerate(c2.past[:C2_DAYS]):
                taker = admin if index == 0 else c2.teacher_user
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
        """Three corrections on C1's closed days: one waiting, one approved, one rejected."""
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
            mark = StudentMark.objects.filter(
                register__school_class=c1.school_class, register__date=day,
                student_id=student_id,
            ).first()
            if mark is None:
                raise CommandError(f"No mark for pupil {student_id} in {c1.name} on {day}.")
            request = CorrectionRequest.objects.filter(mark=mark).first()
            if request is None:
                ctx = load_context(tenant, teacher, c1.school_class.pk, day)
                request = corrections.request_correction(
                    ctx, teacher, student_id, to_mark=to_mark,
                    reason_code_id=code.pk, note=note,
                )
                self._made(f"correction filed: {mark.student.full_name} on {day}")
            if approve is not None and request.status == CorrectionStatus.PENDING:
                corrections.decide(
                    tenant, admin, request.pk, approve=approve,
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
        reminders.remind(load_context(tenant, admin, c2.school_class.pk, today), admin)
        self._made(f"reminder to {c2.teacher_user.full_name} for {c2.name} {today}")

    def _retire_a_reason(self, tenant, admin):
        code = ReasonCode.objects.get(tenant=tenant, name=RETIRED_REASON)
        if code.retired_at is not None:
            return
        reasons.retire_reason_code(tenant, admin, code)
        self._made(f"reason code {RETIRED_REASON} retired")

    # ── what now exists ───────────────────────────────────────────────────

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
        report = build_report(tenant, admin)
        self.stdout.write(
            f"    below the {report['threshold']}% line this term: "
            + ", ".join(f"{r['student']['name']} {r['rate']}%" for r in report["chronic"][:6]),
        )
        self.stdout.write(f"    audit events at this school: "
                          f"{AuditEvent.objects.filter(tenant=tenant).count()}")
