"""A child who leaves is billed no more; a bill keeps the term it was raised for.

Tunde leaves Corona owing N150,000. His account is deactivated when he is
withdrawn, so no later run bills him, and the N150,000 stays on the debtor list.
Readmitted, he is billed again.

A bill's term is stamped when it is raised. Re-linking the structure to Second
Term moves only the runs still to come, and a run for Second Term bills again.
Collections for a term are what was applied to that term's bills, the same figure
the finance dashboard shows.
"""

from __future__ import annotations

from schools.core.fal.adapters.django_finance import (
    DjangoFeeTermBridgeAdapter,
    DjangoFinanceReadAdapter,
)
from schools.core.fal.contracts import Period

from .base import FALFixture


class LeavingTheRollTests(FALFixture):
    def setUp(self):
        super().setUp()
        self.tunde = self.student(self.corona, self.ikeja)
        self.handle = self.student_customer(self.corona_books, self.tunde.pk, branch=self.ikeja)

    def customer(self):
        from vs_finance.models import Customer

        return Customer.objects.get(pk=self.handle.customer_ref)

    def move(self, to_status):
        from schools.vs_students.services.status import transition

        self.tunde.refresh_from_db()
        transition(self.tunde, to_status, actor=self.bursar, reason="Family moved abroad.")

    def test_a_withdrawn_child_is_deactivated_and_readmitted_child_reactivated(self):
        from schools.vs_students.constants import StudentStatus

        self.move(StudentStatus.WITHDRAWN)
        self.assertFalse(self.customer().is_active)

        self.move(StudentStatus.ENROLLED)
        self.assertTrue(self.customer().is_active)

    def test_a_graduate_is_deactivated(self):
        from schools.vs_students.constants import StudentStatus

        self.move(StudentStatus.GRADUATED)
        self.assertFalse(self.customer().is_active)

    def test_a_suspension_leaves_billing_alone(self):
        from schools.vs_students.constants import StudentStatus

        self.move(StudentStatus.SUSPENDED)
        self.assertTrue(self.customer().is_active)

    def test_a_withdrawn_child_is_skipped_by_a_run_and_keeps_their_debt(self):
        from schools.vs_students.constants import StudentStatus
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        session, term = self.session_and_term(self.corona)
        first = self.fee_structure(self.corona_books, code="FIRST", amount=150_000)
        bridge.link_term(first.pk, session.pk, term.pk)
        bridge.generate_cohort_invoices(first.pk, (str(self.tunde.pk),)).unwrap()

        self.move(StudentStatus.WITHDRAWN)
        later = self.fee_structure(self.corona_books, code="LATER", amount=150_000)
        bridge.link_term(later.pk, session.pk, term.pk)
        result = bridge.generate_cohort_invoices(later.pk, (str(self.tunde.pk),)).unwrap()

        self.assertEqual(result.invoices_created, ())
        self.assertEqual(result.students_skipped, (str(self.tunde.pk),))
        self.assertEqual(Invoice.objects.filter(customer_id=self.handle.customer_ref).count(), 1)
        debtors = DjangoFinanceReadAdapter().debtors(self.corona.pk).unwrap()
        self.assertEqual([row.outstanding for row in debtors.items], [150_000])

    def test_another_schools_account_is_never_touched(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentCustomerAdapter

        changed = DjangoStudentCustomerAdapter().set_customer_active(
            self.tunde.pk, active=False).unwrap()
        self.assertEqual(changed, 1)


class StampedPeriodTests(FALFixture):
    def setUp(self):
        super().setUp()
        from schools.vs_academics.models import AcademicTerm

        self.bridge = DjangoFeeTermBridgeAdapter()
        self.session, self.first = self.session_and_term(self.corona)
        self.second = AcademicTerm.all_objects.create(
            tenant=self.corona.tenant, session=self.session, name="Second Term",
            order_index=2, start_date=self.first.end_date.replace(day=16),
            end_date=self.first.end_date.replace(year=2027, month=4, day=10),
        )
        self.structure = self.fee_structure(self.corona_books, amount=300_000)
        self.ada = self.student_customer(self.corona_books, "stu-ada", branch=self.ikeja)

    def bill(self):
        return self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-ada",)).unwrap()

    def test_a_bill_keeps_its_term_when_the_structure_is_relinked(self):
        from vs_finance.models import Invoice

        self.bridge.link_term(self.structure.pk, self.session.pk, self.first.pk)
        self.bill()
        self.bridge.link_term(self.structure.pk, self.session.pk, self.second.pk)
        second_run = self.bill()

        self.assertEqual(len(second_run.invoices_created), 1)
        labels = sorted(Invoice.objects.filter(customer_id=self.ada.customer_ref)
                        .values_list("billing_period_label", flat=True))
        self.assertEqual(labels, ["First Term 2026/2027", "Second Term 2026/2027"])
        reader = DjangoFinanceReadAdapter()
        first_term = reader.fee_liability(
            self.corona.pk, period=Period(session_ref=self.session.pk, term_ref=self.first.pk),
        ).unwrap()
        self.assertEqual(first_term.total_billed, 300_000)

    def test_a_term_bills_once(self):
        self.bridge.link_term(self.structure.pk, self.session.pk, self.first.pk)
        self.bill()
        again = self.bill()
        self.assertEqual(again.invoices_created, ())
        self.assertEqual(again.students_skipped, ("stu-ada",))

    def test_a_term_collects_what_was_applied_to_its_bills(self):
        from vs_finance.models import Invoice

        self.bridge.link_term(self.structure.pk, self.session.pk, self.first.pk)
        self.bill()
        invoice = Invoice.objects.get(customer_id=self.ada.customer_ref)
        self.pay(self.corona_books, self.ada.customer_ref, 500_000, invoice=invoice)

        reader = DjangoFinanceReadAdapter()
        term = Period(session_ref=self.session.pk, term_ref=self.first.pk)
        self.assertEqual(reader.collections(self.corona.pk, period=term).unwrap().value, 300_000)
        self.assertEqual(reader.collection_rate(self.corona.pk, period=term).unwrap().value, 10000)
