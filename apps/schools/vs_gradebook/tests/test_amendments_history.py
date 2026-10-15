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


class CanDecideTests(AmendmentFixture):
    """Whether the reader may decide a correction now: waiting, and not raised by them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def row(self, user, amendment_id):
        rows = self.data(self.amendments_list(user))
        return next(r for r in rows if r["id"] == amendment_id)

    def test_a_waiting_correction_is_the_approvers_to_decide_and_not_the_raisers(self):
        found = self.raised(self.tunde, user=self.both_keys)
        self.assertTrue(self.row(self.approver, found)["can_decide"])
        self.assertFalse(self.row(self.both_keys, found)["can_decide"])

    def test_a_decided_correction_is_nobodys_to_decide(self):
        found = self.raised(self.tunde)
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        self.assertFalse(self.row(self.approver, found)["can_decide"])
        self.assertFalse(self.row(self.both_keys, found)["can_decide"])

    def test_the_answer_to_raising_says_the_raiser_cannot_decide_it(self):
        response = self.raise_amendment(self.both_keys, self.tunde)
        self.assertFalse(self.data(response)["can_decide"])


class ResultsTabSubjectIdTests(AmendmentFixture):
    """The Results tab names each subject's id, so a correction can be asked for from it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_each_published_subject_carries_its_id(self):
        terms = self.data(self.results_tab(self.result_reader, self.tunde))["terms"]
        published = next(t for t in terms if t["published"])
        maths = next(s for s in published["subjects"] if s["name"] == self.maths.name)
        self.assertEqual(maths["id"], self.maths.pk)
