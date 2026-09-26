"""A branch administrator reads shared academic and calendar rows and changes only their own.

Brightfield runs Lekki and Ikeja. The head of Ikeja holds every academic and
calendar key, pinned to Ikeja. Primary 4 A, the bell schedule, Mathematics and
the First Term Examinations are school-wide; SSS2 Science is Ikeja's; JSS1 A is
Lekki's. The whole-school administrator is the control: nothing narrows them.
"""
from __future__ import annotations

import datetime as dt

from rest_framework.test import APIClient  # noqa: F401  (client comes from _Base)

from schools.vs_academics.models import AcademicSession, Level, SubjectOffering
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_permission, make_role_permission

from ..models import CalendarEvent, EventType, Exam, ExamSlot, TimetableSlot
from .base import _Base

ACADEMIC_KEYS = (
    "academics.session.view", "academics.session.create",
    "academics.session.update", "academics.session.activate",
    "academics.session.archive",
    "academics.structure.view", "academics.structure.update",
    "academics.structure.archive",
    "academics.classes.view", "academics.classes.update",
    "academics.classes.archive",
    "academics.subject.view", "academics.subject.update",
    "academics.subject.archive",
)

READ_ONLY = "SHARED_RECORD_READ_ONLY"


class _SharedBase(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in ACADEMIC_KEYS:
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.eze = cls.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")


class AcademicRowsTests(_SharedBase):
    def test_a_shared_class_is_read_only_to_a_branch_head(self):
        response = self.patch(
            self.ikeja_admin, "academics-class-detail", {"name": "Primary 4 Alpha"},
            pk=self.pry4a.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], READ_ONLY)

    def test_their_own_class_is_editable(self):
        response = self.patch(
            self.ikeja_admin, "academics-class-detail", {"name": "SSS2 Sciences"},
            pk=self.sss2.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_archiving_a_shared_class_is_refused(self):
        response = self.post(self.ikeja_admin, "academics-class-archive", pk=self.pry4a.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_class_list_says_which_rows_the_viewer_may_change(self):
        response = self.get(self.ikeja_admin, "academics-class-list")
        self.assertEqual(response.status_code, 200, response.data)
        flags = {row["name"]: row["can_manage"] for row in response.data["data"]}
        self.assertFalse(flags["Primary 4 A"])
        self.assertTrue(flags["SSS2 Science"])
        self.assertNotIn("JSS1 A", flags)

    def test_a_shared_subject_is_read_only(self):
        response = self.patch(
            self.ikeja_admin, "academics-subject-detail", {"name": "Maths"},
            pk=self.maths.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_whole_school_admin_still_edits_shared_rows(self):
        response = self.patch(
            self.admin, "academics-class-detail", {"name": "Primary 4 Alpha"},
            pk=self.pry4a.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["can_manage"])


class OfferingsTests(_SharedBase):
    """Where a shared subject is offered: each offering is judged by its level."""

    def setUp(self):
        self.ikeja_level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.program,
            branch=self.ikeja, name="Ikeja Prep", code="IKP", order_index=3,
        )
        self.lekki_level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.program,
            branch=self.lekki, name="Lekki Prep", code="LKP", order_index=4,
        )
        for level in (self.jss1, self.lekki_level):
            SubjectOffering.all_objects.create(
                tenant=self.tenant, subject=self.maths, level=level,
            )

    def _offered(self):
        return set(
            SubjectOffering.all_objects.filter(subject=self.maths)
            .values_list("level__name", flat=True)
        )

    def test_a_branch_head_adds_their_own_level_and_keeps_everybody_elses(self):
        response = self.put(
            self.ikeja_admin, "academics-subject-offerings",
            {"level_ids": [self.jss1.pk, self.ikeja_level.pk]}, pk=self.maths.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        # Lekki Prep was never sent, because Ikeja cannot see it, and survives.
        self.assertEqual(self._offered(), {"JSS1", "Ikeja Prep", "Lekki Prep"})

    def test_removing_a_shared_levels_offering_is_refused(self):
        response = self.put(
            self.ikeja_admin, "academics-subject-offerings",
            {"level_ids": [self.ikeja_level.pk]}, pk=self.maths.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self._offered(), {"JSS1", "Lekki Prep"})

    def test_rewriting_one_year_leaves_another_years_offerings_alone(self):
        last_year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2024/2025",
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2025, 7, 18),
            status="ARCHIVED",
        )
        old_level = Level.all_objects.create(
            tenant=self.tenant, session=last_year, program=self.program,
            name="JSS1", code="JSS1-OLD", order_index=1,
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=self.maths, level=old_level,
        )
        response = self.put(
            self.admin, "academics-subject-offerings",
            {"level_ids": [self.primary4.pk]}, pk=self.maths.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            SubjectOffering.all_objects.filter(subject=self.maths, level=old_level).exists(),
        )


