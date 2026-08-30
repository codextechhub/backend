"""Components 1, 2 and 3: books, the term link, and a student's AR account."""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import (
    DjangoEntityResolverAdapter,
    DjangoFeeTermBridgeAdapter,
    DjangoStudentCustomerAdapter,
)
from schools.core.fal.exceptions import (
    AmbiguousPrimaryEntity,
    CrossTenantError,
    CustomerNotProvisioned,
    EntityNotProvisioned,
    InvalidTermLinkError,
    TermNotLinkedError,
)
from schools.core.fal.models import FeeStructureTermLink

from .base import FALFixture


class EntityResolverTests(FALFixture):
    """Component 1. The books a school gets, and never gets twice."""

    def setUp(self):
        super().setUp()
        self.resolver = DjangoEntityResolverAdapter()

    def test_provisioning_is_idempotent(self):
        again = self.resolver.provision_entity(
            self.corona.pk, code="CORONA", name="Corona Secondary",
        ).unwrap()

        self.assertEqual(again.entity_ref, self.corona_books.entity_ref)
        self.assertFalse(again.was_created)

    def test_a_retry_that_asks_for_a_different_code_still_finds_the_same_books(self):
        """Retrying onboarding must not mint a second entity under a new code."""
        from vs_finance.models import LedgerEntity

        again = self.resolver.provision_entity(
            self.corona.pk, code="DIFFERENT", name="Corona Secondary",
        ).unwrap()

        self.assertEqual(again.code, "CORONA")
        self.assertEqual(
            LedgerEntity.objects.filter(tenant=self.corona.tenant).count(), 1,
        )

    def test_the_books_are_usable_and_not_merely_present(self):
        """A bare LedgerEntity would pass a shallow test and fail the first invoice."""
        from vs_finance.models import Account, FiscalPeriod

        self.assertTrue(
            Account.objects.filter(
                entity_id=self.corona_books.entity_ref, code="1200",
            ).exists()
        )
        self.assertEqual(
            FiscalPeriod.objects.filter(
                entity_id=self.corona_books.entity_ref,
            ).count(),
            12,
        )

    def test_the_books_belong_to_the_schools_tenant_not_the_platforms(self):
        """LedgerEntity.save() falls back to Codex when nobody passes a tenant."""
        from vs_finance.models import LedgerEntity

        entity = LedgerEntity.objects.get(pk=self.corona_books.entity_ref)
        self.assertEqual(entity.tenant_id, self.corona.tenant_id)

    def test_a_code_owned_by_another_school_is_refused(self):
        school = self._school_without_books()

        with self.assertRaises(CrossTenantError):
            self.resolver.provision_entity(
                school.pk, code="CORONA", name="Impostor Academy",
            )

    def test_resolving_a_school_with_no_books_raises(self):
        school = self._school_without_books()

        with self.assertRaises(EntityNotProvisioned):
            self.resolver.resolve_entity(school.pk)

    def test_resolving_never_provisions(self):
        from vs_finance.models import LedgerEntity

        school = self._school_without_books()
        with self.assertRaises(EntityNotProvisioned):
            self.resolver.resolve_entity(school.pk)

        self.assertFalse(LedgerEntity.objects.filter(tenant=school.tenant).exists())

    def test_two_candidate_primaries_are_refused_rather_than_guessed(self):
        from vs_finance.models import LedgerEntity

        LedgerEntity.objects.create(
            tenant=self.corona.tenant, name="Corona Second Books", code="CORONA2",
            kind=LedgerEntity.Kind.TENANT, base_currency_id="NGN", is_active=True,
        )

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.resolver.resolve_entity(self.corona.pk)

    def test_an_invisible_school_is_not_distinguishable_from_a_missing_one(self):
        with self.assertRaises(CrossTenantError):
            self.resolver.resolve_entity(9_999_999)

    def _school_without_books(self):
        from schools.vs_schools.models import School

        return School.objects.create(
            slug="no-books", name="No Books Academy", code="NB-1", status="ACTIVE",
        )


