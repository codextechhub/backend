"""The pupil portal's home, gathered from every module in one read.

Chiamaka (BFS/001) is in Brightfield's JSS1 A, whose timetable is published,
on Monday 13 October 2025. She owes ₦45,000.00 across two invoices: Second
Term tuition due 30 November and a ₦5,000.00 club levy due 31 October, which
falls due first. A notice for the whole school has just been posted. When the
books cannot be read, her home says so in the fees block and still shows her
classes, her mark and her notices.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal.contracts import FeeStatus, InvoiceLine, InvoiceStatus, InvoiceView
from schools.core.fal.testing import FakeFinanceReader, unavailable_finance_reader
from schools.vs_attendance.tests.base import NOW
from schools.vs_notices.constants import AudienceKind, NoticeStatus
from schools.vs_notices.models import Notice, NoticeAudience

from .test_reads import PortalReadsFixture


def _invoice(ref, title, amount, paid, due):
    return InvoiceView(
        invoice_ref=ref, student_ref="", term_label=title,
        lines=(InvoiceLine(description=title, amount=amount),),
        amount_due=amount, amount_paid=paid, balance=amount - paid,
        status=InvoiceStatus.PARTIAL if paid else InvoiceStatus.UNPAID,
        document_number=f"INV-{ref:04d}", invoice_date=dt.date(2025, 9, 15), due_date=due,
    )


class PortalHomeTests(PortalReadsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        notice = Notice.all_objects.create(
            tenant=cls.tenant, title="Inter-house sports on Friday",
            body="Wear your house colours.", status=NoticeStatus.PUBLISHED,
            published_at=NOW, created_by=cls.admin, published_by=cls.admin,
        )
        NoticeAudience.objects.create(
            tenant=cls.tenant, notice=notice, kind=AudienceKind.SCHOOL,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(fal_registry.reset)
        fal_registry.set_finance_reader(FakeFinanceReader(fee_status=FeeStatus(
            student_ref=str(self.chiamaka.pk), balance=4_500_000,
            total_billed=5_500_000, total_paid=1_000_000,
            invoices=(
                _invoice(11, "Second Term tuition", 5_000_000, 1_000_000, dt.date(2025, 11, 30)),
                _invoice(12, "Club levy", 500_000, 0, dt.date(2025, 10, 31)),
            ),
        )))

    def home(self):
        response = self.portal("student-portal-home")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_home_gathers_a_block_from_every_module(self):
        data = self.home()
        self.assertEqual(data["date"], dt.date(2025, 10, 13))

        self.assertTrue(data["today"]["available"])
        self.assertTrue(data["today"]["published"])
        self.assertIn("state", data["today"]["periods"][0])

        fees = data["fees"]
        self.assertEqual(fees["balance"], 4_500_000)
        self.assertEqual(fees["status"], "OWING")
        self.assertEqual(fees["open_invoice"]["title"], "Club levy")
        self.assertEqual(fees["open_invoice_count"], 2)

        notices = data["notices"]
        self.assertEqual(notices["latest"]["title"], "Inter-house sports on Friday")
        self.assertTrue(notices["latest"]["unread"])
        self.assertEqual(notices["unread_count"], 1)

        self.assertEqual(data["notifications"], {"available": True, "unread_count": 0})

    def test_todays_mark_reads_not_taken_until_the_register_is_in(self):
        attendance = self.home()["attendance"]
        self.assertEqual(attendance["today"]["state"], "NOT_TAKEN")
        self.assertIsNone(attendance["today"]["mark_label"])

        self.mark(self.admin, self.jss1a, self.chiamaka, "L", arrived_at="08:20")
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        attendance = self.home()["attendance"]
        self.assertEqual(attendance["today"]["mark"], "L")
        self.assertEqual(attendance["today"]["mark_label"], "Late")
        self.assertEqual(attendance["rate"], 100)

    def test_books_that_cannot_be_read_are_unavailable_never_zero(self):
        fal_registry.set_finance_reader(unavailable_finance_reader())
        with self.assertLogs("schools.vs_student_portal.home", level="ERROR"):
            data = self.home()
        self.assertEqual(data["fees"], {
            "available": False,
            "message": "Your fees cannot be shown right now. Open Fees to try again.",
        })
        self.assertTrue(data["today"]["available"])
        self.assertTrue(data["notices"]["available"])

    def test_a_block_that_fails_leaves_the_others_standing(self):
        with mock.patch(
            "schools.vs_student_portal.services.timetable", side_effect=RuntimeError("grid"),
        ), self.assertLogs("schools.vs_student_portal.home", level="ERROR"):
            data = self.home()
        self.assertFalse(data["today"]["available"])
        self.assertNotIn("periods", data["today"])
        for name in ("attendance", "fees", "notices", "notifications"):
            self.assertTrue(data[name]["available"], name)

    def test_staff_get_404(self):
        response = TenantAPIClient(self.admin).get(reverse("student-portal-home"))
        self.assertEqual(response.status_code, 404)
