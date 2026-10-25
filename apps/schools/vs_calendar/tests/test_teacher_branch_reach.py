"""A lesson is given only to a teacher whose roles cover the class's branch.

Okafor is a Teacher at Lekki only. A lesson in Ikeja's SSS2 Science would be
hers on the timetable and missing from her own lesson list, so she could never
mark it: the lesson form, the grid save and a copied week all refuse it.
"""
from __future__ import annotations

from ..models import TimetableSlot
from .base import _Base


class LessonTeacherBranchTests(_Base):
    def setUp(self):
        self.okafor = self.make_teacher(
            "okafor@brightfield.test", "Ngozi", "Okafor", branch=self.lekki,
        )

    def lesson(self, school_class, room, teacher):
        return {
            "school_class": school_class.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": teacher.pk, "room": room.pk,
        }

    def test_a_lesson_at_another_branch_is_refused(self):
        response = self.post(
            self.admin, "calendar-slot-list", self.lesson(self.sss2, self.room_c1, self.okafor),
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_NOT_AT_BRANCH")
        self.assertIn("Teacher role at Ikeja Branch", response.data["message"])
        self.assertFalse(TimetableSlot.all_objects.filter(school_class=self.sss2).exists())

    def test_a_lesson_at_their_own_branch_is_saved(self):
        response = self.post(
            self.admin, "calendar-slot-list", self.lesson(self.jss1a, self.room_a1, self.okafor),
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_grid_save_with_one_such_lesson_saves_nothing(self):
        response = self.put(
            self.admin, "calendar-class-grid",
            {"slots": [self.lesson(self.sss2, self.room_c1, self.okafor)]},
            class_id=self.sss2.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_NOT_AT_BRANCH")
        self.assertFalse(TimetableSlot.all_objects.filter(school_class=self.sss2).exists())

    def test_copying_a_week_to_another_branchs_class_is_refused_preview_included(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths,
            teacher=self.okafor, room=self.room_a1,
        )
        for params in ({"preview": "1"}, None):
            with self.subTest(preview=bool(params)):
                response = self.post(
                    self.admin, "calendar-class-duplicate",
                    {"source_class": self.jss1a.pk, "keep_rooms": False},
                    params=params, class_id=self.sss2.pk,
                )
                self.assertEqual(response.status_code, 422, response.data)
                self.assertEqual(response.data["error"]["code"], "TEACHER_NOT_AT_BRANCH")
        self.assertFalse(TimetableSlot.all_objects.filter(school_class=self.sss2).exists())

    def test_copying_without_teachers_is_not_asked(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths,
            teacher=self.okafor, room=self.room_a1,
        )
        response = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk, "keep_teachers": False, "keep_rooms": False},
            class_id=self.sss2.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
