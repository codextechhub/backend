"""Exam scheduling: anchored to the calendar, two refusals, two warnings."""
from __future__ import annotations

import datetime as dt

from ..models import CalendarEvent, EventType, Exam, ExamSlot, PublishState, Sitting
from .base import _Base


class _ExamBase(_Base):
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
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def paper(self, **kwargs):
        body = {
            "school_class": self.jss1a.pk, "subject": self.maths.pk,
            "exam_date": "2025-12-01", "sitting": "MORNING",
            "room": self.room_a1.pk, "invigilator": self.eze.pk,
        }
        body.update(kwargs)
        return self.post(
            self.admin, "calendar-exam-slot-list", body, exam_id=self.exam.pk,
        )


class ExamAnchorTests(_Base):
    def test_an_exam_must_hang_off_an_exam_period(self):
        sports = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Inter-house Sports",
            event_type=EventType.SPORTS,
            start_date=dt.date(2025, 11, 14), end_date=dt.date(2025, 11, 14),
        )
        response = self.post(self.admin, "calendar-exam-list", {
            "calendar_event": sports.pk,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "EXAM_EVENT_NOT_EXAM_PERIOD",
        )

    def test_asking_twice_returns_the_same_exam(self):
        """The design never names an exam: it opens the screen and adds papers."""
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="First Term Exams",
            event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 12),
        )
        first = self.post(
            self.admin, "calendar-exam-list", {"calendar_event": event.pk},
        )
        second = self.post(
            self.admin, "calendar-exam-list", {"calendar_event": event.pk},
        )
        self.assertEqual(first.data["data"]["id"], second.data["data"]["id"])
        self.assertEqual(Exam.all_objects.count(), 1)

    def test_the_exam_period_cannot_be_deleted_while_an_exam_points_at_it(self):
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="First Term Exams",
            event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 12),
        )
        Exam.all_objects.create(
            tenant=self.tenant, calendar_event=event, name="First Term Exams",
        )
        response = self.delete(
            self.admin, "calendar-event-detail", pk=event.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)


class ExamPaperRefusalTests(_ExamBase):
    def test_a_date_outside_the_exam_period_is_refused(self):
        response = self.paper(exam_date="2025-11-28")
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "EXAM_OUTSIDE_EXAM_PERIOD",
        )

    def test_both_ends_of_the_range_are_inclusive(self):
        self.assertEqual(self.paper(exam_date="2025-12-01").status_code, 201)
        self.assertEqual(
            self.paper(exam_date="2025-12-12", subject=self.physics.pk).status_code,
            201,
        )

    def test_a_class_cannot_sit_two_papers_in_one_sitting(self):
        self.assertEqual(self.paper().status_code, 201)
        second = self.paper(subject=self.physics.pk)
        # Physically impossible, and a school never means it: refused by the
        # constraint rather than warned about.
        self.assertIn(second.status_code, (400, 409), second.data)

    def test_the_same_class_may_sit_again_in_the_afternoon(self):
        self.assertEqual(self.paper().status_code, 201)
        self.assertEqual(
            self.paper(subject=self.physics.pk, sitting="AFTERNOON").status_code,
            201,
        )

    def test_a_non_teacher_cannot_invigilate(self):
        from vs_rbac.tests.helpers import make_school_admin

        bursar = make_school_admin(
            None, email="bursar@brightfield.test", tenant=self.tenant,
        )
        response = self.paper(invigilator=bursar.pk)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NOT_A_TEACHING_USER")


