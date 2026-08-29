"""Component 5: what the dashboards, reports and portals read.

Every assertion here is about one school's books never showing another's, and
about an empty answer being told apart from an unavailable one.
"""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import (
    DjangoFeeTermBridgeAdapter,
    DjangoFinanceReadAdapter,
)
from schools.core.fal.contracts import (
    AgeingBucket,
    FilterClause,
    InvoiceStatus,
    Period,
    Unit,
)
from schools.core.fal.exceptions import CrossTenantError, InvalidFilterError

from .base import FALFixture


class _ReadFixture(FALFixture):
    """Corona bills three children across two branches; Greenfield bills one."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        bridge.link_term(cls.structure.pk, cls.session.pk, cls.term.pk)

        cls.ada = cls.student_customer(cls.corona_books, "stu-ada", branch=cls.ikeja)
        cls.tunde = cls.student_customer(cls.corona_books, "stu-tunde", branch=cls.lekki)
        cls.shared = cls.student_customer(cls.corona_books, "stu-shared")
        bridge.generate_cohort_invoices(
            cls.structure.pk, ("stu-ada", "stu-tunde", "stu-shared"),
        )

        # Greenfield's own books, so every Corona assertion has something to
        # wrongly include if the scoping is broken.
        cls.green_session, cls.green_term = cls.session_and_term(cls.greenfield)
        cls.green_structure = cls.fee_structure(cls.greenfield_books, amount=900_000)
        bridge.link_term(cls.green_structure.pk, cls.green_session.pk, cls.green_term.pk)
        cls.green_child = cls.student_customer(cls.greenfield_books, "stu-green")
        bridge.generate_cohort_invoices(cls.green_structure.pk, ("stu-green",))

        # Ada pays in full; Tunde pays half; the school-wide child pays nothing.
        cls.ada_invoice = Invoice.objects.get(customer_id=cls.ada.customer_ref)
        cls.tunde_invoice = Invoice.objects.get(customer_id=cls.tunde.customer_ref)
        cls.pay(cls.corona_books, cls.ada.customer_ref, 300_000,
                invoice=cls.ada_invoice)
        cls.pay(cls.corona_books, cls.tunde.customer_ref, 150_000,
                invoice=cls.tunde_invoice)

    def setUp(self):
        super().setUp()
        self.reader = DjangoFinanceReadAdapter()


class HeadlineKpiTests(_ReadFixture):
    def test_collections_count_only_this_school(self):
        result = self.reader.collections(self.corona.pk)

        self.assertTrue(result.is_available)
        self.assertEqual(result.value.value, 450_000)
        self.assertIs(result.value.unit, Unit.KOBO)

    def test_outstanding_counts_only_unsettled_balances(self):
        # 300,000 billed each. Ada nil, Tunde 150,000, shared 300,000.
        self.assertEqual(
            self.reader.outstanding(self.corona.pk).unwrap().value, 450_000,
        )

    def test_a_branch_narrows_every_headline(self):
        self.assertEqual(
            self.reader.collections(self.corona.pk, branch_ref=self.lekki.pk)
            .unwrap().value,
            150_000,
        )

    def test_the_collection_rate_is_integer_basis_points(self):
        rate = self.reader.collection_rate(self.corona.pk).unwrap()

        # 450,000 collected of 900,000 billed.
        self.assertEqual(rate.value, 5000)
        self.assertEqual(rate.scale, 10000)
        self.assertIs(rate.unit, Unit.RATIO)

    def test_a_school_that_has_billed_nothing_has_collected_all_of_it(self):
        """Reporting 0% for a school with no invoices reads as a crisis."""
        rate = self.reader.collection_rate(self.greenfield.pk, branch_ref=None,
                                           period=None)
        self.assertTrue(rate.is_available)

        empty = self.reader.collection_rate(
            self.greenfield.pk, branch_ref=self.greenfield_main.pk,
        ).unwrap()
        self.assertEqual(empty.value, 10000)

    def test_debtor_count_counts_families_not_invoices(self):
        self.assertEqual(self.reader.debtor_count(self.corona.pk).unwrap().value, 2)

    def test_payment_trend_groups_by_month(self):
        series = self.reader.payment_trend(self.corona.pk).unwrap()

        self.assertEqual([p.label for p in series.points], ["2026-10"])
        self.assertEqual(series.points[0].value, 450_000)

    def test_an_empty_scope_is_available_zero_and_never_unavailable(self):
        """A reachable but empty source is a zero. An unreachable one is not."""
        result = self.reader.collections(
            self.greenfield.pk, branch_ref=self.greenfield_main.pk,
        )

        self.assertTrue(result.is_available)
        self.assertEqual(result.value.value, 0)


class DashboardContractTests(_ReadFixture):
    def test_ageing_buckets_by_days_past_due(self):
        from vs_finance.models import Invoice

        today = datetime.date.today()
        Invoice.objects.filter(pk=self.tunde_invoice.pk).update(
            due_date=today - datetime.timedelta(days=45),
        )
        Invoice.objects.filter(customer_id=self.shared.customer_ref).update(
            due_date=today - datetime.timedelta(days=120),
        )

        report = self.reader.ar_ageing(self.corona.pk).unwrap()
        by_bucket = {row.bucket: row for row in report.buckets}

        self.assertEqual(by_bucket[AgeingBucket.DAYS_31_60].total, 150_000)
        self.assertEqual(by_bucket[AgeingBucket.DAYS_90_PLUS].total, 300_000)
        self.assertEqual(report.total_outstanding, 450_000)

    def test_every_bucket_is_present_even_when_empty(self):
        report = self.reader.ar_ageing(self.corona.pk).unwrap()

        self.assertEqual(len(report.buckets), 5)
        self.assertEqual({row.bucket for row in report.buckets}, set(AgeingBucket))

    def test_fee_liability_reports_billed_collected_and_outstanding(self):
        liability = self.reader.fee_liability(self.corona.pk).unwrap()

        self.assertEqual(liability.total_billed, 900_000)
        self.assertEqual(liability.total_collected, 450_000)
        self.assertEqual(liability.total_outstanding, 450_000)

    def test_a_period_narrows_to_the_structures_linked_to_it(self):
        liability = self.reader.fee_liability(
            self.corona.pk,
            period=Period(session_ref=self.session.pk, term_ref=self.term.pk),
        ).unwrap()

        self.assertEqual(liability.total_billed, 900_000)

    def test_a_period_with_no_linked_structures_is_zero_not_everything(self):
        later, later_term = self.session_and_term(self.corona, name="2027/2028")

        liability = self.reader.fee_liability(
            self.corona.pk,
            period=Period(session_ref=later.pk, term_ref=later_term.pk),
        ).unwrap()

        self.assertEqual(liability.total_billed, 0)

    def test_greenfields_books_never_appear_in_coronas_ageing(self):
        report = self.reader.ar_ageing(self.greenfield.pk).unwrap()

        self.assertEqual(report.total_outstanding, 900_000)


class DetailListTests(_ReadFixture):
    def test_debtors_lists_only_families_who_owe(self):
        page = self.reader.debtors(self.corona.pk).unwrap()

        self.assertEqual(page.total_items, 2)
        self.assertEqual(
            {row.student_ref for row in page.items}, {"stu-tunde", "stu-shared"},
        )

    def test_debtors_are_ordered_by_what_they_owe(self):
        page = self.reader.debtors(self.corona.pk).unwrap()

        self.assertEqual(page.items[0].outstanding, 300_000)

    def test_a_page_carries_its_counts_and_an_empty_page_is_still_a_page(self):
        page = self.reader.debtors(
            self.greenfield.pk, branch_ref=self.greenfield_main.pk,
        ).unwrap()

        self.assertEqual(page.items, ())
        self.assertEqual(page.total_items, 0)
        self.assertEqual(page.total_pages, 1)

    def test_paging_slices_rather_than_truncating(self):
        first = self.reader.debtors(self.corona.pk, page=1, page_size=1).unwrap()
        second = self.reader.debtors(self.corona.pk, page=2, page_size=1).unwrap()

        self.assertEqual(first.total_items, 2)
        self.assertEqual(len(first.items), 1)
        self.assertNotEqual(first.items[0].student_ref, second.items[0].student_ref)

    def test_fee_rows_carry_the_real_term_label(self):
        """The payoff of a foreign key: the label is read, not remembered."""
        page = self.reader.fee_invoices(self.corona.pk).unwrap()

        self.assertTrue(page.items)
        self.assertEqual(page.items[0].term_label, "2026/2027 First Term")

    def test_fee_rows_carry_the_cash_axis_not_the_ledger_lifecycle(self):
        page = self.reader.fee_invoices(
            self.corona.pk,
            filters=(FilterClause("customer_ref", "eq", self.ada.customer_ref),),
        ).unwrap()

        self.assertIs(page.items[0].status, InvoiceStatus.PAID)

    def test_a_partially_paid_invoice_reads_as_partial(self):
        page = self.reader.fee_invoices(
            self.corona.pk,
            filters=(FilterClause("customer_ref", "eq", self.tunde.customer_ref),),
        ).unwrap()

        self.assertIs(page.items[0].status, InvoiceStatus.PARTIAL)

    def test_payments_list_this_schools_receipts_only(self):
        page = self.reader.payments(self.corona.pk).unwrap()

        self.assertEqual(page.total_items, 2)
        self.assertEqual(sum(row.amount for row in page.items), 450_000)

    def test_a_filter_on_a_field_the_source_does_not_expose_is_refused(self):
        with self.assertRaises(InvalidFilterError):
            self.reader.fee_invoices(
                self.corona.pk,
                filters=(FilterClause("customer__billing_email", "contains", "@"),),
            )

    def test_an_unsupported_operator_is_refused(self):
        with self.assertRaises(InvalidFilterError):
            self.reader.fee_invoices(
                self.corona.pk, filters=(FilterClause("status", "regex", ".*"),),
            )

    def test_a_refused_filter_never_reaches_the_database(self):
        """The whitelist runs before the queryset, which is the no-raw-SQL rule."""
        with self.assertNumQueries(0):
            with self.assertRaises(InvalidFilterError):
                from schools.core.fal.adapters.django_finance import _filter_q

                _filter_q("payments", (FilterClause("secret", "eq", 1),))


class PerStudentViewTests(_ReadFixture):
    def test_a_childs_fee_position_is_their_own(self):
        status = self.reader.fee_status("stu-tunde").unwrap()

        self.assertEqual(status.total_billed, 300_000)
        self.assertEqual(status.total_paid, 150_000)
        self.assertEqual(status.balance, 150_000)
        self.assertEqual(len(status.invoices), 1)

    def test_a_child_who_has_never_been_billed_reads_as_zero_not_missing(self):
        result = self.reader.fee_status("stu-nobody")

        self.assertTrue(result.is_available)
        self.assertEqual(result.value.balance, 0)
        self.assertEqual(result.value.invoices, ())

    def test_invoices_carry_their_lines(self):
        views = self.reader.invoices_for("stu-ada").unwrap()

        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].lines[0].description, "Tuition")
        self.assertEqual(views[0].lines[0].amount, 300_000)

    def test_history_can_be_excluded(self):
        settled = self.reader.invoices_for("stu-ada", include_history=False).unwrap()
        outstanding = self.reader.invoices_for(
            "stu-tunde", include_history=False,
        ).unwrap()

        self.assertEqual(settled, ())
        self.assertEqual(len(outstanding), 1)

    def test_siblings_at_one_school_combine(self):
        total = self.reader.combined_balance(("stu-tunde", "stu-shared")).unwrap()

        self.assertEqual(total, 450_000)

    def test_children_at_different_schools_never_combine(self):
        """A guardian with a child at each school gets two bills, not one total."""
        with self.assertRaises(CrossTenantError):
            self.reader.combined_balance(("stu-tunde", "stu-green"))
