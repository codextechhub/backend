"""The class column, and the preview that must not bill anybody.

Two defects fixed together because both are about a fee screen telling the
truth: a class column that was hardcoded blank, and a generation run that had
no way to answer "what would this cost?" without answering it in the ledger.
"""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.core.fal.adapters.django_finance import (
    DjangoFeeTermBridgeAdapter,
    DjangoFinanceReadAdapter,
)

from .base import FALFixture


class ClassLabelTests(FALFixture):
    """Every fee list carries the child's class, or says nothing at all."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        DjangoFeeTermBridgeAdapter().link_term(
            cls.structure.pk, cls.session.pk, cls.term.pk,
        )

        # Ada is in a class. Tunde is on the roll and in none - a real shape,
        # not a contrived one: a child enrols before the term's classes are cut.
        cls.ada = cls.student(cls.corona, cls.ikeja, first="Ada", last="Okeye")
        cls.tunde = cls.student(cls.corona, cls.lekki, first="Tunde", last="Bello")
        cls.place_in_class(cls.corona, cls.ada, cls.session, name="JSS1 A",
                           branch=cls.ikeja)

        DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
            cls.structure.pk, (str(cls.ada.pk), str(cls.tunde.pk)),
        )
        cls.reader = DjangoFinanceReadAdapter()

    def _rows_by_student(self, page):
        return {row.student_ref: row for row in page.items}

    def test_debtor_row_carries_the_class(self):
        page = self.reader.debtors(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows[str(self.ada.pk)].class_label, "JSS1 A")

    def test_fee_row_carries_the_class(self):
        page = self.reader.fee_invoices(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows[str(self.ada.pk)].class_label, "JSS1 A")

    def test_a_child_with_no_placement_reads_blank_not_wrong(self):
        """Blank is the honest answer, and it must not borrow Ada's class."""
        page = self.reader.debtors(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows[str(self.tunde.pk)].class_label, "")

    def test_an_ended_placement_does_not_count(self):
        """A child moved out of a class is not still in it."""
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.ada).update(is_active=False)
        page = self.reader.debtors(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows[str(self.ada.pk)].class_label, "")

    def test_the_label_costs_one_query_however_many_children(self):
        """The guard against the N+1 this fix could easily have introduced.

        Asserted as an invariant rather than a fixed number: what matters is
        that a class of thirty costs the same as a class of two, not what the
        constant happens to be today. A magic number here would break the next
        time someone adds a select_related, and would say nothing about N+1.
        """
        with CaptureQueriesContext(connection) as few:
            self.reader.debtors(self.corona.pk, page_size=50).unwrap()

        for i in range(6):
            child = self.student(self.corona, self.ikeja, first=f"Child{i}")
            self.place_in_class(self.corona, child, self.session,
                                name=f"JSS2 {i}", branch=self.ikeja)
            DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
                self.structure.pk, (str(child.pk),),
            )

        with CaptureQueriesContext(connection) as many:
            page = self.reader.debtors(self.corona.pk, page_size=50).unwrap()
            self.assertGreaterEqual(len(page.items), 7)

        self.assertEqual(len(many), len(few))

    def test_a_stale_reference_cannot_borrow_another_school_s_class(self):
        """The leak this fix could have opened, and the reason it is tenant-scoped.

        Corona imported its receivables before its roll, so it holds a customer
        whose source reference is the bare string "7". Greenfield's pupil Chidi
        happens to have primary key 7. Corona's bursar opens the debtor list:
        without the tenant filter, Chidi's class is printed on Corona's screen,
        against a child Corona has never heard of.
        """
        chidi = self.student(self.greenfield, self.greenfield_main,
                             first="Chidi", last="Nwosu")
        green_session, _ = self.session_and_term(self.greenfield, name="2026/2027 G")
        self.place_in_class(self.greenfield, chidi, green_session, name="SS3 B",
                            branch=self.greenfield_main)

        # Built straight on the engine, not through the FAL, because the FAL
        # refuses this shape today - which is exactly why it survives only in
        # books that predate it. The read path still has to cope with it.
        from vs_finance import fees
        from vs_finance.models import Customer, LedgerEntity

        from schools.core.fal.adapters.django_finance import _receivable_account
        from schools.core.fal.contracts import SOURCE_TYPE_STUDENT

        entity = LedgerEntity.objects.get(pk=self.corona_books.entity_ref)
        legacy = Customer.objects.create(
            entity=entity, code="LEGACY-7", name="Legacy Payer",
            receivable_account=_receivable_account(entity),
            source_type=SOURCE_TYPE_STUDENT, source_id=str(chidi.pk),
        )
        fees.generate_invoices(self.structure, [legacy])

        page = self.reader.debtors(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows[str(chidi.pk)].class_label, "")

    def test_a_reference_that_names_nobody_is_left_alone(self):
        """Books imported before the roll keep opaque refs, and must not break."""
        self.student_customer(self.corona_books, "legacy-ref-99")
        DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
            self.structure.pk, ("legacy-ref-99",),
        )
        page = self.reader.debtors(self.corona.pk).unwrap()
        rows = self._rows_by_student(page)
        self.assertEqual(rows["legacy-ref-99"].class_label, "")


