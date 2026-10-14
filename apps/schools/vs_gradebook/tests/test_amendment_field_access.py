"""Which amendment fields a role reads and changes, proved on every route that carries them.

The two switches of ``gradebook.amendment`` are the reason a correction was
asked for and the note it was decided with. The cells a correction replaces are
marks, so they are governed by the **sheet's** mark and figures switches, not by
switches of their own. Each is closed on the role that raises or decides, and
proved on the drawer, the list, the answers to raising and deciding, and the
Results tab's history. Writes are proved on the raw body, before any record is
touched.
"""
from __future__ import annotations

from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from schools.vs_gradebook.models import Amendment

from .amend_base import AmendmentFixture

REASON = "gradebook.amendment.amendment_reason"
NOTE = "gradebook.amendment.decision_note"
MARK = "gradebook.sheet.mark"
ROW_RESULT = "gradebook.sheet.row_result"
HOLD_REASON = "gradebook.hold.reason"

LINE_FIGURES = ("total", "obtainable", "percentage", "grade", "remark", "is_fail")


class _Switches(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("gradebook.amendment", "gradebook.sheet", "gradebook.hold")
        cls.published()

    def close(self, role, *keys, read=False, write=False):
        set_field_access(role, *keys, read=read, write=write)

    def the_row(self, user=None, **query):
        [row] = self.data(self.amendments_list(user or self.approver, **query))
        return row


class DeclarationTests(_Switches):
    def test_the_registry_holds_the_two_switches_and_they_are_read_and_written(self):
        rows = FieldDefinition.objects.filter(key__startswith="gradebook.amendment.")
        self.assertEqual({r.key: r.writable for r in rows}, {REASON: True, NOTE: True})
        self.assertEqual({r.api_names[0] for r in rows}, {"amendment_reason", "decision_note"})

    def test_both_start_open(self):
        for row in FieldDefinition.objects.filter(key__startswith="gradebook.amendment."):
            self.assertEqual(row.default_access, {"read": True, "write": True})


class DefaultAccessTests(_Switches):
    def test_with_no_switch_closed_every_field_is_on_every_route(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found, note="Checked.")
        row = self.the_row()
        for key in ("amendment_reason", "decision_note", "before", "after"):
            self.assertIn(key, row)
        for line in (row["before"], row["after"]):
            for key in ("cells", *LINE_FIGURES):
                self.assertIn(key, line)
        [entry] = self.data(self.results_tab(self.result_reader, self.kelechi))["history"]
        self.assertIn("amendment_reason", entry["amendment"])
        self.assertIn("decision_note", entry["amendment"])


class ReadSwitchTests(_Switches):
    def test_hiding_the_reason_removes_it_from_the_list_the_answers_and_the_history(self):
        self.close(self.approve_role, REASON)
        self.close(self.create_role, REASON)
        self.close(self.result_role, REASON)
        found = self.raised(self.kelechi, user=self.both_keys)
        answer = self.data(self.reject_amendment(self.approver, found, note="No."))
        self.assertNotIn("amendment_reason", answer)
        self.assertIn("decision_note", answer)
        row = self.the_row()
        self.assertNotIn("amendment_reason", row)
        self.assertIn("decision_note", row)
        [entry] = self.data(self.results_tab(self.result_reader, self.kelechi))["history"]
        self.assertNotIn("amendment_reason", entry["amendment"])
        self.assertEqual(entry["amendment"]["status"], "REJECTED")

    def test_hiding_the_decision_note_removes_it_everywhere_and_keeps_the_reason(self):
        self.close(self.approve_role, NOTE)
        self.close(self.result_role, NOTE)
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found, note="Checked.")
        row = self.the_row()
        self.assertNotIn("decision_note", row)
        self.assertIn("amendment_reason", row)
        [entry] = self.data(self.results_tab(self.result_reader, self.kelechi))["history"]
        self.assertNotIn("decision_note", entry["amendment"])
        self.assertIn("amendment_reason", entry["amendment"])

    def test_the_hold_switch_does_not_govern_the_amendment_reason_and_the_reverse(self):
        self.close(self.result_role, HOLD_REASON)
        self.raised(self.amina)
        entries = self.data(self.results_tab(self.result_reader, self.amina))["history"]
        by_kind = {e["kind"]: e for e in entries}
        self.assertIn("amendment_reason", by_kind["AMENDMENT"]["amendment"])
        self.assertNotIn("reason", by_kind["HOLD_PLACED"])

    def test_hiding_the_marks_leaves_the_figures_and_no_cells_on_every_route(self):
        self.close(self.approve_role, MARK)
        self.close(self.create_role, MARK)
        found = self.raised(self.kelechi, user=self.both_keys)
        row = self.the_row()
        for line in (row["before"], row["after"]):
            self.assertNotIn("cells", line)
            self.assertEqual(line["total"] in (40, 70), True)
        drawer = self.data(self.amendment_form(self.approver, self.kelechi))
        self.assertNotIn("cells", drawer)
        self.assertEqual(drawer["total"], 40)
        self.assertEqual(drawer["pending"]["id"], found)

    def test_hiding_the_figures_leaves_the_cells_and_no_totals_on_every_route(self):
        self.close(self.approve_role, ROW_RESULT)
        self.raised(self.kelechi)
        row = self.the_row()
        for line in (row["before"], row["after"]):
            self.assertIn("cells", line)
            for key in LINE_FIGURES:
                self.assertNotIn(key, line)
        drawer = self.data(self.amendment_form(self.approver, self.kelechi))
        self.assertIn("cells", drawer)
        for key in ("total", "obtainable", "percentage", "grade", "remark"):
            self.assertNotIn(key, drawer)

    def test_a_role_that_may_read_neither_the_marks_nor_the_figures_sees_empty_lines(self):
        self.close(self.approve_role, MARK, ROW_RESULT)
        self.raised(self.kelechi)
        row = self.the_row()
        self.assertEqual(row["before"], {})
        self.assertEqual(row["after"], {})
        self.assertEqual(row["amendment_reason"], "Marked the paper wrongly.")


