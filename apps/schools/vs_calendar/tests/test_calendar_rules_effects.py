"""What each calendar and timetable setting changes, on the screens that read it.

Brightfield runs Lekki and Ikeja, teaches Monday to Friday until it says
otherwise, and rings a school-wide everyday bell of Period 1, Period 2 and a
break. Mr Eze (Chukwuemeka Eze) teaches Mathematics to JSS1 A at Lekki. Mrs
Okafor (Chioma Okafor) is a teacher with no teaching duty for it.
"""
from __future__ import annotations

import datetime as dt

from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from ..models import (
    CalendarEvent,
    EventType,
    Exam,
    Period,
    PeriodType,
    PublishState,
    TimetableSlot,
)
from .base import _Base


class _EffectsBase(_Base):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        cls.okafor = cls.make_teacher("okafor@brightfield.test", "Chioma", "Okafor")
        cls.okafor.gender = "FEMALE"
        cls.okafor.save(update_fields=["gender"])

    def configure(self, **values):
        """Store settings for Brightfield the way the settings screen would."""
        keys = {
            "teaching_days": "calendar.teaching_days",
            "week_starts_on": "calendar.week_starts_on",
            "closes_school_by_type": "calendar.closes_school_by_type",
            "room_required_to_publish": "timetable.room_required_to_publish",
            "teacher_duty_match": "timetable.teacher_duty_match",
            "invigilator_roles": "exams.invigilator_roles",
        }
        for name, value in values.items():
            set_value(
                definition=ConfigurationDefinition.objects.get(key=keys[name]),
                value=value, actor=None, tenant=self.tenant,
            )

    def lesson(self, **overrides):
        body = {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        }
        body.update(overrides)
        return self.post(self.admin, "calendar-slot-list", body)

    def give_duty(self, user, school_class=None, subject=None):
        from schools.vs_staff.constants import EmploymentStatus, TeachingPart
        from schools.vs_staff.models import StaffProfile, TeachingAssignment

        profile = StaffProfile.all_objects.filter(user=user).first() or (
            StaffProfile.all_objects.create(
                tenant=self.tenant, user=user, branch=None,
                employment_status=EmploymentStatus.ACTIVE,
            )
        )
        return TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=profile,
            school_class=school_class or self.jss1a, subject=subject or self.maths,
            session=self.year, part=TeachingPart.LEAD,
        )

    def publish(self, school_class=None):
        return self.post(
            self.admin, "calendar-class-publish",
            class_id=(school_class or self.jss1a).pk,
        )


# ── teaching days ───────────────────────────────────────────────────────────

