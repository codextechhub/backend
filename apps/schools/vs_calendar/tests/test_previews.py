"""Telling a school about a clash before it saves one.

The form asks for a teacher and a room, and the moment both are chosen the
school can already be told that Mr Eze is teaching JSS2 A at that hour. The
alternative was working it out on the client, and these tests exist mostly to
pin down why that would have been wrong.

The clash rules are not simple. They span the whole tenant on purpose, because a
person cannot be at two branches at once. They redact the other side of a clash
the caller may not see, naming neither the class nor the room. A client copy
would get the width wrong, the redaction wrong, or both - and would drift from
the real engine the first time either changed.

So the property under test is not "the preview finds clashes". It is **the
preview and the save give the same answer**, because they are one function
called twice.
"""
from __future__ import annotations

import datetime as dt

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_assignment, make_permission, make_role_permission

from ..models import ExamSlot, Sitting, TimetableSlot
from .base import _Base


class SlotPreviewTests(_Base):
    """One lesson cell, before it is written."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.make_teacher("eze@brightfield.test", "Ngozi", "Eze")
        # JSS1 B already has Mr Eze in Monday Period 1, in Block A Room 1.
        cls.taken = TimetableSlot.all_objects.create(
            tenant=cls.tenant, session=cls.year, school_class=cls.jss1b,
            day_of_week=1, period=cls.p1, subject=cls.physics,
            teacher=cls.eze, room=cls.room_a1,
        )

    def preview(self, user=None, **over):
        body = {
            "school_class": self.jss1a.pk,
            "day_of_week": 1,
            "period": self.p1.pk,
            "subject": self.maths.pk,
            "teacher": self.eze.pk,
            "room": self.room_a2.pk,
        }
        body.update(over)
        return self.post(user or self.admin, "calendar-slot-preview", body)

    def codes(self, response):
        return [w["code"] for w in response.json()["data"]["warnings"]]

    def test_a_clean_cell_previews_clean(self):
        response = self.preview(teacher=None, room=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.codes(response), [])

    def test_it_names_the_teacher_who_is_already_busy(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        warnings = response.json()["data"]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Ngozi Eze", warnings[0]["detail"])
        self.assertIn("JSS1 B", warnings[0]["detail"])

    def test_it_finds_the_room_as_well_as_the_person(self):
        response = self.preview(room=self.room_a1.pk)
        self.assertEqual(len(response.json()["data"]["warnings"]), 2)

    def test_nothing_is_written(self):
        """The point of a preview. Asserted rather than assumed."""
        before = TimetableSlot.all_objects.count()
        self.preview()
        self.assertEqual(TimetableSlot.all_objects.count(), before)

    def test_the_preview_and_the_save_agree(self):
        """The property the whole design rests on.

        They are the same function called twice, so this is a guard against
        somebody later "optimising" one of the two paths into its own copy.
        """
        previewed = self.preview().json()["data"]["warnings"]
        saved = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1, "period": self.p1.pk,
            "subject": self.maths.pk, "teacher": self.eze.pk,
            "room": self.room_a2.pk,
        })
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(
            [w["code"] for w in previewed],
            [w["code"] for w in saved.json()["data"]["warnings"]],
        )
        self.assertEqual(
            [w["detail"] for w in previewed],
            [w["detail"] for w in saved.json()["data"]["warnings"]],
        )

    def test_editing_a_cell_is_not_told_it_clashes_with_itself(self):
        """Without `exclude`, re-previewing a saved cell reports the cell."""
        loud = self.preview(
            school_class=self.jss1b.pk, room=self.room_a1.pk,
        )
        self.assertEqual(len(loud.json()["data"]["warnings"]), 2)
        quiet = self.preview(
            school_class=self.jss1b.pk, room=self.room_a1.pk,
            exclude=self.taken.pk,
        )
        self.assertEqual(quiet.json()["data"]["warnings"], [])

    def test_it_redacts_a_branch_the_caller_cannot_see(self):
        """The redaction is the sharpest reason this is not client-side.

        The Ikeja admin must be told Mr Eze is busy - otherwise they schedule a
        class of thirty into an empty room - and must NOT be told which class or
        which room, or the endpoint maps Lekki's grid for them.
        """
        response = self.preview(
            user=self.ikeja_admin, school_class=self.sss2.pk, room=None,
        )
        self.assertEqual(response.status_code, 200)
        warnings = response.json()["data"]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("another branch", warnings[0]["detail"])
        self.assertNotIn("JSS1 B", warnings[0]["detail"])
        self.assertNotIn("Block A Room 1", warnings[0]["detail"])

    def test_a_reader_who_may_not_write_may_not_preview(self):
        """A preview answers "who is where", so it carries the write key.

        Otherwise it is a way for somebody with view-only access to walk the
        whole school's staffing one request at a time.
        """
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import make_role, make_school_admin

        role = make_role(self.school, name="Timetable Reader", key="tt_reader")
        make_role_permission(
            role, make_permission("academics.timetable.view", scope=PermissionScope.TENANT),
        )
        reader = make_school_admin(None, email="reader@brightfield.test", tenant=self.tenant)
        make_assignment(self.school, reader, role, branch=None)
        self.assertTrue(TenantRoleTemplate.objects.filter(pk=role.pk).exists())

        self.assertEqual(self.preview(user=reader).status_code, 403)


class ExamSlotPreviewTests(_Base):
    """One exam paper, before it is written."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from ..models import CalendarEvent, Exam

        cls.eze = cls.make_teacher("eze2@brightfield.test", "Ngozi", "Eze")
        cls.period_event = CalendarEvent.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=None,
            name="First Term Examinations", event_type="EXAM_PERIOD",
            start_date=dt.date(2025, 11, 3), end_date=dt.date(2025, 11, 14),
        )
        cls.exam = Exam.all_objects.create(
            tenant=cls.tenant, calendar_event=cls.period_event,
            name="First Term Examinations",
        )
        cls.sat = ExamSlot.all_objects.create(
            tenant=cls.tenant, exam=cls.exam, school_class=cls.jss1b,
            subject=cls.physics, exam_date=dt.date(2025, 11, 3),
            sitting=Sitting.MORNING, room=cls.room_a1, invigilator=cls.eze,
        )

    def preview(self, **over):
        body = {
            "school_class": self.jss1a.pk,
            "subject": self.maths.pk,
            "exam_date": "2025-11-03",
            "sitting": Sitting.MORNING,
            "room": self.room_a1.pk,
        }
        body.update(over)
        return self.post(
            self.admin, "calendar-exam-slot-preview", body, exam_id=self.exam.pk,
        )

    def test_it_reports_the_room_already_taken_for_that_sitting(self):
        data = self.preview().json()["data"]
        self.assertIsNone(data["refusal"])
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("Block A Room 1", data["warnings"][0]["detail"])

    def test_a_date_outside_the_exam_period_comes_back_as_a_refusal(self):
        """Not a warning. A form that offered "add anyway" here would offer
        something the server will not do."""
        data = self.preview(exam_date="2025-12-01").json()["data"]
        self.assertIsNotNone(data["refusal"])
        self.assertIn("First Term Examinations", data["refusal"])
        self.assertEqual(data["warnings"], [])

    def test_a_class_sitting_twice_is_a_refusal_not_a_warning(self):
        data = self.preview(school_class=self.jss1b.pk).json()["data"]
        self.assertIsNotNone(data["refusal"])
        self.assertEqual(data["warnings"], [])

    def test_nothing_is_written(self):
        before = ExamSlot.all_objects.count()
        self.preview()
        self.assertEqual(ExamSlot.all_objects.count(), before)

    def test_the_preview_and_the_save_agree(self):
        previewed = self.preview().json()["data"]["warnings"]
        saved = self.post(self.admin, "calendar-exam-slot-list", {
            "school_class": self.jss1a.pk, "subject": self.maths.pk,
            "exam_date": "2025-11-03", "sitting": Sitting.MORNING,
            "room": self.room_a1.pk,
        }, exam_id=self.exam.pk)
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(
            [w["detail"] for w in previewed],
            [w["detail"] for w in saved.json()["data"]["warnings"]],
        )

    def test_editing_a_paper_is_not_told_it_clashes_with_itself(self):
        quiet = self.preview(school_class=self.jss1b.pk, exclude=self.sat.pk)
        data = quiet.json()["data"]
        self.assertIsNone(data["refusal"])
        self.assertEqual(data["warnings"], [])