class ExamPaperWarningTests(_ExamBase):
    def test_a_room_used_twice_in_one_sitting_warns_and_saves(self):
        """Two classes really can sit in the Main Hall together."""
        self.paper()
        response = self.paper(
            school_class=self.jss1b.pk, room=self.room_a1.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("ROOM_DOUBLE_BOOKED", codes)

    def test_an_invigilator_in_two_rooms_warns_and_saves(self):
        """One person really does float between adjacent rooms."""
        self.paper()
        response = self.paper(
            school_class=self.jss1b.pk, room=self.room_a2.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("INVIGILATOR_DOUBLE_BOOKED", codes)

    def test_a_clean_paper_warns_about_nothing(self):
        response = self.paper()
        self.assertEqual(response.data["data"]["warnings"], [])


class ExamPublishTests(_ExamBase):
    def test_a_clean_schedule_publishes(self):
        self.paper()
        response = self.post(
            self.admin, "calendar-exam-publish", exam_id=self.exam.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            Exam.all_objects.get(pk=self.exam.pk).status, PublishState.PUBLISHED,
        )

    def test_a_room_clash_blocks_publication(self):
        self.paper()
        self.paper(school_class=self.jss1b.pk, room=self.room_a1.pk)
        response = self.post(
            self.admin, "calendar-exam-publish", exam_id=self.exam.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TIMETABLE_HAS_CLASHES")

    def test_an_invigilator_clash_also_blocks_publication(self):
        """It warns on write and blocks here - the two are different moments."""
        self.paper()
        self.paper(school_class=self.jss1b.pk, room=self.room_a2.pk)
        response = self.post(
            self.admin, "calendar-exam-publish", exam_id=self.exam.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_publishing_needs_its_own_key(self):
        from vs_rbac.models import TenantRolePermission

        self.paper()
        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.timetable.publish",
        ).update(granted=False)
        response = self.post(
            self.admin, "calendar-exam-publish", exam_id=self.exam.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_published_schedule_refuses_further_papers(self):
        self.paper()
        self.post(self.admin, "calendar-exam-publish", exam_id=self.exam.pk)
        response = self.paper(sitting="AFTERNOON", subject=self.physics.pk)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "EXAM_PUBLISHED_READ_ONLY",
        )

    def test_publication_notifies_nobody(self):
        """No academics event type exists, and none is emitted."""
        from vs_notifications.models import Notification

        self.paper()
        before = Notification.objects.count()
        self.post(self.admin, "calendar-exam-publish", exam_id=self.exam.pk)
        self.assertEqual(Notification.objects.count(), before)


class ExamShapeTests(_ExamBase):
    def test_sittings_order_by_time_of_day_not_by_name(self):
        """"AFTERNOON" sorts before "MORNING" lexically, which would invert a day."""
        self.paper(sitting="AFTERNOON", subject=self.physics.pk)
        self.paper(sitting="MORNING")
        response = self.get(
            self.admin, "calendar-exam-slot-list", exam_id=self.exam.pk,
        )
        sittings = [row["sitting"] for row in response.data["data"]]
        self.assertEqual(sittings, [Sitting.MORNING, Sitting.AFTERNOON])

    def test_no_response_carries_a_count_of_people(self):
        self.paper()
        response = self.get(self.admin, "calendar-exam-list")
        blob = str(response.data).lower()
        for word in ("candidate", "students", "seats", "utilisation", "utilization"):
            self.assertNotIn(word, blob)

    def test_the_dates_come_from_the_event_and_are_never_copied(self):
        response = self.get(self.admin, "calendar-exam-list")
        row = response.data["data"][0]
        self.assertEqual(str(row["start_date"]), "2025-12-01")
        self.assertEqual(str(row["end_date"]), "2025-12-12")

        self.period_event.end_date = dt.date(2025, 12, 15)
        self.period_event.save(update_fields=["end_date"])
        response = self.get(self.admin, "calendar-exam-list")
        # The exam follows the calendar, because it never had its own copy.
        self.assertEqual(str(response.data["data"][0]["end_date"]), "2025-12-15")


class ExamSecurityTests(_ExamBase):
    def test_another_tenants_exam_answers_404(self):
        theirs_event = CalendarEvent.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, name="Theirs",
            event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 12),
        )
        theirs = Exam.all_objects.create(
            tenant=self.other.tenant, calendar_event=theirs_event, name="Theirs",
        )
        response = self.get(self.admin, "calendar-exam-detail", pk=theirs.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_removing_a_paper_needs_the_manage_key(self):
        from vs_rbac.models import TenantRolePermission

        created = self.paper()
        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.timetable.manage",
        ).update(granted=False)
        response = self.delete(
            self.admin, "calendar-exam-slot-detail",
            exam_id=self.exam.pk, pk=created.data["data"]["id"],
        )
        self.assertEqual(response.status_code, 403, response.data)