class TeachingDayTests(_EffectsBase):

    def days_of(self, response):
        return [
            (day["day_of_week"], day["day_label"], day["is_teaching_day"])
            for day in response.data["data"]["days"]
        ]

    def test_a_school_that_has_set_nothing_draws_monday_to_friday(self):
        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        self.assertEqual(self.days_of(grid), [
            (1, "Monday", True), (2, "Tuesday", True), (3, "Wednesday", True),
            (4, "Thursday", True), (5, "Friday", True),
        ])

    def test_a_saturday_school_gets_a_saturday_column_on_both_grids(self):
        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        self.assertEqual(self.days_of(grid)[-1], (6, "Saturday", True))
        self.assertEqual(len(grid.data["data"]["days"]), 6)
        week = self.get(self.admin, "calendar-teacher-grid", user_id=self.eze.pk)
        self.assertEqual(self.days_of(week)[-1], (6, "Saturday", True))

    def test_a_sunday_start_school_draws_sunday_first(self):
        self.configure(teaching_days=[1, 2, 3, 4, 7], week_starts_on=7)
        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        self.assertEqual(
            [day for day, _, _ in self.days_of(grid)], [7, 1, 2, 3, 4],
        )

    def test_a_lesson_on_a_day_the_school_does_not_teach_is_refused(self):
        response = self.lesson(day_of_week=6)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DAY_NOT_TAUGHT")
        self.assertEqual(
            response.data["message"],
            "Saturday is not one of the school's teaching days, so no lesson can "
            "be scheduled on it. Add Saturday to the teaching days in Settings, "
            "Calendar and timetables first.",
        )
        self.assertFalse(TimetableSlot.objects.filter(day_of_week=6).exists())

    def test_the_grid_save_and_a_move_refuse_it_too(self):
        response = self.put(
            self.admin, "calendar-class-grid", {"slots": [{
                "school_class": self.jss1a.pk, "day_of_week": 6,
                "period": self.p1.pk, "subject": self.maths.pk,
            }]},
            class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DAY_NOT_TAUGHT")
        slot = self.lesson().data["data"]
        moved = self.patch(
            self.admin, "calendar-slot-detail", {"day_of_week": 7}, pk=slot["id"],
        )
        self.assertEqual(moved.status_code, 422, moved.data)
        self.assertEqual(moved.data["error"]["code"], "DAY_NOT_TAUGHT")

    def test_a_saturday_school_may_schedule_saturday(self):
        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        self.assertEqual(self.lesson(day_of_week=6).status_code, 201)

    def test_a_lesson_left_on_a_day_no_longer_taught_is_drawn_not_hidden(self):
        """It still counts in clashes and at the gate, so the school must be able to see it.

        Brightfield taught Saturdays, put Mathematics on JSS1 A's Saturday,
        then stopped. The lesson stays; both grids draw Saturday, flagged, so
        it can be found and cleared.
        """
        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        self.assertEqual(self.lesson(day_of_week=6).status_code, 201)
        self.configure(teaching_days=[1, 2, 3, 4, 5])

        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        saturday = grid.data["data"]["days"][-1]
        self.assertEqual(
            (saturday["day_of_week"], saturday["is_teaching_day"]), (6, False),
        )
        held = [cell["slot"] for cell in saturday["cells"] if cell.get("slot")]
        self.assertEqual([slot["subject_name"] for slot in held], ["Mathematics"])
        week = self.get(self.admin, "calendar-teacher-grid", user_id=self.eze.pk)
        self.assertEqual(week.data["data"]["days"][-1]["day_of_week"], 6)
        # Another class with nothing on Saturday draws the school's five days.
        other = self.get(self.admin, "calendar-class-grid", class_id=self.jss1b.pk)
        self.assertEqual(len(other.data["data"]["days"]), 5)

    def test_a_period_for_a_day_the_school_does_not_teach_is_refused(self):
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Saturday Prep", "period_type": "LESSON",
            "start_time": "09:00", "end_time": "10:00", "day_of_week": 6,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DAY_NOT_TAUGHT")
        self.assertEqual(
            response.data["message"],
            "Saturday is not one of the school's teaching days, so no period can "
            "be set for it. Add Saturday to the teaching days in Settings, "
            "Calendar and timetables first.",
        )
        moved = self.patch(
            self.admin, "calendar-period-detail", {"day_of_week": 6}, pk=self.p1.pk,
        )
        self.assertEqual(moved.data["error"]["code"], "DAY_NOT_TAUGHT")

    def overview_term(self, on="2025-10-15"):
        return self.get(self.admin, "calendar-overview", {"on": on}).data["data"]["term"]

    @staticmethod
    def count(start, end, days):
        total, day = 0, start
        while day <= end:
            total += day.isoweekday() in days
            day += dt.timedelta(days=1)
        return total

    def test_the_overview_counts_the_schools_teaching_days(self):
        start, end = self.first_term.start_date, self.first_term.end_date
        on = dt.date(2025, 10, 15)

        term = self.overview_term()
        self.assertEqual(term["teaching_days_total"], self.count(start, end, {1, 2, 3, 4, 5}))
        self.assertEqual(
            term["teaching_days_elapsed"], self.count(start, on, {1, 2, 3, 4, 5}),
        )

        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        term = self.overview_term()
        self.assertEqual(
            term["teaching_days_total"], self.count(start, end, {1, 2, 3, 4, 5, 6}),
        )
        self.assertEqual(
            term["teaching_days_elapsed"], self.count(start, on, {1, 2, 3, 4, 5, 6}),
        )

    def test_a_closed_saturday_is_not_taught(self):
        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        before = self.overview_term()["teaching_days_total"]
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Founders' Day",
            event_type=EventType.HOLIDAY, closes_school=True,
            start_date=dt.date(2025, 10, 4), end_date=dt.date(2025, 10, 4),
        )
        self.assertEqual(self.overview_term()["teaching_days_total"], before - 1)

    def test_duplicating_a_week_skips_a_day_no_longer_taught(self):
        self.configure(teaching_days=[1, 2, 3, 4, 5, 6])
        self.lesson(day_of_week=6)
        self.lesson(day_of_week=1)
        self.configure(teaching_days=[1, 2, 3, 4, 5])
        response = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk, "keep_rooms": False},
            class_id=self.jss1b.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            (response.data["data"]["copied"], response.data["data"]["skipped"]), (1, 1),
        )
        self.assertEqual(
            list(
                TimetableSlot.objects.filter(school_class=self.jss1b)
                .values_list("day_of_week", flat=True),
            ),
            [1],
        )


