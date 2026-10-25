"""Which staff register fields a role reads and changes.

``attendance.staff`` is a resource of its own beside ``attendance.register``.
These tests install the real declarations and switch them per role, then prove
each switch on every route that carries the field, including the two values a row
borrows from Staff Management (the job title and the exit date), which obey that
module's switches and not a copy of them.
"""
from __future__ import annotations

import datetime as dt

from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_staff.models import StaffProfile

from .base import TODAY
from .staff_base import StaffFixture

RESOURCE = "attendance.staff"
MARK = "attendance.staff.mark"
ARRIVED = "attendance.staff.arrived_at"
NOTE = "attendance.staff.note"
STATUS = "attendance.staff.status"
COUNTS = "attendance.staff.counts"
TAKEN_BY = "attendance.staff.taken_by"
JOB_TITLE = "school.teachers.job_title"
EXIT_DATE = "school.teachers.exit_date"
EMPLOYMENT_STATUS = "school.teachers.employment_status"


class _Switches(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(RESOURCE, "school.teachers")

    def close(self, *keys, keep_read=False):
        """Switch *keys* off for the register role: Read and Write, or Write alone with *keep_read*."""
        set_field_access(self.staff_role, *keys, read=keep_read, write=False)

    def row(self, person=None, user=None):
        response = self.staff_day(user or self.registrar)
        return self.staff_rows(response, self.lekki)[(person or self.peter).pk]

    def submitted(self):
        self.lekki_everyone_marked(adeyemi="L")
        self.assertEqual(self.submit_staff(self.registrar, self.lekki).status_code, 200)


class DefaultAccessTests(_Switches):
    def test_the_declaration_matches_what_the_registry_holds(self):
        self.assertEqual(
            set(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", flat=True)),
            {MARK, ARRIVED, NOTE, STATUS, COUNTS, TAKEN_BY},
        )

    def test_no_field_is_sensitive_and_the_server_owned_fields_are_not_writable(self):
        fields = {f.key: f for f in FieldDefinition.objects.filter(key__startswith=RESOURCE)}
        self.assertEqual({k for k, f in fields.items() if f.sensitive}, set())
        self.assertEqual(
            {k for k, f in fields.items() if not f.writable}, {STATUS, COUNTS, TAKEN_BY},
        )

    def test_who_took_the_register_travels_under_two_names_and_one_switch_covers_both(self):
        taker = FieldDefinition.objects.get(key=TAKEN_BY)
        self.assertEqual(taker.api_names, ["submitted_at", "submitted_by_name"])

    def test_without_a_switch_every_field_is_readable_everywhere(self):
        self.submitted()
        self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:30", note="Bus")

        response = self.staff_day(self.registrar)
        group = self.group(response, self.lekki)
        row = self.staff_rows(response, self.lekki)[self.peter.pk]

        for name in ("status", "counts", "submitted_at", "submitted_by_name"):
            self.assertIn(name, group)
        for name in ("mark", "arrived_at", "note", "role", "exit"):
            self.assertIn(name, row)
        self.assertEqual((row["mark"], row["arrived_at"], row["note"]), ("L", "08:30", "Bus"))


class ReadSwitchTests(_Switches):
    def test_a_role_that_may_not_read_the_mark_does_not_see_it(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "A")
        self.close(MARK)

        self.assertNotIn("mark", self.row())
        self.assertIn("arrived_at", self.row())

    def test_a_role_that_may_not_read_the_arrival_time_does_not_see_it(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:30")
        self.close(ARRIVED)

        row = self.row()
        self.assertNotIn("arrived_at", row)
        self.assertEqual(row["mark"], "L")

    def test_a_role_that_may_not_read_the_note_does_not_see_it(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "A", note="Phoned in")
        self.close(NOTE)

        self.assertNotIn("note", self.row())

    def test_a_role_that_may_not_read_the_status_or_counts_sees_neither(self):
        self.close(STATUS, COUNTS)

        group = self.group(self.staff_day(self.registrar), self.lekki)

        self.assertNotIn("status", group)
        self.assertNotIn("counts", group)
        self.assertIn("rows", group)

    def test_a_role_that_may_not_read_who_took_it_sees_neither_name_nor_time(self):
        self.submitted()
        self.close(TAKEN_BY)

        group = self.group(self.staff_day(self.registrar), self.lekki)

        self.assertNotIn("submitted_at", group)
        self.assertNotIn("submitted_by_name", group)

    def test_the_switches_hold_in_the_group_a_write_sends_back(self):
        self.close(COUNTS, STATUS, NOTE)

        response = self.mark_staff(self.registrar, self.lekki, self.peter, "P")
        group = self.group(response, self.lekki)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("counts", group)
        self.assertNotIn("status", group)
        self.assertNotIn("note", group["rows"][0])
        self.assertEqual(group["rows"][0]["mark"], "P")

    def test_the_switches_hold_for_the_people_with_no_home_branch_too(self):
        self.close(MARK, NOTE)

        row = self.data(self.staff_day(self.registrar))["no_home_branch"]["rows"][0]

        self.assertNotIn("mark", row)
        self.assertNotIn("note", row)
        self.assertEqual(row["lock_reason"], "NO_HOME_BRANCH")

    def test_a_branch_bound_role_is_held_to_the_same_switches(self):
        self.close(MARK)

        row = self.staff_rows(self.staff_day(self.lekki_hr), self.lekki)[self.peter.pk]

        self.assertNotIn("mark", row)


class WriteSwitchTests(_Switches):
    def test_a_role_that_may_not_write_the_mark_cannot_mark_or_fill_in(self):
        self.close(MARK, keep_read=True)

        marked = self.mark_staff(self.registrar, self.lekki, self.peter, "P")
        filled = self.fill_staff(self.registrar, self.lekki)

        self.assertEqual(marked.status_code, 403)
        self.assertEqual(filled.status_code, 403)
        self.assertEqual(self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.peter.pk]["mark"], None)

    def test_a_role_that_may_not_write_the_note_can_still_mark_without_one(self):
        self.close(NOTE, keep_read=True)

        refused = self.mark_staff(self.registrar, self.lekki, self.peter, "A", note="Phoned in")
        allowed = self.mark_staff(self.registrar, self.lekki, self.peter, "A")

        self.assertEqual(refused.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_a_role_that_may_not_write_the_arrival_time_cannot_mark_somebody_late(self):
        self.close(ARRIVED, keep_read=True)

        response = self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:30")

        self.assertEqual(response.status_code, 403)

    def test_submitting_needs_no_field_write(self):
        self.lekki_everyone_marked()
        self.close(MARK, NOTE, ARRIVED, keep_read=True)

        self.assertEqual(self.submit_staff(self.registrar, self.lekki).status_code, 200)


class StaffManagementSwitchTests(_Switches):
    def exiting(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="RESIGNED", exit_date=TODAY + dt.timedelta(days=5),
        )

    def test_the_job_title_follows_staff_managements_switch(self):
        self.close(JOB_TITLE)

        rows = self.staff_rows(self.staff_day(self.registrar), self.lekki)

        self.assertIsNone(rows[self.funke.pk]["role"])
        self.assertEqual(rows[self.eze_staff.pk]["role"], "Class teacher · JSS1 A")
        self.assertNotIn("Bursar", repr(rows))

    def test_a_class_teachers_title_is_dropped_but_the_class_they_lead_is_kept(self):
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(job_title="Head of Year")
        self.close(JOB_TITLE)

        row = self.row(self.eze_staff)

        self.assertEqual(row["role"], "Class teacher · JSS1 A")

    def test_the_classes_a_person_leads_follow_the_class_teacher_switch(self):
        install_declared_fields("academics.classes")
        set_field_access(
            self.staff_role, "academics.classes.class_teacher", read=False, write=False,
        )

        self.assertNotIn("JSS1 A", repr(self.row(self.eze_staff)))

    def declares_employment_status(self):
        if EMPLOYMENT_STATUS not in self.keys:
            self.skipTest("Staff Management declares no employment status switch.")

    def test_the_exit_block_carries_the_status_and_the_day_when_both_are_readable(self):
        self.exiting()

        exit_block = self.row(self.funke)["exit"]

        self.assertEqual(exit_block["label"], "Resigned")
        self.assertEqual(exit_block["last_working_day"], TODAY + dt.timedelta(days=5))
        self.assertTrue(exit_block["text"].startswith("Resigned \u00b7 last working day "))

    def test_without_the_exit_date_the_status_stays_and_the_day_goes(self):
        self.declares_employment_status()
        self.exiting()
        self.close(EXIT_DATE)

        exit_block = self.row(self.funke)["exit"]

        self.assertEqual(exit_block, {"text": "Resigned", "label": "Resigned"})
        self.assertNotIn("last working day", repr(self.data(self.staff_day(self.registrar))))

    def test_without_the_status_the_day_stays_and_the_status_goes(self):
        self.declares_employment_status()
        self.exiting()
        self.close(EMPLOYMENT_STATUS)

        exit_block = self.row(self.funke)["exit"]

        self.assertNotIn("label", exit_block)
        self.assertTrue(exit_block["text"].startswith("Last working day "))
        self.assertNotIn("Resigned", repr(self.row(self.funke)))

    def test_with_neither_the_exit_block_is_absent_from_every_row(self):
        self.declares_employment_status()
        self.exiting()
        self.close(EXIT_DATE, EMPLOYMENT_STATUS)

        response = self.staff_day(self.registrar)

        for row in self.group(response, self.lekki)["rows"]:
            self.assertNotIn("exit", row)
        self.assertNotIn("Resigned", repr(self.data(response)))

    def test_hiding_the_exit_date_does_not_hide_the_person(self):
        self.exiting()
        self.close(EXIT_DATE)

        self.assertIn(self.funke.pk, self.staff_rows(self.staff_day(self.registrar), self.lekki))

    def test_the_register_switches_do_not_touch_what_staff_management_governs(self):
        self.exiting()
        self.close(MARK, ARRIVED, NOTE, STATUS, COUNTS, TAKEN_BY)

        row = self.row(self.funke)

        self.assertEqual(row["role"], "Bursar")
        self.assertEqual(row["exit"]["label"], "Resigned")
