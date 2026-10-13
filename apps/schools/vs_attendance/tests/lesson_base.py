"""Brightfield and Sunrise with lesson marking on and a published timetable.

Builds on :class:`AttendanceFixture`. Lesson marking is switched on for the
JSS1 level at Brightfield and for Basic 3 at Sunrise. The bell schedule is
Period 1 (08:00), Period 2 (08:45), a Break (09:30) and Period 3 (10:00).

Brightfield's timetable, Monday unless said:

* JSS1 A (Chiamaka, Tunde and Kelechi on the roll, Amina suspended): Period 1
  English with Mr Eze and Period 2 Mathematics with Ms Bello; on Tuesday Period
  1 English with Mr Eze, on Thursday (the Founders' Day closure) Period 1
  English with Mr Eze;
* JSS1 B: Period 1 Mathematics with Ms Bello;
* SSS2 Science (Ikeja): Period 1 English with Ada Obi, Ikeja's branch admin.

JSS1 A, JSS1 B and SSS2 Science are published; Primary 4 A is not in a level
that marks lessons and has no timetable. Sunrise's Basic 3 has Period 1
Mathematics with its class teacher on Monday, published.

Whole-school and branch-bound report readers are added for the report tests, and
the helpers below take morning and lesson registers through the API or write
them straight to the tables for the tests that need volume.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import Subject
from schools.vs_calendar.models import (
    ClassTimetable,
    DayOfWeek,
    Period,
    PeriodType,
    PublishState,
    TimetableSlot,
)
from vs_rbac.tests.helpers import make_assignment, make_school_admin

from ..constants import (
    CFG_LESSON_MARKING_LEVELS,
    PERM_REGISTER_VIEW,
    PERM_REPORT_EXPORT,
    PERM_REPORT_VIEW,
)
from ..models import ClassRegister, LessonMark, LessonRegister, StudentMark
from .base import LAST_FRIDAY, NOW, TODAY, AttendanceFixture, make_key_role, set_setting

SEND = "schools.vs_attendance.services.notices.send_notification"

#: A Monday inside the first term, a week before the fixture clock's today.
LAST_MONDAY = dt.date(2025, 10, 6)


def make_period(tenant, session, label, order, start, end, kind=PeriodType.LESSON):
    return Period.all_objects.create(
        tenant=tenant, session=session, order_index=order, label=label,
        period_type=kind, start_time=start, end_time=end,
    )


def make_slot(tenant, school_class, day, period, subject, teacher):
    return TimetableSlot.all_objects.create(
        tenant=tenant, session=school_class.session, school_class=school_class,
        day_of_week=day, period=period, subject=subject, teacher=teacher,
    )


def publish(tenant, school_class, status=PublishState.PUBLISHED):
    return ClassTimetable.all_objects.update_or_create(
        tenant=tenant, session=school_class.session, school_class=school_class,
        defaults={"status": status, "published_at": NOW if status == PublishState.PUBLISHED else None},
    )[0]


class LessonFixture(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        set_setting(CFG_LESSON_MARKING_LEVELS, ["JSS1"], cls.tenant, cls.admin)

        cls.english = Subject.all_objects.create(
            tenant=cls.tenant, name="English Language", code="ENG",
        )
        cls.maths = Subject.all_objects.create(
            tenant=cls.tenant, name="Mathematics", code="MTH",
        )
        t = dt.time
        cls.period1 = make_period(cls.tenant, cls.year, "Period 1", 1, t(8, 0), t(8, 45))
        cls.period2 = make_period(cls.tenant, cls.year, "Period 2", 2, t(8, 45), t(9, 30))
        cls.breaktime = make_period(
            cls.tenant, cls.year, "Break", 3, t(9, 30), t(10, 0), PeriodType.BREAK,
        )
        cls.period3 = make_period(cls.tenant, cls.year, "Period 3", 4, t(10, 0), t(10, 45))

        cls.zainab = cls.make_student("Zainab", "Aliyu", cls.jss1b, "BFS/301")
        cls.emeka = cls.make_student("Emeka", "Obi", cls.jss1b, "BFS/302")
        cls.ngozi = cls.make_student("Ngozi", "Ibe", cls.sss2, "BFS/401", branch=cls.ikeja)
        cls.ada = cls.make_student("Ada", "Eze", cls.sss2, "BFS/402", branch=cls.ikeja)

        mon, tue, thu = DayOfWeek.MONDAY, DayOfWeek.TUESDAY, DayOfWeek.THURSDAY
        cls.a_english = make_slot(cls.tenant, cls.jss1a, mon, cls.period1, cls.english, cls.eze)
        cls.a_maths = make_slot(cls.tenant, cls.jss1a, mon, cls.period2, cls.maths, cls.bello)
        cls.a_tuesday = make_slot(cls.tenant, cls.jss1a, tue, cls.period1, cls.english, cls.eze)
        cls.a_thursday = make_slot(cls.tenant, cls.jss1a, thu, cls.period1, cls.english, cls.eze)
        cls.b_maths = make_slot(cls.tenant, cls.jss1b, mon, cls.period1, cls.maths, cls.bello)
        cls.sss2_english = make_slot(
            cls.tenant, cls.sss2, mon, cls.period1, cls.english, cls.ikeja_admin,
        )
        for school_class in (cls.jss1a, cls.jss1b, cls.sss2):
            publish(cls.tenant, school_class)

        # Sunrise: one branch, and a class with no branch of its own.
        set_setting(CFG_LESSON_MARKING_LEVELS, ["BAS3"], cls.solo, cls.solo_admin)
        year = cls.solo_class.session
        cls.solo_maths = Subject.all_objects.create(
            tenant=cls.solo, name="Mathematics", code="MTH",
        )
        cls.solo_period = make_period(cls.solo, year, "Period 1", 1, t(8, 0), t(8, 45))
        cls.solo_slot = make_slot(
            cls.solo, cls.solo_class, mon, cls.solo_period, cls.solo_maths, cls.solo_teacher,
        )
        publish(cls.solo, cls.solo_class)
        keys = (PERM_REGISTER_VIEW, PERM_REPORT_VIEW, PERM_REPORT_EXPORT)
        role = cls.reporter_role = make_key_role(cls.tenant, "Reporter", "reporter", keys)
        cls.reporter = make_school_admin(
            None, email="reports@brightfield.test", tenant=cls.tenant,
            first_name="Rita", last_name="Okon",
        )
        make_assignment(cls.tenant, cls.reporter, role, branch=None)
        cls.lekki_reporter = make_school_admin(
            cls.lekki, email="reports@lekki.test", first_name="Lola", last_name="Ade",
        )
        make_assignment(cls.tenant, cls.lekki_reporter, role, branch=cls.lekki)
        cls.ikeja_reporter = make_school_admin(
            cls.ikeja, email="reports@ikeja.test", first_name="Ike", last_name="Obi",
        )
        make_assignment(cls.tenant, cls.ikeja_reporter, role, branch=cls.ikeja)
        solo_role = make_key_role(cls.solo, "Reporter", "reporter", keys)
        cls.solo_reporter = make_school_admin(
            None, email="reports@sunrise.test", tenant=cls.solo,
            first_name="Soji", last_name="Eze",
        )
        make_assignment(cls.solo, cls.solo_reporter, solo_role, branch=None)

    def setUp(self):
        super().setUp()
        # Class registers taken for a test email the guardians of the absent;
        # the lesson tests are about what is not sent, so sending is watched.
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)

    # ── HTTP ──────────────────────────────────────────────────────────────

    def lesson_url(self, name, slot, on_date=TODAY, **extra):
        return reverse(name, kwargs={"slot_id": slot.pk, "on_date": str(on_date), **extra})

    def lesson(self, user, slot, on_date=TODAY):
        return TenantAPIClient(user).get(self.lesson_url("attendance-lesson", slot, on_date))

    def lessons(self, user, **params):
        return TenantAPIClient(user).get(reverse("attendance-lessons"), params)

    def mark_lesson(self, user, slot, student, mark, on_date=TODAY, **body):
        return TenantAPIClient(user).put(
            self.lesson_url("attendance-lesson-mark", slot, on_date, student_id=student.pk),
            {"mark": mark, **body}, format="json",
        )

    def fill_lesson(self, user, slot, on_date=TODAY):
        return TenantAPIClient(user).post(
            self.lesson_url("attendance-lesson-fill-present", slot, on_date), {}, format="json",
        )

    def submit_lesson(self, user, slot, on_date=TODAY):
        return TenantAPIClient(user).post(
            self.lesson_url("attendance-lesson-submit", slot, on_date), {}, format="json",
        )

    def take_lesson(self, user, slot, on_date=TODAY, **marks):
        """Mark the class's whole roster in the lesson and submit it.

        *marks* maps a pupil's first name (lower case) to a mark; anyone not
        named is Present and a Late mark arrives at 08:30.
        """
        from schools.vs_students.services.roster import roster_as_at

        for entry in roster_as_at(slot.school_class.tenant, slot.school_class, on_date):
            if entry.suspended:
                continue
            kind = marks.get(entry.student.first_name.lower(), "P")
            body = {"arrived_at": "08:30"} if kind == "L" else {}
            response = self.mark_lesson(user, slot, entry.student, kind, on_date, **body)
            assert response.status_code == 200, response.data
        return self.submit_lesson(user, slot, on_date)

    def lesson_rows(self, response):
        return {row["student_id"]: row for row in self.data(response)["rows"]}

    def switch_lessons_off(self):
        set_setting(CFG_LESSON_MARKING_LEVELS, [], self.tenant, self.admin)

    def morning(self, school_class=None, on_date=TODAY, user=None, **marks):
        marks = marks or {"tunde": "A", "kelechi": "L"}
        response = self.take(user or self.eze, school_class or self.jss1a, on_date, **marks)
        self.assertEqual(response.status_code, 200, response.data)

    def english_lesson(self, on_date=TODAY, **marks):
        marks = marks or {"tunde": "A", "kelechi": "A", "chiamaka": "A"}
        response = self.take_lesson(self.eze, self.a_english, on_date, **marks)
        self.assertEqual(response.status_code, 200, response.data)

    def hub(self, user=None, **params):
        response = self.today(user or self.admin, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def report(self, user=None, **params):
        response = TenantAPIClient(user or self.reporter).get(reverse("attendance-reports"), params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def tab(self, pupil, user=None):
        response = self.student_tab(user or self.admin, pupil)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    # ── records written straight to the tables, for the volume tests ──────

    def orm_morning(self, school_class, home, on_date, marks):
        register = ClassRegister.objects.create(
            tenant=school_class.tenant, school_class=school_class, date=on_date, branch=home,
            session=school_class.session, term=self.first_term, submitted_at=NOW,
            submitted_by=self.admin,
        )
        for pupil, kind in marks.items():
            StudentMark.objects.create(
                tenant=school_class.tenant, register=register, student=pupil, mark=kind,
                marked_by=self.admin,
            )

    def orm_lesson(self, slot, home, on_date, marks):
        register = LessonRegister.objects.create(
            tenant=slot.tenant, slot=slot, period=slot.period, school_class=slot.school_class,
            date=on_date, branch=home, session=slot.session, term=self.first_term,
            period_label=slot.period.label, starts_at=slot.period.start_time,
            ends_at=slot.period.end_time, subject=slot.subject, teacher=slot.teacher,
            submitted_at=NOW, submitted_by=self.admin,
        )
        for pupil, kind in marks.items():
            LessonMark.objects.create(
                tenant=slot.tenant, register=register, student=pupil, mark=kind,
                marked_by=self.admin,
            )

    def add_class(self, number, home=None):
        """A class (Ikeja's unless *home* says) with a lesson today that two of its three pupils missed."""
        home = home or self.ikeja
        school_class = self.make_class(
            self.jss1, f"Extra {number}", f"EX{number}", home, None,
        )
        pupils = [
            self.make_student(
                f"Pupil{number}{n}", "Test", school_class, f"EX/{number}{n}", branch=home,
            )
            for n in range(3)
        ]
        slot = make_slot(
            self.tenant, school_class, 1, self.period3, self.maths, self.bello,
        )
        publish(self.tenant, school_class)
        self.orm_morning(school_class, home, TODAY, dict(zip(pupils, "PLP")))
        self.orm_lesson(slot, home, TODAY, dict(zip(pupils, "APA")))
        self.orm_morning(school_class, home, LAST_FRIDAY, dict(zip(pupils, "PPP")))
