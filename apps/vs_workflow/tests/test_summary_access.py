"""An approval summary names the Field Access key behind each value and is cut per reader."""
from unittest import mock

from django.test import SimpleTestCase

from vs_workflow.presentation import summary_fields, summary_for_reader

READ = "vs_rbac.field_enforcement.can_read"


def card():
    return {
        "title": "BX-1",
        "subtitle": "Transfer between own accounts",
        "fields": summary_fields([
            ("Amount", "₦10.00", "finance.banktransfer.amount"),
            ("Note", "Float", ("finance.banktransfer.narration", "finance.banktransfer.reference")),
            ("From", "Ikeja GTBank"),
            ("Blank", ""),
            ("Missing", None, "finance.banktransfer.amount"),
        ]),
    }


class SummaryFieldsTests(SimpleTestCase):
    def test_a_fact_with_no_value_is_left_out_and_a_key_is_recorded_beside_its_value(self):
        self.assertEqual(card()["fields"], [
            {"label": "Amount", "value": "₦10.00", "access": "finance.banktransfer.amount"},
            {"label": "Note", "value": "Float", "access": "finance.banktransfer.narration",
             "access_also": ["finance.banktransfer.reference"]},
            {"label": "From", "value": "Ikeja GTBank"},
        ])


class SummaryForReaderTests(SimpleTestCase):
    def shown(self, readable, request="a request"):
        with mock.patch(READ, side_effect=lambda req, key: key in readable):
            return summary_for_reader(card(), None, request=request)

    def test_a_reader_of_every_key_sees_every_field(self):
        every = {"finance.banktransfer.amount", "finance.banktransfer.narration",
                 "finance.banktransfer.reference"}
        self.assertEqual(len(self.shown(every)["fields"]), 3)

    def test_a_closed_key_drops_its_field_and_unkeyed_fields_stay(self):
        shown = self.shown({"finance.banktransfer.narration", "finance.banktransfer.reference"})
        self.assertEqual([f["label"] for f in shown["fields"]], ["Note", "From"])

    def test_a_value_governed_by_two_keys_needs_both(self):
        shown = self.shown({"finance.banktransfer.amount", "finance.banktransfer.narration"})
        self.assertEqual([f["label"] for f in shown["fields"]], ["Amount", "From"])

    def test_title_and_subtitle_are_untouched(self):
        shown = self.shown(set())
        self.assertEqual(shown["title"], "BX-1")
        self.assertEqual(shown["subtitle"], "Transfer between own accounts")
        self.assertEqual([f["label"] for f in shown["fields"]], ["From"])

    def test_no_request_judges_nobody(self):
        self.assertEqual(len(summary_for_reader(card(), None)["fields"]), 3)

    def test_the_stored_summary_is_not_rewritten(self):
        stored = card()
        before = [dict(f) for f in stored["fields"]]
        with mock.patch(READ, return_value=False):
            summary_for_reader(stored, None, request="a request")
        self.assertEqual(stored["fields"], before)