class SessionTests(_SharedBase):
    def test_a_branch_head_cannot_set_up_a_school_wide_year(self):
        response = self.post(self.ikeja_admin, "academics-session-list", {
            "name": "2026/2027", "start_date": "2026-09-07", "end_date": "2027-07-16",
            "branch_ids": [],
        })
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_branch_head_cannot_archive_the_school_wide_year(self):
        response = self.post(self.ikeja_admin, "academics-session-archive", pk=self.year.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_copying_a_year_forward_is_a_school_wide_act(self):
        response = self.post(
            self.ikeja_admin, "academics-session-roll-forward", {"from": self.year.pk},
            pk=self.year.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)


class CalendarRowsTests(_SharedBase):
    def test_another_branchs_event_is_not_found_by_id(self):
        lekki_day = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki,
            name="Lekki Sports Day", event_type=EventType.SPORTS,
            start_date=dt.date(2025, 10, 3), end_date=dt.date(2025, 10, 3),
        )
        self.assertEqual(
            self.get(self.ikeja_admin, "calendar-event-detail", pk=lekki_day.pk).status_code,
            404,
        )
        response = self.patch(
            self.ikeja_admin, "calendar-event-detail", {"name": "Renamed"}, pk=lekki_day.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_shared_event_is_read_only(self):
        founders = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None,
            name="Founders Day", event_type=EventType.SPORTS,
            start_date=dt.date(2025, 10, 10), end_date=dt.date(2025, 10, 10),
        )
        response = self.get(self.ikeja_admin, "calendar-event-detail", pk=founders.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["can_manage"])
        response = self.patch(
            self.ikeja_admin, "calendar-event-detail", {"name": "Renamed"}, pk=founders.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            self.delete(self.ikeja_admin, "calendar-event-detail", pk=founders.pk).status_code,
            403,
        )

    def test_the_school_wide_bell_schedule_is_read_only(self):
        response = self.patch(
            self.ikeja_admin, "calendar-period-detail", {"label": "Lesson 1"}, pk=self.p1.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)


class ExamTests(_SharedBase):
    def setUp(self):
        self.period_event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year,
            name="First Term Examinations", event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 12),
        )
        self.exam = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=self.period_event,
            name="First Term Examinations",
        )

    def paper(self, user, school_class, room):
        return self.post(user, "calendar-exam-slot-list", {
            "school_class": school_class.pk, "subject": self.maths.pk,
            "exam_date": "2025-12-01", "sitting": "MORNING", "room": room.pk,
        }, exam_id=self.exam.pk)

    def test_a_branch_head_schedules_their_own_class_in_a_shared_exam(self):
        response = self.paper(self.ikeja_admin, self.sss2, self.room_c1)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_branch_head_cannot_schedule_a_shared_class(self):
        response = self.paper(self.ikeja_admin, self.pry4a, self.room_c1)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(ExamSlot.all_objects.filter(school_class=self.pry4a).exists())

    def test_another_branchs_papers_in_a_shared_exam_are_not_listed(self):
        created = self.paper(self.admin, self.jss1a, self.room_a1)
        self.assertEqual(created.status_code, 201, created.data)
        mine = self.paper(self.ikeja_admin, self.sss2, self.room_c1)
        self.assertEqual(mine.status_code, 201, mine.data)

        listed = self.get(
            self.ikeja_admin, "calendar-exam-slot-list", exam_id=self.exam.pk,
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual({row["school_class"] for row in listed.data["data"]}, {self.sss2.pk})

        exams = self.get(self.ikeja_admin, "calendar-exam-list").data["data"]
        nested = next(row for row in exams if row["id"] == self.exam.pk)["slots"]
        self.assertEqual({row["school_class"] for row in nested}, {self.sss2.pk})

    def test_a_branch_head_cannot_publish_a_shared_exam(self):
        response = self.post(self.ikeja_admin, "calendar-exam-publish", exam_id=self.exam.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_branchs_exam_and_its_papers_are_not_found(self):
        lekki_period = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki,
            name="Lekki Mocks", event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 11, 3), end_date=dt.date(2025, 11, 7),
        )
        lekki_exam = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=lekki_period, name="Lekki Mocks",
        )
        self.assertEqual(
            self.get(self.ikeja_admin, "calendar-exam-detail", pk=lekki_exam.pk).status_code,
            404,
        )
        self.assertEqual(
            self.get(
                self.ikeja_admin, "calendar-exam-slot-list", exam_id=lekki_exam.pk,
            ).status_code,
            404,
        )
        names = {row["name"] for row in self.get(self.ikeja_admin, "calendar-exam-list").data["data"]}
        self.assertNotIn("Lekki Mocks", names)


class TimetableTests(_SharedBase):
    def lesson(self, user, school_class, room):
        return self.post(user, "calendar-slot-list", {
            "school_class": school_class.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": room.pk,
        })

    def test_a_branch_head_fills_their_own_classs_grid(self):
        response = self.lesson(self.ikeja_admin, self.sss2, self.room_c1)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_shared_classs_grid_is_read_only(self):
        response = self.lesson(self.ikeja_admin, self.pry4a, self.room_c1)
        self.assertEqual(response.status_code, 403, response.data)
        response = self.post(self.ikeja_admin, "calendar-class-clear", class_id=self.pry4a.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_branchs_lesson_is_not_found_by_id(self):
        created = self.lesson(self.admin, self.jss1a, self.room_a1)
        self.assertEqual(created.status_code, 201, created.data)
        slot = TimetableSlot.all_objects.get(school_class=self.jss1a)
        response = self.get(self.ikeja_admin, "calendar-slot-detail", pk=slot.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_class_picker_says_which_grids_the_viewer_may_change(self):
        response = self.get(self.ikeja_admin, "calendar-class-list")
        self.assertEqual(response.status_code, 200, response.data)
        flags = {row["name"]: row["can_manage"] for row in response.data["data"]}
        self.assertFalse(flags["Primary 4 A"])
        self.assertTrue(flags["SSS2 Science"])