# ── closes the school ───────────────────────────────────────────────────────

class ClosesSchoolDefaultTests(_EffectsBase):

    def create(self, event_type, **extra):
        body = {
            "name": f"A {event_type.lower()} day", "event_type": event_type,
            "start_date": "2025-10-01", "end_date": "2025-10-01", **extra,
        }
        response = self.post(self.admin, "calendar-event-list", body)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]["closes_school"]

    def test_an_entry_that_does_not_say_takes_its_types_default(self):
        self.assertTrue(self.create("HOLIDAY"))
        self.assertTrue(self.create("MIDTERM_BREAK"))
        for kind in ("EXAM_PERIOD", "SCHOOL_EVENT", "PTA", "SPORTS"):
            self.assertFalse(self.create(kind), kind)

    def test_an_entry_that_says_wins(self):
        self.assertFalse(self.create("HOLIDAY", closes_school=False))
        self.assertTrue(self.create("SPORTS", closes_school=True))

    def test_the_schools_own_answer_is_used_and_existing_entries_never_change(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Inter-house Sports",
            event_type=EventType.SPORTS, closes_school=False,
            start_date=dt.date(2025, 11, 14), end_date=dt.date(2025, 11, 14),
        )
        self.configure(closes_school_by_type={
            "HOLIDAY": False, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
            "SCHOOL_EVENT": False, "PTA": False, "SPORTS": True,
        })
        self.assertTrue(self.create("SPORTS"))
        self.assertFalse(self.create("HOLIDAY"))
        self.assertFalse(
            CalendarEvent.objects.get(name="Inter-house Sports").closes_school,
        )

    def test_an_imported_row_with_a_blank_cell_takes_its_types_default(self):
        from ..imports import resolve_row

        def closes(event_type, cell=""):
            row = resolve_row(
                {
                    "name": "Row", "event_type": event_type,
                    "start_date": "2025-10-01", "end_date": "2025-10-01",
                    "closes_school": cell,
                },
                tenant=self.tenant, session=self.year,
                batch_branch=None, multi_branch=True,
            )
            self.assertTrue(row.ok, [i.message for i in row.issues])
            return row.closes_school

        self.assertTrue(closes("Public holiday"))
        self.assertTrue(closes("Mid-term break"))
        self.assertFalse(closes("Sports day"))
        self.assertFalse(closes("Public holiday", "No"))
        self.assertTrue(closes("Sports day", "Yes"))

        self.configure(closes_school_by_type={
            "HOLIDAY": True, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
            "SCHOOL_EVENT": False, "PTA": False, "SPORTS": True,
        })
        self.assertTrue(closes("Sports day"))

    def test_an_unreadable_cell_is_still_refused_in_a_sentence(self):
        from ..imports import resolve_row

        row = resolve_row(
            {
                "name": "Row", "event_type": "Public holiday",
                "start_date": "2025-10-01", "end_date": "2025-10-01",
                "closes_school": "maybe",
            },
            tenant=self.tenant, session=self.year,
            batch_branch=None, multi_branch=True,
        )
        self.assertEqual(
            [i.message for i in row.issues],
            [
                "'maybe' is not yes or no. Write Yes if the school is shut on "
                "these days, No if it is open, or leave it blank for the "
                "school's usual answer for this kind of entry.",
            ],
        )


