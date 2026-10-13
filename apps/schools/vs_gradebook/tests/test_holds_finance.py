"""The holds list against the real Finance reader, not a fake.

Four children of one school: Ada and Bola owe, Chidi has paid in full and Dayo
was never billed. The reader is asked for three of them, and the answer must
name only the ones asked for, with Ada's bill dated by when it fell due.
"""
from __future__ import annotations

import datetime as dt

from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.tests.base import FALFixture
from vs_config.clock import branch_today

from ..services import holds


class RealFinanceReaderTests(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        session, term = cls.session_and_term(cls.corona)
        cls.today = branch_today(cls.corona.tenant, cls.ikeja.pk)
        cls.ada = cls.student(cls.corona, cls.ikeja, first="Ada", last="Okoro")
        cls.bola = cls.student(cls.corona, cls.ikeja, first="Bola", last="Peters")
        cls.chidi = cls.student(cls.corona, cls.ikeja, first="Chidi", last="Quadri")
        cls.dayo = cls.student(cls.corona, cls.ikeja, first="Dayo", last="Raji")
        customers = {
            student: cls.student_customer(cls.corona_books, str(student.pk), branch=cls.ikeja)
            for student in (cls.ada, cls.bola, cls.chidi)
        }
        tuition = cls.fee_structure(cls.corona_books, code="TUITION", amount=300_000)
        DjangoFeeTermBridgeAdapter().link_term(tuition.pk, session.pk, term.pk)
        DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
            tuition.pk, tuple(str(s.pk) for s in customers), branch_ref=cls.ikeja.pk,
        )
        Invoice.objects.filter(customer_id=customers[cls.ada].customer_ref).update(
            invoice_date=cls.today - dt.timedelta(days=100),
            due_date=cls.today - dt.timedelta(days=40),
        )
        paid = Invoice.objects.get(customer_id=customers[cls.chidi].customer_ref)
        cls.pay(cls.corona_books, customers[cls.chidi].customer_ref, 300_000, invoice=paid)

    def test_the_students_asked_for_are_answered_and_nobody_else(self):
        finance = holds.read_finance(self.corona.tenant, [self.ada, self.chidi, self.dayo])
        self.assertTrue(finance.available)
        self.assertEqual(set(finance.owed), {self.ada.pk})
        owed, oldest = finance.owed[self.ada.pk]
        self.assertEqual(owed, 300_000)
        self.assertEqual(oldest, self.today - dt.timedelta(days=40))

    def test_a_child_who_was_never_billed_or_has_paid_is_absent_and_so_owes_nothing(self):
        finance = holds.read_finance(self.corona.tenant, [self.chidi, self.dayo])
        self.assertTrue(finance.available)
        self.assertEqual(finance.owed, {})

    def test_asking_for_nobody_does_not_ask_finance(self):
        finance = holds.read_finance(self.corona.tenant, [])
        self.assertTrue(finance.available)
        self.assertEqual(finance.owed, {})
