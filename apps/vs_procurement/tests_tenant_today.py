"""Procurement reads the school's calendar day, not the server's UTC day.

At 23:30 UTC on 14 March it is already 00:30 on 15 March in Lagos. A bill that
fell due on the 14th is overdue for a Lagos school from midnight, a contract
that ended on the 14th has lapsed, a milestone ticked off now is completed on
the 15th, and a report run without a date is as at the 15th. Asked of the
server, each of these still answered the 14th for the first hour of the day.
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.test import TestCase

from .tests import _BranchTenantsFixture

#: 23:30 UTC on 14 March: 00:30 on 15 March in Lagos.
LATE_EVENING_UTC = datetime.datetime(2026, 3, 14, 23, 30, tzinfo=datetime.timezone.utc)
THE_14TH = datetime.date(2026, 3, 14)
THE_15TH = datetime.date(2026, 3, 15)


def _at(instant):
    return mock.patch("django.utils.timezone.now", return_value=instant)


class ProcurementTodayIsTheSchoolsDayTests(_BranchTenantsFixture, TestCase):
    """Half past midnight in Lagos, half past eleven the evening before in UTC."""

    def setUp(self):
        super().setUp()
        self.entity = self.multi.entity
        self.vendor = self.multi.vendor

    def contract(self, **fields):
        from .models import VendorContract

        return VendorContract.objects.create(
            entity=self.entity, vendor=self.vendor, reference="CT-LAGOS", title="Cleaning",
            status="ACTIVE", start_date=datetime.date(2025, 3, 15), end_date=THE_14TH, **fields,
        )

    def test_a_bill_due_yesterday_is_overdue_from_midnight(self):
        from vs_finance.constants import DocumentStatus

        from .models import VendorInvoice
        from .serializers import VendorInvoiceSerializer

        bill = VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendor, invoice_date=THE_14TH, due_date=THE_14TH,
            total=10_000, subtotal=10_000, status=DocumentStatus.POSTED,
        )
        with _at(LATE_EVENING_UTC):
            self.assertTrue(VendorInvoiceSerializer(bill, context={}).data["is_overdue"])

    def test_a_contract_that_ended_yesterday_has_lapsed(self):
        from .contracts import mark_expired
        from .serializers import _contract_is_expired

        contract = self.contract()
        with _at(LATE_EVENING_UTC):
            self.assertTrue(_contract_is_expired(contract, {}))
            self.assertEqual(mark_expired(self.entity), 1)

    def test_a_milestone_ticked_off_now_is_completed_today(self):
        from .contracts import complete_milestone
        from .models import ContractMilestone

        milestone = ContractMilestone.objects.create(
            contract=self.contract(), line_no=1, name="Handover", due_date=THE_15TH,
        )
        with _at(LATE_EVENING_UTC):
            complete_milestone(milestone)
        milestone.refresh_from_db()
        self.assertEqual(milestone.completed_date, THE_15TH)

    def test_a_report_run_without_a_date_is_as_at_the_schools_today(self):
        from .reports import ap_aging

        with _at(LATE_EVENING_UTC):
            self.assertEqual(ap_aging(self.entity).as_of, THE_15TH)
