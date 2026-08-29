"""The branch switcher, on every screen that has one.

Written after the switcher was found to work on two surfaces out of seven. The
client had been sending ``?branch=<id>`` everywhere from the day the module
shipped; rooms and the bell schedule read it, and events, class timetables,
exam papers, the teacher picker and the hub did not. An unrecognised query
parameter is not an error, so those five accepted it, ignored it, and answered
with every branch: a Lekki administrator switched to Lekki was reading Ikeja's
events, Ikeja's timetables and Ikeja's exam papers, with the switcher on screen
saying Lekki the whole time.

Nothing failed. That is the point of this file, and the reason every surface is
asserted here by name rather than the helper being unit-tested once: the defect
was never in the filtering, it was in the five places that did not call it.

Two rules run through all of it:

**The read is inclusive.** A null branch means "shared across the whole school",
so Lekki's calendar carries the school-wide public holidays as well as its own.
Filtering them out would empty the screen of the rows most schools create, which
is the same defect in the opposite direction.

**A person's WEEK is never narrowed.** The teacher list narrows, because a
branch administrator should not scroll past another branch's staff. The grid
does not, because Mr Eze teaches at two branches and a half-shown week is how a
school double-books him.
"""
from __future__ import annotations

import datetime as dt

from ..models import CalendarEvent, Exam, ExamSlot, Sitting, TimetableSlot
from .base import _Base, _SingleBranchBase


