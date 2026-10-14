"""A student's Results tab lists their corrections beside their holds, newest first."""
from __future__ import annotations

from .amend_base import AmendmentFixture


class HistoryTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def history(self, student):
        return self.data(self.results_tab(self.result_reader, student))["history"]

    def test_a_waiting_correction_is_listed_with_who_asked_and_why(self):
        found = self.raised(self.kelechi, reason="The exam was added up wrongly.")
        [entry] = self.history(self.kelechi)
        self.assertEqual(entry["kind"], "AMENDMENT")
        self.assertEqual(entry["text"], "Correction to Mathematics asked for, First Term")
        self.assertEqual(entry["by"], "Amara Amend")
        block = entry["amendment"]
        self.assertEqual(block["id"], found)
        self.assertEqual(block["status"], "PENDING")
        self.assertEqual(block["subject"]["name"], "Mathematics")
        self.assertEqual(block["term"]["name"], "First Term")
        self.assertEqual(block["amendment_reason"], "The exam was added up wrongly.")
        self.assertEqual(block["raised_by"], "Amara Amend")
        self.assertIsNone(block["decided_by"])
        self.assertIsNone(block["decided_at"])
        self.assertIsNone(block["version"])
        for hold_field in ("reason", "lift_note"):
            self.assertNotIn(hold_field, entry)

    def test_an_approved_correction_names_who_approved_it_and_the_version_it_made(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found, note="Checked the script.")
        [entry] = self.history(self.kelechi)
        self.assertEqual(entry["text"], "Mathematics corrected for First Term")
        self.assertEqual(entry["by"], "Apollo Approve")
        block = entry["amendment"]
        self.assertEqual(
            (block["status"], block["decided_by"], block["version"], block["decision_note"]),
            ("APPROVED", "Apollo Approve", 2, "Checked the script."),
        )
        self.assertEqual(block["raised_by"], "Amara Amend")

    def test_a_rejected_correction_is_listed_with_the_note_it_was_rejected_with(self):
        found = self.raised(self.kelechi)
        self.reject_amendment(self.approver, found, note="The script stands.")
        [entry] = self.history(self.kelechi)
        self.assertEqual(entry["text"], "Correction to Mathematics rejected for First Term")
        self.assertEqual(entry["amendment"]["status"], "REJECTED")
        self.assertEqual(entry["amendment"]["decision_note"], "The script stands.")
        self.assertIsNone(entry["amendment"]["version"])

    def test_corrections_and_holds_are_merged_newest_first(self):
        self.raised(self.amina)
        entries = self.history(self.amina)
        self.assertEqual([e["kind"] for e in entries], ["AMENDMENT", "HOLD_PLACED"])
        self.assertEqual(entries[1]["reason"], "Fees outstanding since September.")

    def test_every_correction_is_its_own_entry(self):
        first = self.raised(self.kelechi)
        self.reject_amendment(self.approver, first, note="No.")
        self.raised(self.kelechi, cells=self.cells(None, None, 55))
        entries = self.history(self.kelechi)
        self.assertEqual([e["amendment"]["status"] for e in entries], ["PENDING", "REJECTED"])

    def test_a_student_nobody_corrected_has_no_amendment_entry(self):
        self.raised(self.kelechi)
        self.assertEqual(self.history(self.tunde), [])

    def test_a_correction_is_never_listed_for_another_student(self):
        self.raised(self.kelechi)
        self.assertEqual(self.history(self.chiamaka), [])