# ── publishing without a room ───────────────────────────────────────────────

class RoomRequiredTests(_EffectsBase):

    def test_by_default_a_lesson_needs_a_room_to_publish(self):
        self.lesson(room=None)
        response = self.publish()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TIMETABLE_INCOMPLETE")
        self.assertEqual(
            response.data["message"],
            "1 lesson has no teacher or room yet. Fill it in and publish again.",
        )
        self.assertEqual(
            response.data["error"]["detail"]["items"], ["Monday Period 1 - Mathematics has no room."],
        )

    def test_a_school_that_does_not_need_rooms_publishes_without_them(self):
        self.configure(room_required_to_publish=False)
        self.lesson(room=None)
        response = self.publish()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], PublishState.PUBLISHED)
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_a_lesson_still_needs_a_teacher(self):
        self.configure(room_required_to_publish=False)
        self.lesson(teacher=None, room=None)
        response = self.publish()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["message"],
            "1 lesson has no teacher yet. Fill it in and publish again.",
        )
        self.assertEqual(
            response.data["error"]["detail"]["items"],
            ["Monday Period 1 - Mathematics has no teacher."],
        )


# ── the teaching duty ───────────────────────────────────────────────────────

class DutyMatchTests(_EffectsBase):

    NO_DUTY = "Chioma Okafor has no teaching duty for JSS1 A Mathematics."

    def codes(self, response):
        return [w["code"] for w in response.data["data"]["warnings"]]

    def test_off_lets_any_teacher_take_any_lesson(self):
        response = self.lesson(teacher=self.okafor.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["warnings"], [])
        self.assertEqual(self.publish().status_code, 200)

    def test_warn_saves_and_says_so(self):
        self.configure(teacher_duty_match="WARN")
        response = self.lesson(teacher=self.okafor.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["warnings"], [{
            "code": "TEACHER_HAS_NO_DUTY", "detail": self.NO_DUTY,
            "slot_ids": [response.data["data"]["id"]],
        }])
        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        self.assertEqual(self.codes(grid), ["TEACHER_HAS_NO_DUTY"])

    def test_warn_is_silent_for_a_teacher_with_the_duty(self):
        self.configure(teacher_duty_match="WARN")
        self.give_duty(self.eze)
        response = self.lesson()
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_warn_on_the_preview_and_the_move(self):
        self.configure(teacher_duty_match="WARN")
        preview = self.post(self.admin, "calendar-slot-preview", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.okafor.pk,
        })
        self.assertEqual(preview.data["data"]["warnings"], [{
            "code": "TEACHER_HAS_NO_DUTY", "detail": self.NO_DUTY, "slot_ids": [],
        }])
        self.give_duty(self.eze)
        slot = self.lesson().data["data"]
        moved = self.patch(
            self.admin, "calendar-slot-detail", {"teacher": self.okafor.pk}, pk=slot["id"],
        )
        self.assertEqual(self.codes(moved), ["TEACHER_HAS_NO_DUTY"])

    def test_warn_publishes_and_lists_the_lesson(self):
        self.configure(teacher_duty_match="WARN")
        slot = self.lesson(teacher=self.okafor.pk).data["data"]
        response = self.publish()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["warnings"], [{
            "code": "TEACHER_HAS_NO_DUTY", "detail": self.NO_DUTY,
            "slot_ids": [slot["id"]],
        }])

    def test_refuse_refuses_the_save_and_names_the_colleague_who_has_it(self):
        self.configure(teacher_duty_match="REFUSE")
        self.give_duty(self.eze)
        response = self.lesson(teacher=self.okafor.pk)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NO_TEACHING_DUTY")
        self.assertEqual(
            response.data["message"],
            "Chioma Okafor has no teaching duty for JSS1 A Mathematics. Give her "
            "the duty in Teaching duties first, or choose Chukwuemeka Eze, who has it.",
        )
        self.assertFalse(TimetableSlot.objects.filter(school_class=self.jss1a).exists())
        self.assertEqual(self.lesson().status_code, 201)

    def test_refuse_with_nobody_holding_the_duty(self):
        self.configure(teacher_duty_match="REFUSE")
        response = self.lesson()
        self.assertEqual(
            response.data["message"],
            "Chukwuemeka Eze has no teaching duty for JSS1 A Mathematics. Give "
            "them the duty in Teaching duties first.",
        )

    def test_refuse_refuses_the_whole_grid_save(self):
        self.configure(teacher_duty_match="REFUSE")
        response = self.put(
            self.admin, "calendar-class-grid", {"slots": [{
                "school_class": self.jss1a.pk, "day_of_week": 1,
                "period": self.p1.pk, "subject": self.maths.pk,
                "teacher": self.okafor.pk,
            }]},
            class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NO_TEACHING_DUTY")

    def test_refuse_blocks_publishing_when_a_duty_is_withdrawn_after_the_save(self):
        self.configure(teacher_duty_match="REFUSE")
        duty = self.give_duty(self.eze)
        slot = self.lesson().data["data"]
        duty.delete()
        response = self.publish()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "TIMETABLE_TEACHER_HAS_NO_DUTY",
        )
        self.assertEqual(
            response.data["message"],
            "1 lesson has a teacher with no teaching duty for it. Give the duty "
            "in Teaching duties, or change the teacher, and publish again.",
        )
        self.assertEqual(response.data["error"]["detail"]["items"], [
            "Monday Period 1 - Mathematics: Chukwuemeka Eze has no teaching duty "
            "for JSS1 A Mathematics.",
        ])
        self.assertEqual(response.data["error"]["detail"]["slot_ids"], [slot["id"]])
        grid = self.get(self.admin, "calendar-class-grid", class_id=self.jss1a.pk)
        self.assertEqual(self.codes(grid), ["TEACHER_HAS_NO_DUTY"])

    def test_a_copied_teacher_is_judged_against_the_target_class(self):
        self.give_duty(self.eze)
        self.lesson()
        self.configure(teacher_duty_match="REFUSE")
        preview = self.post(
            self.admin, "calendar-class-duplicate", {"source_class": self.jss1a.pk},
            params={"preview": 1}, class_id=self.jss1b.pk,
        )
        self.assertEqual(
            [w["detail"] for w in preview.data["data"]["warnings"]],
            ["Chukwuemeka Eze has no teaching duty for JSS1 B Mathematics."],
        )
        refused = self.post(
            self.admin, "calendar-class-duplicate", {"source_class": self.jss1a.pk},
            class_id=self.jss1b.pk,
        )
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "NO_TEACHING_DUTY")
        self.assertFalse(TimetableSlot.objects.filter(school_class=self.jss1b).exists())
        without = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk, "keep_teachers": False},
            class_id=self.jss1b.pk,
        )
        self.assertEqual(without.status_code, 200, without.data)