class _LensBase(_Base):
    """Brightfield, plus one row of everything at each branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.lekki_event = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=cls.lekki,
            name="Lekki Founder's Day", event_type="SCHOOL_EVENT",
            start_date=dt.date(2025, 10, 6), end_date=dt.date(2025, 10, 6),
        )
        cls.ikeja_event = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=cls.ikeja,
            name="Ikeja Speech Day", event_type="SCHOOL_EVENT",
            start_date=dt.date(2025, 10, 7), end_date=dt.date(2025, 10, 7),
        )
        cls.shared_event = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=None,
            name="Independence Day", event_type="HOLIDAY",
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
            closes_school=True,
        )

        # An exam period per branch, each with its own timetable.
        cls.lekki_exam = Exam.all_objects.create(
            tenant=cls.tenant, name="Lekki Mocks",
            calendar_event=CalendarEvent.all_objects.create(
                tenant=cls.tenant, session=cls.year, branch=cls.lekki,
                name="Lekki Mock Exams", event_type="EXAM_PERIOD",
                start_date=dt.date(2025, 11, 3), end_date=dt.date(2025, 11, 7),
            ),
        )
        cls.ikeja_exam = Exam.all_objects.create(
            tenant=cls.tenant, name="Ikeja Mocks",
            calendar_event=CalendarEvent.all_objects.create(
                tenant=cls.tenant, session=cls.year, branch=cls.ikeja,
                name="Ikeja Mock Exams", event_type="EXAM_PERIOD",
                start_date=dt.date(2025, 11, 3), end_date=dt.date(2025, 11, 7),
            ),
        )

    def names(self, response):
        rows = response.json()["data"]
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        return {row["name"] for row in rows}


class EventLensTests(_LensBase):
    """The calendar, which is where the report came from."""

    def test_no_lens_shows_every_branch(self):
        found = self.names(self.get(self.admin, "calendar-event-list"))
        self.assertIn("Lekki Founder's Day", found)
        self.assertIn("Ikeja Speech Day", found)

    def test_the_lens_drops_the_other_branch(self):
        found = self.names(
            self.get(self.admin, "calendar-event-list", {"branch": self.lekki.pk}),
        )
        self.assertIn("Lekki Founder's Day", found)
        self.assertNotIn("Ikeja Speech Day", found)

    def test_the_lens_keeps_the_school_wide_entries(self):
        """Independence Day is on Lekki's calendar too, and closes it.

        An exclusive read would take the public holidays off every branch's
        calendar, which is worse than not filtering at all: a branch would see
        its own speech day and no sign that the school is shut on 1 October.
        """
        found = self.names(
            self.get(self.admin, "calendar-event-list", {"branch": self.lekki.pk}),
        )
        self.assertIn("Independence Day", found)

    def test_the_lens_and_the_scope_facet_compose(self):
        """The switcher says which branch; the on-screen filter says which
        kind of row inside it. Neither replaces the other."""
        found = self.names(self.get(self.admin, "calendar-event-list", {
            "branch": self.lekki.pk, "scope": "school",
        }))
        self.assertIn("Independence Day", found)
        self.assertNotIn("Lekki Founder's Day", found)

    def test_a_branch_that_is_not_this_schools_is_refused(self):
        """Refused rather than ignored, which is the whole defect in one line:
        a filter nobody reads looks exactly like a filter that found nothing."""
        response = self.get(
            self.admin, "calendar-event-list", {"branch": 999999},
        )
        self.assertEqual(response.status_code, 400)


class ClassTimetableLensTests(_LensBase):
    def test_the_picker_narrows_to_the_branch(self):
        found = self.names(
            self.get(self.admin, "calendar-class-list", {"branch": self.lekki.pk}),
        )
        self.assertIn("JSS1 A", found)
        self.assertNotIn("SSS2 Science", found)

    def test_a_school_wide_class_stays_on_every_branch(self):
        """Primary 4 A has no branch, so it belongs to both of them."""
        for branch in (self.lekki, self.ikeja):
            found = self.names(
                self.get(self.admin, "calendar-class-list", {"branch": branch.pk}),
            )
            self.assertIn("Primary 4 A", found, branch.name)


class ExamLensTests(_LensBase):
    def test_exams_follow_the_branch_of_their_period(self):
        """An exam has no branch column. It hangs off the exam period on the
        calendar, and that is what carries the scope."""
        found = self.names(
            self.get(self.admin, "calendar-exam-list", {"branch": self.lekki.pk}),
        )
        self.assertIn("Lekki Mocks", found)
        self.assertNotIn("Ikeja Mocks", found)

    def test_a_school_wide_exam_period_shows_at_every_branch(self):
        Exam.all_objects.create(
            tenant=self.tenant, name="Whole School Mocks",
            calendar_event=CalendarEvent.all_objects.create(
                tenant=self.tenant, session=self.year, branch=None,
                name="Mock Exams", event_type="EXAM_PERIOD",
                start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 5),
            ),
        )
        for branch in (self.lekki, self.ikeja):
            found = self.names(
                self.get(self.admin, "calendar-exam-list", {"branch": branch.pk}),
            )
            self.assertIn("Whole School Mocks", found, branch.name)


class TeacherLensTests(_LensBase):
    """The list narrows. The week never does."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.make_teacher("eze@brightfield.test", "Ngozi", "Eze")
        cls.tayo = cls.make_teacher("tayo@brightfield.test", "Tayo", "Bello")
        cls.newcomer = cls.make_teacher("new@brightfield.test", "Amara", "Nwosu")

        # Mr Eze teaches at both branches. Mr Bello only at Ikeja.
        TimetableSlot.all_objects.create(
            tenant=cls.tenant, session=cls.year, school_class=cls.jss1a,
            day_of_week=1, period=cls.p1, subject=cls.physics, teacher=cls.eze,
        )
        TimetableSlot.all_objects.create(
            tenant=cls.tenant, session=cls.year, school_class=cls.sss2,
            day_of_week=4, period=cls.p1, subject=cls.physics, teacher=cls.eze,
        )
        TimetableSlot.all_objects.create(
            tenant=cls.tenant, session=cls.year, school_class=cls.sss2,
            day_of_week=2, period=cls.p2, subject=cls.maths, teacher=cls.tayo,
        )

    def test_the_list_narrows_to_who_teaches_there(self):
        found = self.names(
            self.get(self.admin, "calendar-teacher-list", {"branch": self.lekki.pk}),
        )
        self.assertIn("Ngozi Eze", found)
        self.assertNotIn("Tayo Bello", found)

    def test_someone_teaching_at_both_appears_under_both(self):
        for branch in (self.lekki, self.ikeja):
            found = self.names(
                self.get(self.admin, "calendar-teacher-list", {"branch": branch.pk}),
            )
            self.assertIn("Ngozi Eze", found, branch.name)

    def test_a_teacher_with_no_lessons_yet_appears_everywhere(self):
        """The one rule that looks like a loophole and is not.

        Amara Nwosu was added this morning, teaches nothing and is tied to no
        branch. Hiding her from every branch's picker would make a new teacher
        unreachable from all of them.
        """
        for branch in (self.lekki, self.ikeja):
            found = self.names(
                self.get(self.admin, "calendar-teacher-list", {"branch": branch.pk}),
            )
            self.assertIn("Amara Nwosu", found, branch.name)

    def test_a_teachers_week_is_never_narrowed(self):
        """Mr Eze's Thursday is at Ikeja. Filter it away and Lekki books him
        for Thursday and Ikeja loses him."""
        response = self.get(
            self.admin, "calendar-teacher-grid",
            {"branch": self.lekki.pk}, user_id=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200)
        grid = response.json()["data"]
        filled = [
            cell for day in grid["days"] for cell in day["cells"]
            if cell.get("slot")
        ]
        # Both branches are in his week, and the Ikeja cell says so.
        self.assertEqual(len(filled), 2)
        self.assertIn(
            "Ikeja Branch",
            {cell["slot"].get("branch_name") for cell in filled},
        )


