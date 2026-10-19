"""The pupil's fee history is sliced after its priority and date order is set."""
from unittest import mock

from django.test import SimpleTestCase

from schools.vs_student_portal.fees import bill_page, payment_page


class FeePaginationTests(SimpleTestCase):
    def test_outstanding_bills_and_charges_lead_every_page(self):
        invoices = [
            {"invoice_ref": index, "invoice_date": f"2025-{index:02d}-01", "due_date": f"2026-{index:02d}-01",
             "balance": 100 if index <= 9 else 0}
            for index in range(1, 11)
        ]
        invoices.reverse()
        summary = {
            "balance": 900, "total_billed": 1000, "total_paid": 100,
            "can_pay": True, "min_part_payment": 100,
            "invoices": invoices,
            "debit_notes": [{"note_ref": 11, "date": "2026-10-01", "balance": 100}],
        }
        with mock.patch("schools.vs_student_portal.fees.fee_summary", return_value=summary):
            first = bill_page(object(), page=1)
            second = bill_page(object(), page=2)
            paid = bill_page(object(), page=1, status="paid")
            by_year = bill_page(object(), page=1, year="2025")

        self.assertEqual(first["pagination"]["total"], 11)
        self.assertEqual([row["invoice_ref"] for row in first["items"]], list(range(1, 9)))
        self.assertEqual([row["kind"] for row in second["items"]], ["invoice", "charge", "invoice"])
        self.assertEqual(second["items"][-1]["invoice_ref"], 10)
        self.assertEqual(paid["items"][0]["invoice_ref"], 10)
        self.assertEqual(by_year["pagination"]["total"], 10)
        self.assertEqual(first["balance"], 900)

    def test_payment_year_filter_applies_before_page_slice(self):
        rows = [{"reference": str(index), "paid_on": "2026-10-01" if index < 11 else "2025-10-01"}
                for index in range(12)]
        with mock.patch("schools.vs_student_portal.fees.payment_history", return_value=rows):
            first = payment_page(object(), page=1)
            second = payment_page(object(), page=2)
            old = payment_page(object(), page=1, year="2025")

        self.assertEqual((first["pagination"]["total"], len(first["items"])), (12, 10))
        self.assertEqual([row["reference"] for row in second["items"]], ["10", "11"])
        self.assertEqual([row["reference"] for row in old["items"]], ["11"])
        self.assertEqual(first["years"], ["2026", "2025"])