class WriteSwitchTests(_Switches):
    def test_a_role_that_may_not_write_the_reason_cannot_raise_a_correction(self):
        self.close(self.create_role, REASON, read=True, write=False)
        for name in ("reason", "amendment_reason"):
            with self.subTest(name):
                body = self.amendment_body(self.kelechi)
                body.pop("reason")
                body[name] = "Marked wrongly."
                response = self.post_amendment(self.amender, body)
                self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Amendment.objects.count(), 0)

    def test_a_role_that_may_not_write_marks_cannot_raise_a_correction(self):
        self.close(self.create_role, MARK, read=True, write=False)
        response = self.raise_amendment(self.amender, self.kelechi)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Amendment.objects.count(), 0)

    def test_a_role_that_may_not_write_the_note_cannot_decide_with_one(self):
        found = self.raised(self.kelechi)
        self.close(self.approve_role, NOTE, read=True, write=False)
        for name in ("note", "decision_note"):
            with self.subTest(name):
                self.assertEqual(
                    self.reject_amendment(self.approver, found, **{name: "No."}).status_code, 403,
                )
                self.assertEqual(
                    self.approve_amendment(self.approver, found, **{name: "Yes."}).status_code, 403,
                )
        self.assertEqual(Amendment.objects.get(pk=found).status, "PENDING")

    def test_a_role_that_may_not_write_the_note_can_still_approve_without_one(self):
        found = self.raised(self.kelechi)
        self.close(self.approve_role, NOTE, read=True, write=False)
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)

    def test_the_hold_switches_do_not_govern_an_amendments_reason(self):
        self.close(self.create_role, HOLD_REASON, read=False, write=False)
        self.assertEqual(self.raise_amendment(self.amender, self.kelechi).status_code, 201)