class OverviewLensTests(_LensBase):
    """The hub counts what the screens below it list."""

    #: The hub reads "today" from the request, so these pin it inside the
    #: fixture's year rather than depending on the day the suite is run.
    ON = "2025-09-15"

    def test_next_up_drops_the_other_branch(self):
        """The hub's own list, which is the events list in miniature."""
        whole = self.get(
            self.admin, "calendar-overview", {"on": self.ON},
        ).json()["data"]
        lekki = self.get(
            self.admin, "calendar-overview",
            {"on": self.ON, "branch": self.lekki.pk},
        ).json()["data"]
        self.assertIn(
            "Ikeja Speech Day", {row["name"] for row in whole["next_up"]},
        )
        self.assertNotIn(
            "Ikeja Speech Day", {row["name"] for row in lekki["next_up"]},
        )
        self.assertIn(
            "Lekki Founder's Day", {row["name"] for row in lekki["next_up"]},
        )

    def test_the_room_count_follows_the_lens(self):
        """Brightfield has two rooms at Lekki and one at Ikeja. A hub reading
        three over a Rooms screen showing two is worse than no count."""
        whole = self.get(self.admin, "calendar-overview").json()["data"]
        lekki = self.get(
            self.admin, "calendar-overview", {"branch": self.lekki.pk},
        ).json()["data"]
        self.assertEqual(whole["counts"]["rooms"], 3)
        self.assertEqual(lekki["counts"]["rooms"], 2)

    def test_the_event_count_follows_the_lens(self):
        whole = self.get(
            self.admin, "calendar-overview", {"on": self.ON},
        ).json()["data"]
        lekki = self.get(
            self.admin, "calendar-overview",
            {"on": self.ON, "branch": self.lekki.pk},
        ).json()["data"]
        self.assertLess(
            lekki["counts"]["events_in_term"],
            whole["counts"]["events_in_term"],
        )


class SingleBranchLensTests(_SingleBranchBase):
    """Sunrise runs one branch, so the lens has nothing to do."""

    def test_the_parameter_is_ignored_rather_than_refused(self):
        """A single-branch school never renders the switcher, but a stale tab
        or a bookmarked URL can still carry the parameter, and answering 400 to
        it would break a screen for no gain."""
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None,
            name="Founders Day", event_type="SCHOOL_EVENT",
            start_date=dt.date(2025, 10, 6), end_date=dt.date(2025, 10, 6),
        )
        response = self.get(
            self.admin, "calendar-event-list", {"branch": self.branch.pk},
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertEqual({r["name"] for r in rows}, {"Founders Day"})
