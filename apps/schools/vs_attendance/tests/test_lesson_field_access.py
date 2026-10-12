"""Which lesson fields a role reads and changes.

``attendance.lesson`` is a resource of its own, beside ``attendance.register``.
These tests install the real declarations and switch them per role, then prove
each switch on every route that carries the field: the lesson register and its
history, the mark write and the fill-in, the administrator's list, a teacher's
day, and the blocks the hub, the student's tab and the report carry.
"""
from __future__ import annotations

from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..models import LessonMark
from .base import TODAY
from .lesson_base import LessonFixture

RESOURCE = "attendance.lesson"
MARK = "attendance.lesson.mark"
ARRIVED = "attendance.lesson.arrived_at"
NOTE = "attendance.lesson.note"
MORNING = "attendance.lesson.morning_mark"
STATUS = "attendance.lesson.status"
COUNTS = "attendance.lesson.counts"
TAKEN_BY = "attendance.lesson.taken_by"
REGISTER_MARK = "attendance.register.mark"


class _Switches(LessonFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(RESOURCE, "attendance.register")

    def close(self, role, *keys):
        set_field_access(role, *keys, read=False, write=False)

    def row(self, user=None, pupil=None, slot=None):
        response = self.lesson(user or self.eze, slot or self.a_english)
        return self.lesson_rows(response)[(pupil or self.tunde).pk]

    def taken(self):
        """Morning register in, and a lesson register with a missed lesson in it."""
        self.take(self.eze, self.jss1a, TODAY, tunde="A", kelechi="L")
        self.take_lesson(self.eze, self.a_english, TODAY, kelechi="A", chiamaka="A")


class DefaultAccessTests(_Switches):
    def test_the_declaration_matches_what_the_registry_holds(self):
        self.assertEqual(
            set(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", flat=True)),
            {MARK, ARRIVED, NOTE, MORNING, STATUS, COUNTS, TAKEN_BY},
        )

    def test_no_field_is_sensitive_and_the_server_owned_fields_are_not_writable(self):
        fields = {f.key: f for f in FieldDefinition.objects.filter(key__startswith=RESOURCE)}
        self.assertEqual({k for k, f in fields.items() if f.sensitive}, set())
        self.assertEqual(
            {k for k, f in fields.items() if not f.writable},
            {MORNING, STATUS, COUNTS, TAKEN_BY},
        )

    def test_the_lesson_mark_travels_under_three_names_and_one_switch_covers_them(self):
        mark = FieldDefinition.objects.get(key=MARK)
        self.assertEqual(mark.api_names, ["mark", "from_mark", "to_mark"])
        taker = FieldDefinition.objects.get(key=TAKEN_BY)
        self.assertEqual(taker.api_names, ["taken_by", "submitted_at", "submitted_by_name"])

    def test_without_a_switch_every_field_is_readable_everywhere(self):
        self.taken()
        self.mark_lesson(self.eze, self.a_english, self.chiamaka, "P")
        row = self.row(pupil=self.kelechi)
        for name in ("mark", "arrived_at", "note", "morning_mark"):
            self.assertIn(name, row)
        body = self.data(self.lesson(self.eze, self.a_english))
        for name in ("status", "counts", "taken_by"):
            self.assertIn(name, body)
        listed = self.data(self.lessons(self.admin))["lessons"][0]
        for name in ("status", "submitted_at", "submitted_by_name"):
            self.assertIn(name, listed)
        self.assertIn("missed_lessons", self.data(self.today(self.admin)))
        self.assertIn("missed_a_lesson", self.data(self.today(self.admin))["kpis"])


class ReadSwitchTests(_Switches):
    def test_a_role_that_may_not_read_the_lesson_mark_does_not_see_it_on_a_row_or_in_the_history(self):
        self.take_lesson(self.eze, self.a_english)
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A")
        self.close(self.teacher_role, MARK)
        body = self.data(self.lesson(self.eze, self.a_english))
        for row in body["rows"]:
            self.assertNotIn("mark", row)
        [entry] = body["history"]
        self.assertNotIn("from_mark", entry)
        self.assertNotIn("to_mark", entry)
        self.assertIn("changed_by_name", entry)
        self.assertIn("morning_mark", body["rows"][0])

    def test_the_note_the_arrival_time_and_the_morning_mark_have_switches_of_their_own(self):
        self.take(self.eze, self.jss1a, TODAY)
        self.mark_lesson(self.eze, self.a_english, self.kelechi, "L", arrived_at="08:10", note="Bus")
        self.close(self.teacher_role, NOTE, ARRIVED)
        row = self.row(pupil=self.kelechi)
        self.assertNotIn("note", row)
        self.assertNotIn("arrived_at", row)
        self.assertIn("mark", row)
        self.assertEqual(row["morning_mark"], "P")
        self.close(self.teacher_role, MORNING)
        self.assertNotIn("morning_mark", self.row(pupil=self.kelechi))

    def test_a_morning_mark_hidden_on_the_class_register_is_hidden_beside_a_lesson_too(self):
        self.take(self.eze, self.jss1a, TODAY, tunde="A")
        self.assertEqual(self.row()["morning_mark"], "A")
        self.close(self.teacher_role, REGISTER_MARK)
        row = self.row()
        self.assertNotIn("morning_mark", row)
        self.assertIn("mark", row)
        self.assertNotIn("mark", self.rows(self.read(self.eze, self.jss1a))[self.tunde.pk])

    def test_status_counts_and_who_took_it_are_derived_and_each_can_be_hidden(self):
        self.take_lesson(self.eze, self.a_english)
        self.close(self.teacher_role, STATUS, COUNTS, TAKEN_BY)
        body = self.data(self.lesson(self.eze, self.a_english))
        for name in ("status", "counts", "taken_by"):
            self.assertNotIn(name, body)
        self.assertIn("rows", body)
        self.assertIn("lesson", body)

    def test_the_lessons_list_follows_the_status_and_the_taker_switches(self):
        self.take_lesson(self.eze, self.a_english)
        self.close(self.admin_role, STATUS)
        first = self.data(self.lessons(self.admin))["lessons"][0]
        self.assertNotIn("status", first)
        self.assertIn("submitted_at", first)
        self.close(self.admin_role, TAKEN_BY)
        first = self.data(self.lessons(self.admin))["lessons"][0]
        for name in ("submitted_at", "submitted_by_name"):
            self.assertNotIn(name, first)
        self.assertEqual(first["subject"], "English Language")

    def test_a_teachers_day_follows_the_same_switches(self):
        self.take_lesson(self.eze, self.a_english)
        [card] = self.data(self.today_mine(self.eze))["lessons"]
        self.assertEqual(card["status"], "SUBMITTED")
        self.close(self.teacher_role, STATUS, TAKEN_BY)
        [card] = self.data(self.today_mine(self.eze))["lessons"]
        self.assertEqual(set(card), {"slot_id", "period_label", "start_time", "subject", "class_name"})

    def test_a_role_with_nothing_open_still_gets_the_lesson_and_its_roster(self):
        self.close(self.teacher_role, *[k for k in self.keys if k.startswith(RESOURCE)])
        body = self.data(self.lesson(self.eze, self.a_english))
        self.assertEqual(body["lesson"]["subject"], "English Language")
        self.assertEqual(body["school_class"]["name"], "JSS1 A")
        for row in body["rows"]:
            self.assertEqual(set(row), {"student_id", "name", "student_number", "locked"})

    def test_the_register_switches_do_not_reach_the_lesson_register(self):
        self.take_lesson(self.eze, self.a_english)
        self.close(
            self.teacher_role, "attendance.register.note", "attendance.register.arrived_at",
            "attendance.register.counts", "attendance.register.status",
        )
        body = self.data(self.lesson(self.eze, self.a_english))
        for name in ("status", "counts", "taken_by"):
            self.assertIn(name, body)
        self.assertIn("note", body["rows"][0])


class MissedBlocksTests(_Switches):
    def setUp(self):
        super().setUp()
        self.taken()

    def hub(self):
        return self.data(self.today(self.admin))

    def test_the_list_needs_the_lesson_mark(self):
        self.assertEqual(len(self.hub()["missed_lessons"]), 2)
        self.close(self.admin_role, MARK)
        body = self.hub()
        self.assertNotIn("missed_lessons", body)
        self.assertNotIn("missed_a_lesson", body["kpis"])

    def test_the_count_needs_the_lesson_counts_as_well_and_the_list_does_not(self):
        self.close(self.admin_role, COUNTS)
        body = self.hub()
        self.assertNotIn("missed_a_lesson", body["kpis"])
        self.assertEqual(len(body["missed_lessons"]), 2)

    def test_the_rows_lose_the_morning_mark_with_either_of_its_switches(self):
        self.assertEqual(self.hub()["missed_lessons"][0]["morning_mark"], "L")
        self.close(self.admin_role, MORNING)
        for row in self.hub()["missed_lessons"]:
            self.assertNotIn("morning_mark", row)
        set_field_access(self.admin_role, MORNING, read=True, write=False)
        self.assertIn("morning_mark", self.hub()["missed_lessons"][0])
        self.close(self.admin_role, REGISTER_MARK)
        for row in self.hub()["missed_lessons"]:
            self.assertNotIn("morning_mark", row)
            self.assertIn("subject", row)

    def test_the_students_lesson_absences_need_the_lesson_mark(self):
        self.assertEqual(len(self.tab(self.chiamaka)["lesson_absences"]), 1)
        self.close(self.admin_role, MORNING)
        [row] = self.tab(self.chiamaka)["lesson_absences"]
        self.assertNotIn("morning_mark", row)
        self.close(self.admin_role, MARK)
        self.assertNotIn("lesson_absences", self.tab(self.chiamaka))
        self.assertIn("counts", self.tab(self.chiamaka))

    def test_the_morning_mark_on_the_tab_follows_the_class_register_switch(self):
        self.close(self.admin_role, REGISTER_MARK)
        [row] = self.tab(self.chiamaka)["lesson_absences"]
        self.assertNotIn("morning_mark", row)

    def test_the_reports_table_needs_the_lesson_mark_and_the_lesson_counts(self):
        reporter_role = self.reporter_role
        self.assertEqual(len(self.report()["lessons_missed"]), 1)
        self.close(reporter_role, COUNTS)
        self.assertNotIn("lessons_missed", self.report())
        set_field_access(reporter_role, COUNTS, read=True, write=False)
        self.close(reporter_role, MARK)
        self.assertNotIn("lessons_missed", self.report())
        set_field_access(reporter_role, MARK, read=True, write=True)
        self.assertIn("lessons_missed", self.report())

    def test_the_registers_own_switches_do_not_decide_the_lesson_blocks(self):
        self.close(
            self.admin_role, "attendance.register.counts", "attendance.register.status",
            "attendance.register.note",
        )
        body = self.hub()
        self.assertNotIn("kpis", body)
        self.assertEqual(len(body["missed_lessons"]), 2)


class WriteSwitchTests(_Switches):
    def test_a_role_that_may_not_write_the_lesson_mark_cannot_mark(self):
        set_field_access(self.teacher_role, MARK, read=True, write=False)
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "P")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LessonMark.objects.exists())

    def test_nor_can_it_fill_in_the_rest_of_the_register(self):
        set_field_access(self.teacher_role, MARK, read=True, write=False)
        response = self.fill_lesson(self.eze, self.a_english)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LessonMark.objects.exists())

    def test_a_role_that_may_not_write_the_arrival_time_cannot_mark_a_student_late(self):
        set_field_access(self.teacher_role, ARRIVED, read=True, write=False)
        response = self.mark_lesson(self.eze, self.a_english, self.tunde, "L", arrived_at="08:10")
        self.assertEqual(response.status_code, 403)
        self.assertIn("arrived_at", response.data["error"]["detail"])
        self.assertEqual(self.mark_lesson(self.eze, self.a_english, self.tunde, "P").status_code, 200)

    def test_a_role_that_may_not_write_the_note_cannot_attach_one(self):
        self.mark_lesson(self.eze, self.a_english, self.tunde, "A", note="Fever")
        set_field_access(self.teacher_role, NOTE, read=True, write=False)
        refused = self.mark_lesson(self.eze, self.a_english, self.tunde, "A", note="Malaria")
        self.assertEqual(refused.status_code, 403)
        echoed = self.mark_lesson(self.eze, self.a_english, self.tunde, "P", note="Fever")
        self.assertEqual(echoed.status_code, 200, echoed.data)
        self.assertEqual(LessonMark.objects.get().note, "Fever")

    def test_a_write_switch_cannot_be_given_to_a_field_nothing_can_write(self):
        from django.core.exceptions import ValidationError

        from vs_rbac.models import RoleFieldAccess

        for key in (MORNING, STATUS, COUNTS, TAKEN_BY):
            row = RoleFieldAccess(
                role=self.teacher_role, field_id=key, can_read=True, can_write=True,
            )
            with self.assertRaises(ValidationError):
                row.full_clean()

    def test_the_administrator_role_is_governed_by_the_same_switches(self):
        set_field_access(self.admin_role, MARK, read=True, write=False)
        self.assertEqual(self.mark_lesson(self.admin, self.a_english, self.tunde, "P").status_code, 403)

    def test_the_class_registers_write_switch_does_not_govern_a_lesson_mark(self):
        set_field_access(self.teacher_role, REGISTER_MARK, read=True, write=False)
        self.assertEqual(self.mark_lesson(self.eze, self.a_english, self.tunde, "P").status_code, 200)
        self.assertEqual(self.mark(self.eze, self.jss1a, self.tunde, "P").status_code, 403)
