"""A debtor row carries the date its oldest unpaid bill fell due.

Ada owes two bills. The one raised first is not the one that fell due first,
because the second carries a short due date. The row names the earlier due date
(not the earlier invoice date), the same date the ageing bucket is read from.
Tunde's only bill carries no due date, so its invoice date stands in. Bola has
paid in full and is not a debtor.
"""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import (
    DjangoFeeTermBridgeAdapter,
    DjangoFinanceReadAdapter,
)
from schools.core.fal.contracts import AgeingBucket
from vs_config.clock import branch_today

from .base import FALFixture


class OldestUnpaidOnTests(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        session, term = cls.session_and_term(cls.corona)
        cls.today = branch_today(cls.corona.tenant, cls.ikeja.pk)

        cls.ada = cls.student_customer(cls.corona_books, "stu-ada", branch=cls.ikeja)
        cls.tunde = cls.student_customer(cls.corona_books, "stu-tunde", branch=cls.ikeja)
        cls.bola = cls.student_customer(cls.corona_books, "stu-bola", branch=cls.ikeja)

        tuition = cls.fee_structure(cls.corona_books, code="TUITION", amount=300_000)
        books = cls.fee_structure(cls.corona_books, code="BOOKS", amount=50_000)
        for structure in (tuition, books):
            bridge.link_term(structure.pk, session.pk, term.pk)
        bridge.generate_cohort_invoices(
            tuition.pk, ("stu-ada", "stu-tunde", "stu-bola"), branch_ref=cls.ikeja.pk,
        )
        bridge.generate_cohort_invoices(books.pk, ("stu-ada",), branch_ref=cls.ikeja.pk)

        days = datetime.timedelta
        ada_tuition = Invoice.objects.get(customer_id=cls.ada.customer_ref, total=300_000)
        ada_books = Invoice.objects.get(customer_id=cls.ada.customer_ref, total=50_000)
        # Raised first, due last; raised later, due first.
        Invoice.objects.filter(pk=ada_tuition.pk).update(
            invoice_date=cls.today - days(100), due_date=cls.today - days(10),
        )
        Invoice.objects.filter(pk=ada_books.pk).update(
            invoice_date=cls.today - days(60), due_date=cls.today - days(45),
        )
        Invoice.objects.filter(customer_id=cls.tunde.customer_ref).update(
            invoice_date=cls.today - days(70), due_date=None,
        )
        bola_invoice = Invoice.objects.get(customer_id=cls.bola.customer_ref)
        cls.pay(cls.corona_books, cls.bola.customer_ref, 300_000, invoice=bola_invoice)

    def rows(self):
        page = DjangoFinanceReadAdapter().debtors(self.corona.pk).unwrap()
        return {row.student_ref: row for row in page.items}

    def test_the_earlier_due_date_wins_over_the_earlier_invoice_date(self):
        row = self.rows()["stu-ada"]

        self.assertEqual(row.oldest_unpaid_on, self.today - datetime.timedelta(days=45))
        self.assertIs(row.ageing, AgeingBucket.DAYS_31_60)

    def test_a_bill_without_a_due_date_falls_back_to_its_invoice_date(self):
        row = self.rows()["stu-tunde"]

        self.assertEqual(row.oldest_unpaid_on, self.today - datetime.timedelta(days=70))
        self.assertIs(row.ageing, AgeingBucket.DAYS_61_90)

    def test_a_child_who_has_paid_in_full_is_not_listed(self):
        self.assertNotIn("stu-bola", self.rows())