# ── invigilators ────────────────────────────────────────────────────────────

class InvigilatorRoleTests(_EffectsBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_rbac.tests.helpers import make_assignment, make_role, make_school_admin

        cls.bursar_role = make_role(cls.school, name="Bursar", key="bursar")
        cls.bursar = make_school_admin(
            None, email="bursar@brightfield.test", tenant=cls.tenant,
            first_name="Bola", last_name="Adeyemi",
        )
        make_assignment(cls.school, cls.bursar, cls.bursar_role, branch=None)
        # Kunle teaches at Lekki only; Ikeja's head never sees him in the picker.
        cls.kunle = cls.make_teacher(
            "kunle@brightfield.test", "Kunle", "Bakare", branch=cls.lekki,
        )

    def setUp(self):
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year,
            name="First Term Examinations", event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 12),
        )
        self.exam = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=event, name="First Term Examinations",
        )

    def paper(self, invigilator):
        return self.post(self.admin, "calendar-exam-slot-list", {
            "school_class": self.jss1a.pk, "subject": self.maths.pk,
            "exam_date": "2025-12-01", "sitting": "MORNING",
            "room": self.room_a1.pk, "invigilator": invigilator.pk,
        }, exam_id=self.exam.pk)

    def listed(self, user):
        response = self.get(user, "calendar-exam-invigilators")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_by_default_only_teachers_invigilate(self):
        self.assertEqual(self.paper(self.eze).status_code, 201)
        response = self.paper(self.bursar)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NOT_AN_INVIGILATOR")
        self.assertEqual(
            response.data["message"],
            "Bola Adeyemi does not hold a role whose holders may invigilate at "
            "this school (Teacher). Choose someone who does, or add their role "
            "in Settings, Calendar and timetables.",
        )

    def test_a_school_that_adds_the_bursar_role_may_use_the_bursar(self):
        self.configure(invigilator_roles=["teacher", "bursar"])
        self.assertEqual(self.paper(self.bursar).status_code, 201)

    def test_a_school_that_drops_the_teacher_role_refuses_teachers(self):
        self.configure(invigilator_roles=["bursar"])
        self.assertEqual(self.paper(self.eze).status_code, 422)

    def test_the_picker_lists_everyone_who_may_invigilate(self):
        self.assertEqual(self.listed(self.admin), [
            {"id": self.okafor.pk, "name": "Chioma Okafor", "role_label": "Teacher"},
            {"id": self.eze.pk, "name": "Chukwuemeka Eze", "role_label": "Teacher"},
            {"id": self.kunle.pk, "name": "Kunle Bakare", "role_label": "Teacher"},
        ])
        self.configure(invigilator_roles=["teacher", "bursar"])
        names = [row["name"] for row in self.listed(self.admin)]
        self.assertEqual(names[0], "Bola Adeyemi")
        self.assertEqual(self.listed(self.admin)[0]["role_label"], "Bursar")

    def test_the_picker_is_narrowed_to_a_branch_bound_callers_branches(self):
        """Ikeja's head sees the school-wide teachers and not Kunle, who is Lekki's."""
        names = [row["name"] for row in self.listed(self.ikeja_admin)]
        self.assertEqual(names, ["Chioma Okafor", "Chukwuemeka Eze"])

    def test_the_picker_needs_the_exam_view_key(self):
        response = self.get(self.bursar, "calendar-exam-invigilators")
        self.assertEqual(response.status_code, 403, response.data)


