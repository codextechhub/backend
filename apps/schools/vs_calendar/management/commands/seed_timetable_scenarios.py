"""Build a calendar and a timetable for each shape of school, so it can be seen.

A screen cannot be checked against an endpoint that returns nothing, and the
states this module has to render cannot all exist in one tenant:

    brightfield-lekki   Two branches. A school-wide calendar plus a branch-only
                        event, an audience-narrowed closure, two branches'
                        rooms, a shared bell schedule with its own short Friday,
                        a draft grid holding a deliberate cross-branch teacher
                        clash, a published grid, and a draft exam timetable
                        with a room clash in it.
    st-monicas          One branch. Every row shared, which is what a
                        single-branch school writes, and the case where the
                        whole branch dimension must recede from the responses.
                        No exam period, so the exam screen's empty state is
                        reachable.
    holy-cross          Two branches AND LIVE. The other two are still
                        onboarding, so neither can reach anything gated on a
                        live tenant - the Export Centre most of all.

Everything is driven through the real services - the clash rules, the branch
containment, the publish gate - rather than by writing rows that look right. A
state that cannot be reached honestly fails loudly here instead of being faked
into existence and believed later. The one deliberate exception is the clash
itself, which is written through the ordinary create path precisely because a
clash is savable; that is the rule, not a workaround.

Idempotent: re-running tops each school up and leaves what already matches. Run
it twice - a seeder that is not idempotent invents data.

    python manage.py seed_timetable_scenarios
    python manage.py seed_timetable_scenarios --only st-monicas

Run ``seed_academic_scenarios`` first, which builds the years, classes and
subjects this hangs off. Never run against production.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from schools.vs_academics.models import AcademicSession, SchoolClass, SessionStatus, Subject
from schools.vs_calendar.models import (
    CalendarEvent,
    CalendarEventAudience,
    ClassTimetable,
    DayOfWeek,
    Exam,
    ExamSlot,
    EventType,
    Period,
    PeriodType,
    PublishState,
    Room,
    RoomType,
    Sitting,
    TimetableSlot,
)

CAST = ("brightfield-lekki", "st-monicas", "holy-cross")

#: label, start, end, type. The everyday schedule, shared by the whole school.
BELLS = (
    ("Period 1", "08:00", "08:45", PeriodType.LESSON),
    ("Period 2", "08:45", "09:30", PeriodType.LESSON),
    ("Period 3", "09:30", "10:15", PeriodType.LESSON),
    ("Break", "10:15", "10:45", PeriodType.BREAK),
    ("Period 4", "10:45", "11:30", PeriodType.LESSON),
    ("Period 5", "11:30", "12:15", PeriodType.LESSON),
    ("Lunch", "12:15", "13:00", PeriodType.LUNCH),
    ("Period 6", "13:00", "13:45", PeriodType.LESSON),
)

#: Friday runs short, and it REPLACES the everyday schedule rather than adding
#: to it. Seeded so the screen's "Friday uses its own schedule" line has
#: something real behind it.
FRIDAY_BELLS = (
    ("Assembly", "08:00", "08:30", PeriodType.ASSEMBLY),
    ("Period 1", "08:30", "09:15", PeriodType.LESSON),
    ("Period 2", "09:15", "10:00", PeriodType.LESSON),
)

#: name, code, type, capacity
ROOMS = (
    ("Block A Room 1", "A-1", RoomType.CLASSROOM, 40),
    ("Block A Room 2", "A-2", RoomType.CLASSROOM, 40),
    ("Science Lab", "LAB", RoomType.LABORATORY, 30),
    ("Main Hall", "HALL", RoomType.HALL, 250),
)

SECOND_BRANCH_ROOMS = (
    ("Block C Room 1", "C-1", RoomType.CLASSROOM, 35),
    ("Assembly Hall", "ASM", RoomType.HALL, 200),
)


def _t(value):
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute))


class Command(BaseCommand):
    help = "Seed calendars and timetables for the multi- and single-branch cases."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="One school slug from the cast.")

    def handle(self, *args, **options):
        only = options.get("only")
        if only and only not in CAST:
            raise CommandError(
                f"{only!r} is not in the cast. Pick one of: {', '.join(CAST)}",
            )
        for slug in ([only] if only else list(CAST)):
            self._seed(slug)

    # ── plumbing ──────────────────────────────────────────────────────────

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios and then "
                f"seed_academic_scenarios first - they build the school and "
                f"the year this hangs a calendar off.",
            )
        return school

    def _year(self, tenant):
        year = AcademicSession.all_objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        if year is None:
            raise CommandError(
                f"{tenant.slug} has no active year. Run "
                f"seed_academic_scenarios first.",
            )
        return year

    def _teachers(self, tenant):
        """The people this school can put on a timetable.

        A teacher is a role grant, not a persona column, so this asks the same
        question ``services.teachers`` asks rather than a cheaper one that
        would drift from it.
        """
        from schools.vs_calendar.services.teachers import teaching_users

        return list(teaching_users(tenant))

    # ── the seed ──────────────────────────────────────────────────────────

    @transaction.atomic
    def _seed(self, slug):
        from vs_tenants.models import Branch

        school = self._school(slug)
        tenant = school.tenant
        year = self._year(tenant)
        branches = list(Branch.all_objects.filter(tenant=tenant).order_by("pk"))
        main = branches[0]
        second = branches[1] if len(branches) > 1 else None

        self.stdout.write(f"\n{slug}")

        rooms = self._rooms(tenant, main, second)
        periods = self._periods(tenant, year)
        events = self._events(tenant, year, second)
        self._timetables(tenant, year, rooms, periods)
        self._exams(tenant, year, events, rooms)

    def _rooms(self, tenant, main, second):
        made = {}
        for name, code, kind, capacity in ROOMS:
            row, created = Room.all_objects.get_or_create(
                tenant=tenant, branch=main, name=name,
                defaults={"code": code, "room_type": kind, "capacity": capacity},
            )
            made[(main.pk, name)] = row
            if created:
                self.stdout.write(f"  room     {name} at {main.name}")
        if second is not None:
            for name, code, kind, capacity in SECOND_BRANCH_ROOMS:
                row, created = Room.all_objects.get_or_create(
                    tenant=tenant, branch=second, name=name,
                    defaults={"code": code, "room_type": kind, "capacity": capacity},
                )
                made[(second.pk, name)] = row
                if created:
                    self.stdout.write(f"  room     {name} at {second.name}")
        return made

    def _periods(self, tenant, year):
        made = []
        for index, (label, start, end, kind) in enumerate(BELLS, start=1):
            row, created = Period.all_objects.get_or_create(
                tenant=tenant, session=year, branch=None, day_of_week=None,
                label=label,
                defaults={
                    "order_index": index, "period_type": kind,
                    "start_time": _t(start), "end_time": _t(end),
                },
            )
            made.append(row)
            if created:
                self.stdout.write(f"  period   {label} {start}-{end}")
        for index, (label, start, end, kind) in enumerate(FRIDAY_BELLS, start=1):
            row, created = Period.all_objects.get_or_create(
                tenant=tenant, session=year, branch=None,
                day_of_week=DayOfWeek.FRIDAY, label=label,
                defaults={
                    "order_index": index, "period_type": kind,
                    "start_time": _t(start), "end_time": _t(end),
                },
            )
            if created:
                self.stdout.write(f"  period   Friday {label} {start}-{end}")
        return [p for p in made if p.period_type == PeriodType.LESSON]

    def _events(self, tenant, year, second):
        """A calendar with one of each thing a screen has to render."""
        start = year.start_date
        rows = [
            ("Independence Day", EventType.HOLIDAY, 23, 23, True, None,
             "National public holiday."),
            ("Mid-term break", EventType.MIDTERM_BREAK, 49, 53, True, None, ""),
            ("PTA Meeting", EventType.PTA, 61, 61, False, None,
             "Termly parents and teachers meeting."),
            ("Inter-house Sports", EventType.SPORTS, 67, 67, False, None, ""),
            ("First Term Examinations", EventType.EXAM_PERIOD, 84, 95, False,
             None, "End-of-term examinations."),
        ]
        made = {}
        for name, kind, from_day, to_day, closed, branch, desc in rows:
            row, created = CalendarEvent.all_objects.get_or_create(
                tenant=tenant, session=year, name=name,
                defaults={
                    "event_type": kind,
                    "start_date": start + dt.timedelta(days=from_day),
                    "end_date": start + dt.timedelta(days=to_day),
                    "closes_school": closed, "branch": branch,
                    "description": desc,
                },
            )
            made[name] = row
            if created:
                self.stdout.write(f"  event    {name}")

        if second is not None:
            # A branch-only event, so the scope chip has something behind it.
            row, created = CalendarEvent.all_objects.get_or_create(
                tenant=tenant, session=year, name="Founder's Day",
                defaults={
                    "event_type": EventType.SCHOOL_EVENT,
                    "start_date": start + dt.timedelta(days=74),
                    "end_date": start + dt.timedelta(days=74),
                    "closes_school": True, "branch": second,
                    "description": f"{second.name} only.",
                },
            )
            made["Founder's Day"] = row
            if created:
                self.stdout.write(f"  event    Founder's Day ({second.name})")

        # An audience-narrowed closure: the case the whole audience table
        # exists for. Primary is off; the secondary school is not, and its
        # teaching-day count must not lose the day.
        primary = SchoolClass.all_objects.filter(
            tenant=tenant, session=year, name__istartswith="Primary",
        ).first()
        if primary is not None:
            row, created = CalendarEvent.all_objects.get_or_create(
                tenant=tenant, session=year, name="Primary Speech Day",
                defaults={
                    "event_type": EventType.SCHOOL_EVENT,
                    "start_date": start + dt.timedelta(days=68),
                    "end_date": start + dt.timedelta(days=68),
                    "closes_school": True,
                    "description": "The primary school only.",
                },
            )
            if created:
                CalendarEventAudience.all_objects.get_or_create(
                    tenant=tenant, event=row, level=primary.level,
                )
                self.stdout.write(
                    f"  event    Primary Speech Day (narrowed to "
                    f"{primary.level.name})",
                )
            made["Primary Speech Day"] = row
        return made

    def _timetables(self, tenant, year, rooms, periods):
        teachers = self._teachers(tenant)
        if not teachers:
            self.stdout.write(
                "  timetable  skipped - this school has nobody carrying the "
                "teacher role, so no lesson can name anyone.",
            )
            return
        subjects = list(Subject.all_objects.filter(tenant=tenant)[:4])
        classes = list(
            SchoolClass.all_objects.filter(
                tenant=tenant, session=year, is_active=True,
            ).order_by("pk")[:3],
        )
        if not (subjects and classes and periods):
            return

        by_branch = {}
        for (branch_id, _name), room in rooms.items():
            by_branch.setdefault(branch_id, []).append(room)

        days = [DayOfWeek.MONDAY, DayOfWeek.TUESDAY, DayOfWeek.WEDNESDAY]
        for index, school_class in enumerate(classes):
            branch_id = school_class.branch_id or next(iter(by_branch))
            options = by_branch.get(branch_id) or next(iter(by_branch.values()))
            room = options[index % len(options)]
            for day_index, day in enumerate(days):
                period = periods[day_index % len(periods)]
                TimetableSlot.all_objects.get_or_create(
                    tenant=tenant, session=year, school_class=school_class,
                    day_of_week=day, period=period,
                    defaults={
                        "subject": subjects[day_index % len(subjects)],
                        "teacher": teachers[index % len(teachers)],
                        "room": room,
                    },
                )
            record, _ = ClassTimetable.all_objects.get_or_create(
                tenant=tenant, session=year, school_class=school_class,
            )
            # One published, the rest draft, so both chips are on screen.
            if index == 0 and record.status != PublishState.PUBLISHED:
                record.status = PublishState.PUBLISHED
                record.published_at = timezone.now()
                record.save(update_fields=["status", "published_at", "updated_at"])
            self.stdout.write(
                f"  grid     {school_class.name} ({record.get_status_display()})",
            )

        # A deliberate clash on the SECOND class, so the red cells and the
        # blocked publish are both reachable. Written through the ordinary
        # path, because a clash is savable - that is the rule, not a shortcut.
        if len(classes) > 1 and len(teachers) >= 1:
            first, second = classes[0], classes[1]
            anchor = TimetableSlot.all_objects.filter(
                session=year, school_class=first,
            ).first()
            if anchor is not None:
                TimetableSlot.all_objects.get_or_create(
                    tenant=tenant, session=year, school_class=second,
                    day_of_week=anchor.day_of_week, period=anchor.period,
                    defaults={
                        "subject": subjects[0],
                        "teacher": anchor.teacher,      # the double booking
                        "room": anchor.room,
                    },
                )
                self.stdout.write(
                    f"  clash    {second.name} shares "
                    f"{DayOfWeek(anchor.day_of_week).label} "
                    f"{anchor.period.label} with {first.name}",
                )

    def _exams(self, tenant, year, events, rooms):
        period = events.get("First Term Examinations")
        if period is None:
            return
        exam, created = Exam.all_objects.get_or_create(
            tenant=tenant, calendar_event=period,
            defaults={"name": period.name},
        )
        if created:
            self.stdout.write(f"  exam     {exam.name}")

        teachers = self._teachers(tenant)
        subjects = list(Subject.all_objects.filter(tenant=tenant)[:2])
        classes = list(
            SchoolClass.all_objects.filter(
                tenant=tenant, session=year, is_active=True,
            ).order_by("pk")[:2],
        )
        if not (subjects and classes):
            return

        hall = next(
            (r for (_b, name), r in rooms.items() if name in ("Main Hall", "Block A Room 1")),
            None,
        )
        for index, school_class in enumerate(classes):
            ExamSlot.all_objects.get_or_create(
                tenant=tenant, exam=exam, school_class=school_class,
                exam_date=period.start_date, sitting=Sitting.MORNING,
                defaults={
                    "subject": subjects[0],
                    # Both classes in one hall: a real arrangement, and the
                    # room clash the exam screen has to show in red.
                    "room": hall,
                    "invigilator": teachers[index % len(teachers)] if teachers else None,
                },
            )
        self.stdout.write(
            f"  papers   {len(classes)} in the {period.start_date:%d %b} morning "
            f"sitting, sharing a room",
        )
