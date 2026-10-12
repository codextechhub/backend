"""Which lessons a day has, and what reading one shows.

Monday 13 October 2025 at Brightfield (lesson marking on for JSS1) and Sunrise
(on for Basic 3). A lesson exists only when the class's level marks lessons, its
timetable is published, the school is open to it and the timetable has a
teaching period for the weekday.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_calendar.models import Period, PublishState, TimetableSlot
from schools.vs_students.models import ClassEnrolment

from ..models import LessonRegister
from .base import FOUNDERS_DAY, LEKKI_OPEN_DAY, TODAY
from .lesson_base import LessonFixture, make_slot, publish

TOMORROW = dt.date(2025, 10, 14)


class LessonsListTests(LessonFixture):
    def list(self, user=None, **params):
        response = self.lessons(user or self.admin, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def test_the_days_lessons_come_by_start_time_then_class(self):
        body = self.list()
        self.assertTrue(body["lesson_marking_on"])
        self.assertEqual(body["closed"], None)
        self.assertEqual(
            [(r["start_time"], r["school_class"]["name"], r["subject"]) for r in body["lessons"]],
            [
                ("08:00", "JSS1 A", "English Language"),
                ("08:00", "JSS1 B", "Mathematics"),
                ("08:00", "SSS2 Science", "English Language"),
                ("08:45", "JSS1 A", "Mathematics"),
            ],
        )

    def test_a_row_names_the_lesson_its_teacher_and_where_its_register_stands(self):
        [first, *_] = self.list()["lessons"]
        self.assertEqual(first["slot_id"], self.a_english.pk)
        self.assertEqual(first["period_label"], "Period 1")
        self.assertEqual(first["end_time"], "08:45")
        self.assertEqual(first["school_class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(first["teacher_name"], "Chidi Eze")
        self.assertEqual(first["status"], "NOT_STARTED")
        self.assertIsNone(first["submitted_at"])
        self.assertEqual(first["branch"], {"id": self.lekki.pk, "name": "Lekki Branch"})

    def test_the_status_and_the_taker_follow_the_register(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        self.assertEqual(self.list()["lessons"][0]["status"], "IN_PROGRESS")
        self.fill_lesson(self.eze, self.a_english)
        self.assertEqual(self.list()["lessons"][0]["status"], "READY")
        self.submit_lesson(self.eze, self.a_english)
        first = self.list()["lessons"][0]
        self.assertEqual(first["status"], "SUBMITTED")
        self.assertIsNotNone(first["submitted_at"])
        self.assertEqual(first["submitted_by_name"], "Chidi Eze")

    def test_a_branch_narrows_the_list_and_a_branch_bound_reader_sees_only_their_own(self):
        names = lambda body: [r["school_class"]["name"] for r in body["lessons"]]  # noqa: E731
        self.assertEqual(names(self.list(branch=self.ikeja.pk)), ["SSS2 Science"])
        self.assertEqual(names(self.list(self.lekki_admin)), ["JSS1 A", "JSS1 B", "JSS1 A"])
        self.assertEqual(names(self.list(self.ikeja_admin)), ["SSS2 Science"])
        self.assertEqual(
            self.lessons(self.lekki_admin, branch=self.ikeja.pk).status_code, 404,
        )

    def test_a_school_with_one_branch_carries_no_branch_column(self):
        response = self.lessons(self.solo_admin)
        body = self.data(response)
        self.assertFalse(body["branch_dimension"])
        [row] = body["lessons"]
        self.assertIsNone(row["branch"])
        self.assertEqual(row["school_class"]["name"], "Basic 3")
        self.assertEqual(row["teacher_name"], "Bola Ade")

    def test_the_list_is_the_administrators_and_a_teacher_is_sent_to_their_own(self):
        response = self.lessons(self.eze)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "ADMINISTRATORS_ONLY")

    def test_with_lesson_marking_off_everywhere_the_list_is_empty_and_says_so(self):
        self.switch_lessons_off()
        body = self.list()
        self.assertEqual(body["lessons"], [])
        self.assertFalse(body["lesson_marking_on"])

    def test_a_level_that_does_not_mark_lessons_has_none(self):
        make_slot(self.tenant, self.pry4a, 1, self.period1, self.english, self.eze)
        publish(self.tenant, self.pry4a)
        self.make_student("Pat", "Okon", self.pry4a, "BFS/777")
        self.assertNotIn("Primary 4 A", [r["school_class"]["name"] for r in self.list()["lessons"]])

    def test_a_class_whose_timetable_is_a_draft_has_no_lessons(self):
        publish(self.tenant, self.jss1a, PublishState.DRAFT)
        names = [r["school_class"]["name"] for r in self.list()["lessons"]]
        self.assertEqual(names, ["JSS1 B", "SSS2 Science"])

    def test_a_break_is_not_a_lesson(self):
        make_slot(self.tenant, self.jss1b, 1, self.breaktime, self.maths, self.bello)
        rows = self.list()["lessons"]
        self.assertEqual(len(rows), 4)
        self.assertNotIn("Break", [r["period_label"] for r in rows])

    def test_a_period_taken_out_of_use_is_not_a_lesson(self):
        Period.objects.filter(pk=self.period2.pk).update(is_active=False)
        self.assertEqual(len(self.list()["lessons"]), 3)

    def test_a_class_with_nobody_placed_has_no_lessons_to_mark(self):
        ClassEnrolment.all_objects.filter(school_class=self.sss2).update(
            effective_date=dt.date(2026, 1, 5),
        )
        names = [r["school_class"]["name"] for r in self.list()["lessons"]]
        self.assertNotIn("SSS2 Science", names)

    def test_a_day_the_school_is_closed_lists_nothing_and_says_why(self):
        body = self.list(date=str(FOUNDERS_DAY))
        self.assertEqual(body["lessons"], [])
        self.assertEqual(body["closed"]["reason"], "Founders' Day")

    def test_a_branch_closure_takes_only_that_branchs_lessons(self):
        make_slot(self.tenant, self.jss1b, 2, self.period1, self.maths, self.bello)
        make_slot(self.tenant, self.sss2, 2, self.period1, self.english, self.ikeja_admin)
        body = self.list(date=str(LEKKI_OPEN_DAY))
        self.assertEqual(
            [r["school_class"]["name"] for r in body["lessons"]], ["SSS2 Science"],
        )

    def test_a_day_that_has_not_come_is_refused(self):
        response = self.lessons(self.admin, date=str(TOMORROW))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DATE_IN_FUTURE")

    def test_another_schools_lessons_are_not_listed(self):
        names = [r["school_class"]["name"] for r in self.list(self.solo_admin)["lessons"]]
        self.assertEqual(names, ["Basic 3"])

    def test_a_weekday_with_no_lessons_is_an_empty_list_not_an_error(self):
        body = self.list(date="2025-10-10")
        self.assertEqual(body["lessons"], [])
        self.assertTrue(body["lesson_marking_on"])


class LessonRegisterReadTests(LessonFixture):
    def test_the_register_names_the_lesson_the_class_and_the_roster(self):
        body = self.data(self.lesson(self.eze, self.a_english))
        self.assertEqual(body["lesson"]["subject"], "English Language")
        self.assertEqual(body["lesson"]["period_label"], "Period 1")
        self.assertEqual(body["lesson"]["start_time"], "08:00")
        self.assertEqual(body["lesson"]["teacher_name"], "Chidi Eze")
        self.assertEqual(body["school_class"]["name"], "JSS1 A")
        self.assertEqual(body["school_class"]["branch"]["name"], "Lekki Branch")
        self.assertEqual(body["status"], "NOT_STARTED")
        self.assertIsNone(body["closed"])
        self.assertIsNone(body["taken_by"])
        self.assertEqual(
            [r["name"] for r in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        self.assertEqual(body["counts"], {
            "total": 3, "present": 0, "late": 0, "absent": 0, "unmarked": 3, "locked": 1,
        })
        self.assertNotIn("submit_preview", body)

    def test_each_row_shows_the_morning_mark_only_once_the_class_register_is_in(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        rows = self.lesson_rows(self.lesson(self.eze, self.a_english))
        self.assertIsNone(rows[self.tunde.pk]["morning_mark"])

        self.take(self.eze, self.jss1a, TODAY, tunde="A", kelechi="L")
        rows = self.lesson_rows(self.lesson(self.eze, self.a_english))
        self.assertEqual(rows[self.tunde.pk]["morning_mark"], "A")
        self.assertEqual(rows[self.kelechi.pk]["morning_mark"], "L")
        self.assertEqual(rows[self.chiamaka.pk]["morning_mark"], "P")
        self.assertIsNone(rows[self.amina.pk]["morning_mark"])

    def test_a_lesson_with_no_morning_register_reads_not_taken(self):
        rows = self.lesson_rows(self.lesson(self.eze, self.a_english))
        self.assertTrue(all(row["morning_mark"] is None for row in rows.values()))

    def test_a_slot_on_another_weekday_is_refused(self):
        for slot, day in ((self.a_english, TOMORROW), (self.a_tuesday, TODAY)):
            response = self.lesson(self.admin, slot, day)
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(self.code(response), "LESSON_NOT_ON_THIS_DAY")
        self.assertIn("Mondays", self.lesson(self.admin, self.a_english, TOMORROW).data["message"])

    def test_a_break_period_is_not_a_lesson(self):
        slot = make_slot(self.tenant, self.jss1b, 1, self.breaktime, self.maths, self.bello)
        response = self.lesson(self.admin, slot)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "LESSON_NOT_ON_THIS_DAY")
        self.assertEqual(self.mark_lesson(self.admin, slot, self.zainab, "P").status_code, 422)

    def test_a_level_with_lesson_marking_off_points_at_the_settings(self):
        self.switch_lessons_off()
        for response in (
            self.lesson(self.eze, self.a_english),
            self.mark_lesson(self.eze, self.a_english, self.tunde, "P"),
            self.fill_lesson(self.eze, self.a_english),
            self.submit_lesson(self.eze, self.a_english),
        ):
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(self.code(response), "LESSON_MARKING_OFF")
            self.assertIn("Attendance settings", response.data["message"])
            self.assertIn("JSS1", response.data["message"])
        self.assertFalse(LessonRegister.objects.exists())

    def test_a_timetable_that_is_not_published_has_no_lessons_to_mark(self):
        publish(self.tenant, self.jss1a, PublishState.DRAFT)
        for response in (
            self.lesson(self.eze, self.a_english),
            self.mark_lesson(self.eze, self.a_english, self.tunde, "P"),
        ):
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(self.code(response), "TIMETABLE_NOT_PUBLISHED")
        self.assertFalse(LessonRegister.objects.exists())

    def test_a_lesson_register_is_a_register_of_its_own(self):
        self.take(self.eze, self.jss1a, TODAY)
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        self.assertEqual(LessonRegister.objects.count(), 1)
        body = self.data(self.read(self.eze, self.jss1a))
        self.assertEqual(body["counts"]["absent"], 0)


class SnapshotTests(LessonFixture):
    """A lesson already taken is not rewritten by a later edit to the timetable."""

    def test_the_teacher_subject_and_period_are_those_of_the_day_it_was_taken(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        TimetableSlot.objects.filter(pk=self.a_english.pk).update(
            teacher=self.bello, subject=self.maths,
        )
        Period.objects.filter(pk=self.period1.pk).update(
            label="First lesson", start_time=dt.time(7, 30), end_time=dt.time(8, 15),
        )
        body = self.data(self.lesson(self.admin, self.a_english))
        self.assertEqual(body["lesson"]["teacher_name"], "Chidi Eze")
        self.assertEqual(body["lesson"]["subject"], "English Language")
        self.assertEqual(body["lesson"]["period_label"], "Period 1")
        self.assertEqual(body["lesson"]["start_time"], "08:00")
        register = LessonRegister.objects.get()
        self.assertEqual((register.teacher, register.subject), (self.eze, self.english))

        listed = [
            r for r in self.data(self.lessons(self.admin))["lessons"]
            if r["slot_id"] == self.a_english.pk
        ]
        self.assertEqual(listed[0]["teacher_name"], "Chidi Eze")
        self.assertEqual(listed[0]["subject"], "English Language")

    def test_before_any_mark_the_lesson_is_described_from_the_timetable_as_it_stands(self):
        TimetableSlot.objects.filter(pk=self.a_english.pk).update(teacher=self.bello)
        self.assertEqual(
            self.data(self.lesson(self.admin, self.a_english))["lesson"]["teacher_name"],
            "Sade Bello",
        )

    def test_saving_the_grid_again_does_not_lose_or_duplicate_the_lessons_register(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        # The grid save deletes the class's slots and writes them afresh.
        TimetableSlot.objects.filter(pk=self.a_english.pk).delete()
        fresh = make_slot(self.tenant, self.jss1a, 1, self.period1, self.english, self.eze)
        self.assertNotEqual(fresh.pk, self.a_english.pk)
        self.assertIsNone(LessonRegister.objects.get().slot)

        body = self.data(self.lesson(self.eze, fresh))
        rows = {row["student_id"]: row for row in body["rows"]}
        self.assertEqual(rows[self.tunde.pk]["mark"], "A")
        self.mark_lesson(self.eze, fresh, self.kelechi, "P")
        self.assertEqual(LessonRegister.objects.count(), 1)
        self.assertEqual(LessonRegister.objects.get().slot, fresh)

    def test_a_lesson_register_is_unique_to_its_class_period_and_day(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        first = LessonRegister.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LessonRegister.objects.create(
                tenant=self.tenant, school_class=self.jss1a, period=self.period1, date=TODAY,
                branch=self.lekki, session=self.year, period_label="Period 1",
                starts_at=dt.time(8, 0), ends_at=dt.time(8, 45), subject=self.english,
            )
        self.assertEqual(LessonRegister.objects.get(), first)


class SettingsTests(LessonFixture):
    def test_each_level_says_whether_a_class_in_it_has_a_published_timetable(self):
        response = TenantAPIClient(self.admin).get(reverse("attendance-settings"))
        levels = {level["code"]: level for level in self.data(response)["levels"]}
        self.assertTrue(levels["JSS1"]["has_published_timetable"])
        self.assertTrue(levels["JSS1"]["lesson_marking"])
        self.assertFalse(levels["PRY4"]["has_published_timetable"])
        self.assertFalse(levels["PRY4"]["lesson_marking"])

    def test_a_level_with_only_drafts_has_none(self):
        for school_class in (self.jss1a, self.jss1b, self.sss2):
            publish(self.tenant, school_class, PublishState.DRAFT)
        response = TenantAPIClient(self.admin).get(reverse("attendance-settings"))
        levels = {level["code"]: level for level in self.data(response)["levels"]}
        self.assertFalse(levels["JSS1"]["has_published_timetable"])
        self.assertTrue(levels["JSS1"]["lesson_marking"])

    def test_another_schools_published_timetable_does_not_count(self):
        response = TenantAPIClient(self.solo_admin).get(reverse("attendance-settings"))
        levels = {level["code"]: level for level in self.data(response)["levels"]}
        self.assertEqual(set(levels), {"BAS3"})
        self.assertTrue(levels["BAS3"]["has_published_timetable"])