# ── copying a bell schedule ─────────────────────────────────────────────────

class BellScheduleCopyTests(_EffectsBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_academics.models import AcademicSession

        cls.next_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2026/2027",
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2027, 7, 16),
            status="DRAFT",
        )
        # Ikeja rings its own bell, and Friday runs its own short day.
        cls.ikeja_p1 = Period.all_objects.create(
            tenant=cls.tenant, session=cls.year, branch=cls.ikeja, order_index=1,
            label="Ikeja Period 1", period_type=PeriodType.LESSON,
            start_time=dt.time(7, 45), end_time=dt.time(8, 30),
        )
        cls.friday = Period.all_objects.create(
            tenant=cls.tenant, session=cls.year, day_of_week=5, order_index=1,
            label="Friday Period 1", period_type=PeriodType.LESSON,
            start_time=dt.time(8, 0), end_time=dt.time(8, 40), is_active=False,
        )

    def copy(self, user=None, into=None, **body):
        body.setdefault("from_session", self.year.pk)
        return self.post(
            user or self.admin, "calendar-period-copy", body,
            params={"session": (into or self.next_year).pk},
        )

    def shape(self, session):
        return sorted(
            (p.label, p.branch_id, p.day_of_week, p.order_index, p.start_time,
             p.end_time, p.period_type, p.is_active)
            for p in Period.all_objects.filter(session=session)
        )

    def test_every_period_is_copied_as_it_stands(self):
        response = self.copy()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["message"], "5 periods copied from 2025/2026 into 2026/2027.")
        self.assertEqual(response.data["data"]["copied"], 5)
        self.assertEqual(
            sorted(p["label"] for p in response.data["data"]["periods"]),
            ["Break", "Friday Period 1", "Ikeja Period 1", "Period 1", "Period 2"],
        )
        self.assertEqual(self.shape(self.next_year), self.shape(self.year))

    def test_a_year_that_already_has_periods_is_refused(self):
        self.copy()
        response = self.copy()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "BELL_SCHEDULE_NOT_EMPTY")
        self.assertEqual(
            response.data["message"],
            "2026/2027 already has periods, so nothing was copied. Copying fills "
            "an empty bell schedule: change 2026/2027's periods on the Bell "
            "schedule instead.",
        )
        self.assertEqual(Period.all_objects.filter(session=self.next_year).count(), 5)

    def test_the_source_must_be_another_year_of_this_school(self):
        response = self.copy(into=self.year)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["error"]["detail"]["from_session"],
            "A year's bell schedule cannot be copied into itself. Choose an earlier year.",
        )
        missing = self.copy(from_session=None)
        self.assertEqual(
            missing.data["error"]["detail"]["from_session"],
            "Say which year to copy the bell schedule from.",
        )
        foreign = self.copy(from_session=self.other_year.pk)
        self.assertEqual(foreign.status_code, 404, foreign.data)
        self.assertFalse(Period.all_objects.filter(session=self.next_year).exists())

    def test_an_archived_year_is_refused_the_way_every_write_is(self):
        type(self.next_year).all_objects.filter(pk=self.next_year.pk).update(
            status="ARCHIVED",
        )
        response = self.copy()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY")
        self.assertFalse(Period.all_objects.filter(session=self.next_year).exists())

    def test_a_branch_bound_caller_copies_only_their_branchs_periods(self):
        response = self.copy(user=self.ikeja_admin)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [p.label for p in Period.all_objects.filter(session=self.next_year)],
            ["Ikeja Period 1"],
        )
        again = self.copy(user=self.ikeja_admin)
        self.assertEqual(again.status_code, 409, again.data)
        self.assertEqual(
            again.data["message"],
            "2026/2027 already has periods at your branch, so nothing was copied. "
            "Copying fills an empty bell schedule: change 2026/2027's periods on "
            "the Bell schedule instead.",
        )

    def test_a_branch_bound_caller_with_nothing_of_theirs_to_copy_is_told_why(self):
        Period.all_objects.filter(pk=self.ikeja_p1.pk).delete()
        response = self.copy(user=self.ikeja_admin)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BELL_SCHEDULE_EMPTY")
        self.assertEqual(
            response.data["message"],
            "2025/2026 has no periods at your branch to copy. The school's shared "
            "periods are copied by a school-wide administrator.",
        )

    def test_copying_needs_the_key_that_adds_a_period(self):
        response = self.copy(user=self.eze)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Period.all_objects.filter(session=self.next_year).exists())