class StudentCustomerTests(FALFixture):
    """Component 3. One AR account per child, whatever the concurrency."""

    def setUp(self):
        super().setUp()
        self.port = DjangoStudentCustomerAdapter()

    def test_first_billing_creates_and_a_repeat_finds(self):
        first = self.port.ensure_customer(
            "stu-1", entity_ref=self.corona_books.entity_ref, name="Mrs Adeyemi",
        ).unwrap()
        second = self.port.ensure_customer(
            "stu-1", entity_ref=self.corona_books.entity_ref, name="Mrs Adeyemi",
        ).unwrap()

        self.assertTrue(first.was_created)
        self.assertFalse(second.was_created)
        self.assertEqual(first.customer_ref, second.customer_ref)

    def test_the_customer_carries_the_loose_student_reference(self):
        from vs_finance.models import Customer

        handle = self.port.ensure_customer(
            "stu-2", entity_ref=self.corona_books.entity_ref, name="Mr Bello",
        ).unwrap()

        customer = Customer.objects.get(pk=handle.customer_ref)
        self.assertEqual(customer.source_type, "vs_students.Student")
        self.assertEqual(customer.source_id, "stu-2")

    def test_a_school_wide_customer_needs_no_branch(self):
        """A nullable branch is a first-class case, not a gap to fill."""
        from vs_finance.models import Customer

        handle = self.port.ensure_customer(
            "stu-3", entity_ref=self.greenfield_books.entity_ref, name="Ms Chukwu",
        ).unwrap()

        self.assertIsNone(Customer.objects.get(pk=handle.customer_ref).branch_id)

    def test_a_branch_from_another_school_is_refused(self):
        with self.assertRaises(CrossTenantError):
            self.port.ensure_customer(
                "stu-4", entity_ref=self.corona_books.entity_ref, name="Mr Okafor",
                branch_ref=self.greenfield_main.pk,
            )

    def test_a_child_already_billed_elsewhere_cannot_be_billed_here(self):
        self.port.ensure_customer(
            "stu-5", entity_ref=self.greenfield_books.entity_ref, name="Mrs Eze",
        )

        with self.assertRaises(CrossTenantError):
            self.port.ensure_customer(
                "stu-5", entity_ref=self.corona_books.entity_ref, name="Mrs Eze",
            )

    def test_a_child_who_attends_another_school_cannot_be_billed_here(self):
        """The check the specification always wanted, and could not have.

        Before Module 11 the FAL could only notice a child who was *already*
        billed elsewhere. It can now ask the roll directly, so Corona is refused
        the moment it tries to open an account for a Greenfield pupil, rather
        than at the second school to try.
        """
        greenfield_pupil = self.student(
            self.greenfield, self.greenfield_main, first="Chidi",
        )

        with self.assertRaises(CrossTenantError):
            self.port.ensure_customer(
                str(greenfield_pupil.pk),
                entity_ref=self.corona_books.entity_ref, name="Mrs Okonkwo",
            )

    def test_the_school_a_child_actually_attends_may_bill_them(self):
        pupil = self.student(self.corona, self.ikeja)

        handle = self.port.ensure_customer(
            str(pupil.pk), entity_ref=self.corona_books.entity_ref,
            name="Mrs Adeyemi", branch_ref=self.ikeja.pk,
        ).unwrap()

        self.assertTrue(handle.was_created)

    def test_a_reference_that_names_no_student_is_still_accepted(self):
        """A school that imported receivables before its roll keeps them.

        The refs are opaque strings because the ledger stores them as strings,
        and refusing one that resolves to nothing would strand every AR account
        opened before Module 11 existed.
        """
        handle = self.port.ensure_customer(
            "legacy-0042", entity_ref=self.corona_books.entity_ref,
            name="Mrs Legacy",
        ).unwrap()

        self.assertTrue(handle.was_created)

    def test_an_account_is_opened_in_the_childs_own_name(self):
        """Decided 2026-08-30: the account is the child's, not the payer's."""
        from vs_finance.models import Customer

        pupil = self.student(self.corona, self.ikeja, first="Tunde", last="Adeyemi")
        self.guardian_of(self.corona, pupil, full_name="Mrs Adeyemi")

        handle = self.port.ensure_customer(
            str(pupil.pk), entity_ref=self.corona_books.entity_ref,
        ).unwrap()

        self.assertEqual(Customer.objects.get(pk=handle.customer_ref).name,
                         "Tunde Adeyemi")

    def test_an_account_lands_in_the_childs_own_branch(self):
        """The customer decides where a receivable is filed, and the child
        decides the customer."""
        from vs_finance.models import Customer

        pupil = self.student(self.corona, self.lekki, first="Ada")

        handle = self.port.ensure_customer(
            str(pupil.pk), entity_ref=self.corona_books.entity_ref,
        ).unwrap()

        self.assertEqual(Customer.objects.get(pk=handle.customer_ref).branch_id,
                         self.lekki.pk)

    def test_a_caller_may_still_name_the_account_itself(self):
        from vs_finance.models import Customer

        pupil = self.student(self.corona, self.ikeja)

        handle = self.port.ensure_customer(
            str(pupil.pk), entity_ref=self.corona_books.entity_ref,
            name="The Adeyemi Family",
        ).unwrap()

        self.assertEqual(Customer.objects.get(pk=handle.customer_ref).name,
                         "The Adeyemi Family")

    def test_customer_for_never_creates(self):
        from vs_finance.models import Customer

        result = self.port.customer_for(
            "stu-6", entity_ref=self.corona_books.entity_ref,
        )

        self.assertTrue(result.is_available)
        self.assertIsNone(result.value)
        self.assertFalse(
            Customer.objects.filter(source_id="stu-6").exists()
        )


