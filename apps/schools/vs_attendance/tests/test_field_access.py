"""Which register fields a role reads and changes.

The register's fields are all open to every role by default. These tests
install the real declarations (``install_declared_fields``) and switch them per
role, then prove each switch on every route that carries the field: the register read,
the history inside it, the mark write and the fill-in.
"""
from __future__ import annotations

from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..models import StudentMark
from .base import AttendanceFixture

RESOURCE = "attendance.register"
NOTE = "attendance.register.note"
MARK = "attendance.register.mark"
ARRIVED = "attendance.register.arrived_at"
REASON = "attendance.register.reason_code"
NOTICE = "attendance.register.notice_state"
COUNTS = "attendance.register.counts"
STATUS = "attendance.register.status"
TAKEN_BY = "attendance.register.taken_by"


class _Switches(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(RESOURCE)

    def teacher_sees(self, *, read=(), closed=()):
        """Open the named fields to the Teacher role and close the others named."""
        if read:
            set_field_access(self.teacher_role, *read, read=True, write=True)
        if closed:
            set_field_access(self.teacher_role, *closed, read=False, write=False)

    def row(self, user=None, pupil=None):
        user = user or self.eze
        return self.rows(self.read(user, self.jss1a))[(pupil or self.tunde).pk]


class DefaultAccessTests(_Switches):
    def test_the_declaration_matches_what_the_registry_holds(self):
        self.assertEqual(
            set(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", flat=True)),
            {NOTE, MARK, ARRIVED, REASON, NOTICE, COUNTS, STATUS, TAKEN_BY},
        )

    def test_no_field_is_sensitive_and_the_server_owned_fields_are_not_writable(self):
        fields = {f.key: f for f in FieldDefinition.objects.filter(key__startswith=RESOURCE)}
        self.assertEqual({k for k, f in fields.items() if f.sensitive}, set())
        self.assertEqual(
            {k for k, f in fields.items() if not f.writable},
            {NOTICE, COUNTS, STATUS, TAKEN_BY},
        )

    def test_without_a_switch_every_field_including_the_note_is_readable(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        StudentMark.objects.filter(student=self.tunde).update(note="fever")
        row = self.row()
        for name in ("mark", "arrived_at", "reason_code", "reason_label", "note", "notice_state"):
            self.assertIn(name, row)
        self.assertEqual(row["note"], "fever")
        body = self.data(self.read(self.eze, self.jss1a))
        for name in ("counts", "status", "submit_preview", "taken_by"):
            self.assertIn(name, body)

    def test_the_note_is_attached_and_corrected_by_whoever_may_mark_without_a_switch(self):
        first = self.mark(self.eze, self.jss1a, self.tunde, "A", note="fever")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(StudentMark.objects.get(student=self.tunde).note, "fever")

        corrected = self.mark(self.eze, self.jss1a, self.tunde, "A", note="malaria")
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(StudentMark.objects.get(student=self.tunde).note, "malaria")

    def test_a_school_can_close_the_note_to_a_role(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A", note="fever")
        self.teacher_sees(closed=(NOTE,))
        self.assertNotIn("note", self.row())
        refused = self.mark(self.eze, self.jss1a, self.tunde, "A", note="malaria")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(StudentMark.objects.get(student=self.tunde).note, "fever")

    def test_a_note_that_may_be_read_but_not_written_is_refused_a_correction(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A", note="fever")
        set_field_access(self.teacher_role, NOTE, read=True, write=False)
        corrected = self.mark(self.eze, self.jss1a, self.tunde, "A", note="malaria")
        self.assertEqual(corrected.status_code, 403)
        self.assertIn("note", corrected.data["error"]["detail"])

    def test_a_form_that_sends_the_note_back_unchanged_is_not_refused(self):
        self.mark(self.eze, self.jss1a, self.tunde, "A", note="fever")
        set_field_access(self.teacher_role, NOTE, read=True, write=False)
        echoed = self.mark(self.eze, self.jss1a, self.tunde, "P", note="fever")
        self.assertEqual(echoed.status_code, 200, echoed.data)


class ReadSwitchTests(_Switches):
    def setUp(self):
        super().setUp()
        for pupil, kind in ((self.tunde, "A"), (self.kelechi, "P"), (self.chiamaka, "P")):
            self.mark(self.eze, self.jss1a, pupil, kind)

    def test_a_role_that_may_not_read_the_mark_does_not_see_it_on_a_row_or_in_the_history(self):
        self.submit(self.eze, self.jss1a)
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.teacher_sees(closed=(MARK,))

        body = self.data(self.read(self.eze, self.jss1a))
        for row in body["rows"]:
            self.assertNotIn("mark", row)
        [entry] = body["history"]
        self.assertNotIn("from_mark", entry)
        self.assertNotIn("to_mark", entry)
        self.assertIn("changed_by_name", entry)

    def test_the_note_on_a_changed_row_follows_the_same_switch_in_the_history(self):
        self.submit(self.eze, self.jss1a)
        self.teacher_sees(read=(NOTE,))
        self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=self.some_reason(), note="clinic")
        self.assertEqual(self.data(self.read(self.eze, self.jss1a))["history"][0]["note"], "clinic")

        self.teacher_sees(closed=(NOTE,))
        [entry] = self.data(self.read(self.eze, self.jss1a))["history"]
        self.assertNotIn("note", entry)

    def some_reason(self):
        from ..models import ReasonCode

        return ReasonCode.objects.get(tenant=self.tenant, name="Sick").pk

    def test_the_reason_travels_as_an_id_and_a_name_and_one_switch_hides_both(self):
        self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=self.some_reason())
        row = self.row()
        self.assertEqual(row["reason_label"], "Sick")
        self.teacher_sees(closed=(REASON,))
        row = self.row()
        self.assertNotIn("reason_code", row)
        self.assertNotIn("reason_label", row)

    def test_the_arrival_time_and_the_notice_state_have_their_own_switches(self):
        self.mark(self.eze, self.jss1a, self.kelechi, "L", arrived_at="08:40")
        self.teacher_sees(closed=(ARRIVED, NOTICE))
        row = self.row(pupil=self.kelechi)
        self.assertNotIn("arrived_at", row)
        self.assertNotIn("notice_state", row)
        self.assertNotIn("notice_sent_at", row)
        self.assertIn("mark", row)

    def test_counts_status_and_who_took_it_are_derived_and_each_can_be_hidden(self):
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        self.teacher_sees(closed=(COUNTS, STATUS, TAKEN_BY))
        body = self.data(self.read(self.eze, self.jss1a))
        for name in ("counts", "status", "taken_by", "submit_preview"):
            self.assertNotIn(name, body)
        self.assertIn("rows", body)

    def test_a_role_with_nothing_open_still_gets_the_register_and_its_class(self):
        self.teacher_sees(closed=tuple(k for k in self.keys))
        body = self.data(self.read(self.eze, self.jss1a))
        self.assertEqual(body["school_class"]["name"], "JSS1 A")
        self.assertEqual([r["name"] for r in body["rows"]][:1], ["Tunde Bello"])
        for row in body["rows"]:
            self.assertEqual(
                set(row), {"student_id", "name", "student_number", "locked"},
            )


class WriteSwitchTests(_Switches):
    def test_a_role_that_may_not_write_the_mark_cannot_mark(self):
        self.teacher_sees(read=(MARK,))
        set_field_access(self.teacher_role, MARK, read=True, write=False)
        response = self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StudentMark.objects.exists())

    def test_nor_can_it_fill_in_the_rest_of_the_register(self):
        set_field_access(self.teacher_role, MARK, read=True, write=False)
        response = self.fill(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StudentMark.objects.exists())

    def test_a_role_that_may_not_write_the_arrival_time_cannot_mark_a_child_late(self):
        set_field_access(self.teacher_role, ARRIVED, read=True, write=False)
        response = self.mark(self.eze, self.jss1a, self.tunde, "L", arrived_at="08:30")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.mark(self.eze, self.jss1a, self.tunde, "P").status_code, 200)

    def test_a_role_that_may_not_write_the_reason_cannot_excuse_an_absence(self):
        from ..services.reasons import ensure_default_reason_codes

        ensure_default_reason_codes(self.tenant)
        set_field_access(self.teacher_role, REASON, read=True, write=False)
        response = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=self.reason())
        self.assertEqual(response.status_code, 403)

    def reason(self):
        from ..models import ReasonCode

        return ReasonCode.objects.get(tenant=self.tenant, name="Sick").pk

    def test_a_write_switch_cannot_be_given_to_a_field_nothing_can_write(self):
        from django.core.exceptions import ValidationError

        from vs_rbac.models import RoleFieldAccess

        row = RoleFieldAccess(
            role=self.teacher_role, field_id=COUNTS, can_read=True, can_write=True,
        )
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_the_administrator_role_is_governed_by_the_same_switches(self):
        set_field_access(self.admin_role, MARK, read=True, write=False)
        self.assertEqual(self.mark(self.admin, self.jss1a, self.tunde, "P").status_code, 403)