class GenerationPreviewTests(FALFixture):
    """A dry run answers the question without writing the answer down."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        DjangoFeeTermBridgeAdapter().link_term(
            cls.structure.pk, cls.session.pk, cls.term.pk,
        )
        cls.ada = cls.student(cls.corona, cls.ikeja, first="Ada", last="Okeye")
        cls.tunde = cls.student(cls.corona, cls.lekki, first="Tunde", last="Bello")
        cls.refs = (str(cls.ada.pk), str(cls.tunde.pk))

    @property
    def bridge(self):
        return DjangoFeeTermBridgeAdapter()

    def _invoice_count(self):
        from vs_finance.models import Invoice

        return Invoice.objects.filter(entity_id=self.corona_books.entity_ref).count()

    def _customer_count(self):
        from vs_finance.models import Customer

        return Customer.objects.filter(entity_id=self.corona_books.entity_ref).count()

    def test_preview_writes_no_invoice(self):
        before = self._invoice_count()
        self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()
        self.assertEqual(self._invoice_count(), before)

    def test_preview_opens_no_ar_account(self):
        """The rollback has to reach the customer the run would have created."""
        before = self._customer_count()
        self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()
        self.assertEqual(self._customer_count(), before)

    def test_preview_says_what_it_would_bill(self):
        result = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()
        self.assertTrue(result.dry_run)
        self.assertEqual(result.invoices_created, ())
        self.assertEqual(set(result.students_to_bill), set(self.refs))
        self.assertEqual(result.total_billed, 600_000)

    def test_the_preview_total_is_the_amount_actually_billed(self):
        """The whole point: the quote a bursar sees is the bill they get."""
        preview = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()
        real = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs,
        ).unwrap()

        self.assertEqual(preview.total_billed, real.total_billed)
        self.assertEqual(set(preview.students_to_bill), set(real.students_to_bill))

    def test_a_preview_does_not_make_the_real_run_skip(self):
        """The trap this fix could set: previewing marks everyone as billed."""
        self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()
        real = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs,
        ).unwrap()

        self.assertEqual(real.students_skipped, ())
        self.assertEqual(len(real.invoices_created), 2)

    def test_preview_reports_an_already_billed_child_as_skipped(self):
        self.bridge.generate_cohort_invoices(self.structure.pk, self.refs).unwrap()
        preview = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs, dry_run=True,
        ).unwrap()

        self.assertEqual(set(preview.students_skipped), set(self.refs))
        self.assertEqual(preview.students_to_bill, ())
        self.assertEqual(preview.total_billed, 0)

    def test_a_real_run_is_not_marked_as_a_preview(self):
        result = self.bridge.generate_cohort_invoices(
            self.structure.pk, self.refs,
        ).unwrap()
        self.assertFalse(result.dry_run)
        self.assertEqual(len(result.invoices_created), 2)

    def test_a_preview_still_refuses_another_school_s_child(self):
        """A preview that hid this would promise a run that then fails."""
        from schools.core.fal.exceptions import CrossTenantError

        outsider = self.student(self.greenfield, self.greenfield_main,
                                first="Chidi", last="Nwosu")
        with self.assertRaises(CrossTenantError):
            self.bridge.generate_cohort_invoices(
                self.structure.pk, (str(outsider.pk),), dry_run=True,
            ).unwrap()