class FeeTermBridgeTests(FALFixture):
    """Component 2. Nothing is billed that cannot be attributed to a term."""

    def setUp(self):
        super().setUp()
        self.bridge = DjangoFeeTermBridgeAdapter()
        self.session, self.term = self.session_and_term(self.corona)
        self.structure = self.fee_structure(self.corona_books)

    def test_linking_is_idempotent_and_relinking_updates_in_place(self):
        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        link = self.bridge.link_term(
            self.structure.pk, self.session.pk, None,
        ).unwrap()

        self.assertIsNone(link.term_ref)
        self.assertEqual(
            FeeStructureTermLink.objects.filter(fee_structure=self.structure).count(), 1,
        )

    def test_a_session_from_another_school_is_refused(self):
        other_session, _term = self.session_and_term(self.greenfield)

        with self.assertRaises(CrossTenantError):
            self.bridge.link_term(self.structure.pk, other_session.pk)

    def test_a_term_from_a_different_session_is_refused(self):
        later, later_term = self.session_and_term(self.corona, name="2027/2028")

        with self.assertRaises(InvalidTermLinkError):
            self.bridge.link_term(self.structure.pk, self.session.pk, later_term.pk)
        self.assertIsNotNone(later)

    def test_a_term_in_use_for_billing_cannot_be_deleted(self):
        """The whole point of a real foreign key rather than a stored string."""
        from django.db.models import ProtectedError

        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)

        with self.assertRaises(ProtectedError):
            self.term.delete()

    def test_billing_an_unlinked_structure_is_refused(self):
        with self.assertRaises(TermNotLinkedError):
            self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-1",))

    def test_a_cohort_is_billed_once_however_often_the_run_repeats(self):
        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        for ref in ("stu-1", "stu-2"):
            self.student_customer(self.corona_books, ref)

        first = self.bridge.generate_cohort_invoices(
            self.structure.pk, ("stu-1", "stu-2"),
        ).unwrap()
        second = self.bridge.generate_cohort_invoices(
            self.structure.pk, ("stu-1", "stu-2"),
        ).unwrap()

        self.assertEqual(len(first.invoices_created), 2)
        self.assertEqual(first.total_billed, 600_000)
        self.assertEqual(second.invoices_created, ())
        self.assertEqual(set(second.students_skipped), {"stu-1", "stu-2"})

    def test_a_cohort_run_opens_an_account_for_a_child_who_has_none(self):
        """What this method was always specified to do, and finally can.

        It refused before Module 11 because opening an account needs a name and
        there was no roll to read one from.
        """
        from vs_finance.models import Customer

        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        pupil = self.student(self.corona, self.ikeja, first="Ngozi")

        result = self.bridge.generate_cohort_invoices(
            self.structure.pk, (str(pupil.pk),),
        ).unwrap()

        self.assertEqual(len(result.invoices_created), 1)
        self.assertTrue(
            Customer.objects.filter(
                entity_id=self.corona_books.entity_ref,
                source_id=str(pupil.pk), name="Ngozi Adeyemi",
            ).exists()
        )

    def test_a_child_with_no_ar_account_stops_the_run_rather_than_vanishing(self):
        """Silently dropping the child is how a school under-bills and never knows."""
        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        self.student_customer(self.corona_books, "stu-1")

        with self.assertRaises(CustomerNotProvisioned):
            self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-1", "stu-9"))

    def test_nothing_is_billed_when_the_run_is_refused(self):
        from vs_finance.models import Invoice

        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        self.student_customer(self.corona_books, "stu-1")

        with self.assertRaises(CustomerNotProvisioned):
            self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-1", "stu-9"))

        self.assertFalse(
            Invoice.objects.filter(entity_id=self.corona_books.entity_ref).exists()
        )

    def test_billing_a_child_of_another_school_is_refused(self):
        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        self.student_customer(self.greenfield_books, "stu-green")

        with self.assertRaises(CrossTenantError):
            self.bridge.generate_cohort_invoices(self.structure.pk, ("stu-green",))

    def test_billing_a_period_the_structure_does_not_cover_is_refused(self):
        from schools.core.fal.contracts import Period

        self.bridge.link_term(self.structure.pk, self.session.pk, self.term.pk)
        self.student_customer(self.corona_books, "stu-1")
        _later, later_term = self.session_and_term(self.corona, name="2027/2028")

        with self.assertRaises(InvalidTermLinkError):
            self.bridge.generate_cohort_invoices(
                self.structure.pk, ("stu-1",),
                period=Period(session_ref=self.session.pk, term_ref=later_term.pk),
            )
        self.assertIsNotNone(datetime.date.today())
