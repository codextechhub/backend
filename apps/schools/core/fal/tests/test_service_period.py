"""A term's fees are earned over the term: the FAL bills them with the term's dates.

Greenfield bills Second Term weeks before it starts. The engine is told only the
term's first and last day, as each fee line's service period, and defers the
income until the term's months come. A term already under way is income on the
day it is billed, and a fee linked to the whole session carries the session's
dates. The finance engine never learns what a term is.

The dates are worked out from today in ``setUp``, because the fee run bills on
today's date and the question is always "does the term start after the bill?".
"""

from __future__ import annotations

import datetime

from django.db.models import Sum

from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from vs_config.clock import branch_today

from .base import FALFixture


class ServicePeriodTests(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ada = cls.student_customer(cls.greenfield_books, "stu-ada", branch=cls.greenfield_main)
        cls.structure = cls.fee_structure(cls.greenfield_books, code="TERM", amount=300_000)

    def setUp(self):
        super().setUp()
        from schools.vs_academics.models import AcademicSession, AcademicTerm

        today = branch_today(self.greenfield.tenant, self.greenfield_main.pk)
        self.session = AcademicSession.all_objects.create(
            tenant=self.greenfield.tenant, name="Next Year",
            start_date=today - datetime.timedelta(days=10),
            end_date=today + datetime.timedelta(days=300),
        )
        self.upcoming = AcademicTerm.all_objects.create(
            tenant=self.greenfield.tenant, session=self.session, name="Upcoming Term",
            order_index=2, start_date=today + datetime.timedelta(days=40),
            end_date=today + datetime.timedelta(days=130),
        )
        self.current = AcademicTerm.all_objects.create(
            tenant=self.greenfield.tenant, session=self.session, name="Current Term",
            order_index=1, start_date=today - datetime.timedelta(days=10),
            end_date=today + datetime.timedelta(days=30),
        )
        self.bridge = DjangoFeeTermBridgeAdapter()

    def bill(self, term):
        from vs_finance.models import Invoice

        self.bridge.link_term(self.structure.pk, self.session.pk, getattr(term, "pk", None))
        self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-ada",)).unwrap()
        return Invoice.objects.get(customer_id=self.ada.customer_ref)

    def credited(self, invoice, code):
        total = invoice.journal.lines.filter(account__code=code).aggregate(c=Sum("credit"))["c"]
        return int(total or 0)

    def test_a_term_billed_before_it_starts_carries_its_dates_and_is_deferred(self):
        invoice = self.bill(self.upcoming)

        line = invoice.lines.get()
        self.assertEqual((line.service_start, line.service_end),
                         (self.upcoming.start_date, self.upcoming.end_date))
        self.assertEqual(self.credited(invoice, "2160"), 300_000)
        self.assertEqual(self.credited(invoice, "4100"), 0)
        self.assertTrue(invoice.deferred_income_entries.exists())

    def test_a_term_under_way_is_income_on_the_day(self):
        invoice = self.bill(self.current)

        self.assertEqual(invoice.lines.get().service_start, self.current.start_date)
        self.assertEqual(self.credited(invoice, "4100"), 300_000)
        self.assertFalse(invoice.deferred_income_entries.exists())

    def test_a_whole_session_fee_carries_the_session_dates(self):
        invoice = self.bill(None)

        line = invoice.lines.get()
        self.assertEqual((line.service_start, line.service_end),
                         (self.session.start_date, self.session.end_date))
