"""Component 6: the parent portal's one door onto the payment gateway.

The bridge initiates and reads. It never books: settlement stays inside
``vs_payments``' confirm-then-book flow, and a second settlement path is exactly
how a parent gets credited twice.
"""

from __future__ import annotations

from django.test import override_settings

from schools.core.fal.adapters.django_finance import (
    DjangoFeeTermBridgeAdapter,
    DjangoParentPaymentBridgeAdapter,
)
from schools.core.fal.contracts import Unavailable
from schools.core.fal.exceptions import (
    CrossTenantError,
    GuardianLinkNotConfigured,
    PaymentGatewayError,
)
from schools.core.fal.testing import FakeGuardianLink

from .base import FALFixture


@override_settings(PAYMENTS_DEFAULT_PROVIDER="FAKE")
class ParentPaymentBridgeTests(FALFixture):
    """Mrs Adeyemi has one child at Corona. Mr Okafor has one at Greenfield."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        bridge.link_term(cls.structure.pk, cls.session.pk, cls.term.pk)
        cls.ada = cls.student_customer(cls.corona_books, "stu-ada", branch=cls.ikeja)
        bridge.generate_cohort_invoices(cls.structure.pk, ("stu-ada",))
        cls.ada_invoice = Invoice.objects.get(customer_id=cls.ada.customer_ref)

        cls.green_session, cls.green_term = cls.session_and_term(cls.greenfield)
        cls.green_structure = cls.fee_structure(cls.greenfield_books, amount=500_000)
        bridge.link_term(cls.green_structure.pk, cls.green_session.pk,
                         cls.green_term.pk)
        cls.green_child = cls.student_customer(cls.greenfield_books, "stu-green")
        bridge.generate_cohort_invoices(cls.green_structure.pk, ("stu-green",))
        cls.green_invoice = Invoice.objects.get(
            customer_id=cls.green_child.customer_ref,
        )

    def bridge_for(self, links=None):
        return DjangoParentPaymentBridgeAdapter(
            guardian_link=FakeGuardianLink(
                links if links is not None else {"g-adeyemi": {"stu-ada"}},
            ),
        )

    # ----- the shipped default, now that there is a roll ------------------- #
    def test_the_shipped_default_reads_the_real_student_roll(self):
        """The bridge is open, and it is open on the real link.

        The shipped default reads StudentGuardian, so a mother linked to her
        own child is admitted with no resolver injected by the test. Without a
        student roll to answer the ownership question the resolver can only
        refuse, which is why the roll is the default's source.
        """
        student = self.student(self.corona, self.ikeja)
        guardian = self.guardian_of(self.corona, student)
        billed = self.student_customer(
            self.corona_books, str(student.pk), branch=self.ikeja,
        )

        result = DjangoParentPaymentBridgeAdapter().start_payment_session(
            guardian_ref=str(guardian.pk),
            entity_ref=self.corona_books.entity_ref,
            amount=50_000, customer_ref=billed.customer_ref,
        )

        self.assertTrue(result.is_available)
        self.assertTrue(result.value.checkout_url)

    def test_the_shipped_default_refuses_somebody_elses_child(self):
        student = self.student(self.corona, self.ikeja)
        self.guardian_of(self.corona, student)
        stranger = self.guardian_of(
            self.corona, self.student(self.corona, self.ikeja, first="Bola"),
            full_name="Mr Okafor", is_primary=True,
        )
        billed = self.student_customer(
            self.corona_books, str(student.pk), branch=self.ikeja,
        )

        with self.assertRaises(CrossTenantError):
            DjangoParentPaymentBridgeAdapter().start_payment_session(
                guardian_ref=str(stranger.pk),
                entity_ref=self.corona_books.entity_ref,
                amount=50_000, customer_ref=billed.customer_ref,
            )

    def test_a_deployment_with_no_roll_still_fails_closed(self):
        """The refusing resolver is kept for a deployment without Module 11."""
        from schools.core.fal.adapters.django_finance import (
            DenyAllGuardianLinkAdapter,
        )

        bridge = DjangoParentPaymentBridgeAdapter(
            guardian_link=DenyAllGuardianLinkAdapter(),
        )

        with self.assertRaises(GuardianLinkNotConfigured):
            bridge.start_payment_session(
                guardian_ref="g-adeyemi",
                entity_ref=self.corona_books.entity_ref,
                amount=100_000, invoice_ref=self.ada_invoice.pk,
            )

    # ----- ownership ------------------------------------------------------- #
    def test_a_guardian_can_start_a_session_for_their_own_child(self):
        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk,
            payer_email="adeyemi@example.test",
        )

        self.assertTrue(result.is_available)
        self.assertTrue(result.value.checkout_url)

    def test_the_actor_who_pressed_pay_starts_a_portal_collection(self):
        from vs_payments.constants import CollectionOrigin
        from vs_payments.models import CollectionIntent

        actor = self.user_for(self.corona, "adeyemi@corona.test")

        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk, actor=actor,
        )

        intent = CollectionIntent.objects.get(reference=result.value.reference)
        self.assertEqual(intent.origin, CollectionOrigin.PORTAL)
        self.assertEqual(intent.created_by_id, actor.pk)
        self.assertEqual(intent.invoice_id, self.ada_invoice.pk)

    def test_without_an_actor_the_collection_is_a_standard_one(self):
        from vs_payments.constants import CollectionOrigin
        from vs_payments.models import CollectionIntent

        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk,
        )

        intent = CollectionIntent.objects.get(reference=result.value.reference)
        self.assertEqual(intent.origin, CollectionOrigin.STANDARD)
        self.assertIsNone(intent.created_by_id)

    def test_the_invoices_own_books_are_used_when_no_entity_is_named(self):
        from vs_payments.models import CollectionIntent

        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", amount=100_000, invoice_ref=self.ada_invoice.pk,
        )

        intent = CollectionIntent.objects.get(reference=result.value.reference)
        self.assertEqual(intent.entity_id, self.corona_books.entity_ref)

    def test_no_entity_and_no_invoice_is_a_rejection(self):
        with self.assertRaises(PaymentGatewayError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", amount=100_000,
                customer_ref=self.ada.customer_ref,
            )

    def test_an_invoice_found_without_an_entity_is_still_ownership_checked(self):
        from vs_payments.models import CollectionIntent

        with self.assertRaises(CrossTenantError):
            self.bridge_for({"g-okafor": {"stu-green"}}).start_payment_session(
                guardian_ref="g-okafor", amount=100_000,
                invoice_ref=self.ada_invoice.pk,
            )

        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_guardian_cannot_pay_a_child_who_is_not_theirs(self):
        with self.assertRaises(CrossTenantError):
            self.bridge_for({"g-okafor": {"stu-green"}}).start_payment_session(
                guardian_ref="g-okafor", entity_ref=self.corona_books.entity_ref,
                amount=100_000, invoice_ref=self.ada_invoice.pk,
            )

    def test_the_ownership_check_runs_before_anything_is_created(self):
        from vs_payments.models import CollectionIntent

        with self.assertRaises(CrossTenantError):
            self.bridge_for({}).start_payment_session(
                guardian_ref="g-stranger", entity_ref=self.corona_books.entity_ref,
                amount=100_000, invoice_ref=self.ada_invoice.pk,
            )

        self.assertFalse(CollectionIntent.objects.exists())

    def test_an_invoice_from_another_school_is_refused(self):
        with self.assertRaises(CrossTenantError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
                amount=100_000, invoice_ref=self.green_invoice.pk,
            )

    def test_a_customer_and_an_invoice_that_disagree_are_refused(self):
        other = self.student_customer(self.corona_books, "stu-other")

        with self.assertRaises(CrossTenantError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
                amount=100_000, invoice_ref=self.ada_invoice.pk,
                customer_ref=other.customer_ref,
            )

    def test_a_customer_that_is_not_a_students_is_refused(self):
        """A guardian may act on a child's ledger, not on the school's suppliers."""
        from vs_finance.models import Customer

        plain = Customer.objects.create(
            entity_id=self.corona_books.entity_ref, code="PLAIN", name="A Company",
        )

        with self.assertRaises(CrossTenantError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
                amount=100_000, customer_ref=plain.pk,
            )

    # ----- outage versus rejection ----------------------------------------- #
    def test_a_session_with_neither_customer_nor_invoice_is_a_rejection(self):
        with self.assertRaises(PaymentGatewayError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
                amount=100_000,
            )

    def test_an_amount_over_the_invoice_balance_is_a_rejection_not_an_outage(self):
        with self.assertRaises(PaymentGatewayError):
            self.bridge_for().start_payment_session(
                guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
                amount=999_000_000, invoice_ref=self.ada_invoice.pk,
            )

    def test_a_provider_outage_is_unavailable_and_not_an_exception(self):
        """A parent sees "try again shortly", not a stack trace and not a zero."""
        from vs_payments.exceptions import ProviderError
        from vs_payments.providers import registry as provider_registry

        class BrokenProvider:
            name = "FAKE"

            def create_checkout(self, **kwargs):
                raise ProviderError("The gateway timed out.")

        provider_registry.register("FAKE", BrokenProvider())
        self.addCleanup(provider_registry.unregister, "FAKE")

        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk,
        )

        self.assertFalse(result.is_available)
        self.assertEqual(result.reason, Unavailable.GATEWAY_UNAVAILABLE)

    @override_settings(PAYMENTS_DEFAULT_PROVIDER="PAYSTACK", PAYSTACK_SECRET_KEY="")
    def test_an_unconfigured_provider_is_unavailable_with_its_own_reason(self):
        result = self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk,
        )

        self.assertFalse(result.is_available)
        self.assertEqual(result.reason, Unavailable.NOT_CONFIGURED)

    # ----- receipts -------------------------------------------------------- #
    def test_a_guardian_sees_their_own_childs_receipt(self):
        payment = self.pay(
            self.corona_books, self.ada.customer_ref, 100_000,
            invoice=self.ada_invoice,
        )

        receipt = self.bridge_for().receipt_for(
            payment.pk, guardian_ref="g-adeyemi",
        ).unwrap()

        self.assertEqual(receipt.payment_ref, payment.pk)
        self.assertEqual(receipt.amount, 100_000)
        self.assertEqual(receipt.receipt_number, payment.document_number)
        self.assertEqual(receipt.invoice_refs, (self.ada_invoice.pk,))

    def test_a_guardian_cannot_read_another_familys_receipt(self):
        payment = self.pay(
            self.corona_books, self.ada.customer_ref, 100_000,
            invoice=self.ada_invoice,
        )

        with self.assertRaises(CrossTenantError):
            self.bridge_for({"g-okafor": {"stu-green"}}).receipt_for(
                payment.pk, guardian_ref="g-okafor",
            )

    def test_an_unknown_payment_reads_as_no_receipt_rather_than_an_error(self):
        """Same answer as "not yours", so nobody can probe for payment ids."""
        result = self.bridge_for().receipt_for(9_999_999, guardian_ref="g-adeyemi")

        self.assertTrue(result.is_available)
        self.assertIsNone(result.value)

    def test_the_bridge_never_books(self):
        """It creates an intent and stops. Settlement is vs_payments' alone."""
        from vs_finance.models import Payment
        from vs_payments.models import CollectionIntent

        self.bridge_for().start_payment_session(
            guardian_ref="g-adeyemi", entity_ref=self.corona_books.entity_ref,
            amount=100_000, invoice_ref=self.ada_invoice.pk,
        )

        self.assertEqual(CollectionIntent.objects.count(), 1)
        self.assertFalse(Payment.objects.exists())
        self.ada_invoice.refresh_from_db()
        self.assertEqual(self.ada_invoice.amount_paid, 0)

    # ----- the pupil port's invoice PDF, read by the same family screens ---- #
    def test_an_invoice_on_the_pupils_account_renders_as_a_pdf(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        result = DjangoStudentPaymentAdapter().invoice_pdf("stu-ada", self.ada_invoice.pk)

        self.assertTrue(result.value.startswith(b"%PDF"))

    def test_an_invoice_not_on_the_pupils_account_has_no_pdf(self):
        """Another school's invoice and a missing one answer alike."""
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        port = DjangoStudentPaymentAdapter()

        self.assertIsNone(port.invoice_pdf("stu-ada", self.green_invoice.pk).value)
        self.assertIsNone(port.invoice_pdf("stu-ada", 9_999_999).value)
        self.assertIsNone(port.invoice_pdf("stu-nobody", self.ada_invoice.pk).value)

    # ----- the same invoice and receipt as documents, for the family's screen -- #
    def ada_code(self):
        from vs_finance.models import Customer

        return Customer.objects.get(pk=self.ada.customer_ref).code

    def test_the_invoice_document_is_the_printed_invoice_in_kobo(self):
        """Ada's 3,000 bill reads with Corona's letterhead, its line and its totals."""
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter
        from schools.core.fal.contracts import InvoiceStatus

        self.pay(self.corona_books, self.ada.customer_ref, 100_000, invoice=self.ada_invoice)
        doc = DjangoStudentPaymentAdapter().invoice_document("stu-ada", self.ada_invoice.pk).value

        self.assertEqual(doc.document_number, self.ada_invoice.document_number)
        self.assertEqual(doc.letterhead.name, "Corona Secondary")
        self.assertEqual(doc.billed_to.code, self.ada_code())
        self.assertEqual(doc.total, 300_000)
        self.assertEqual(doc.amount_paid, 100_000)
        self.assertEqual(doc.adjustments, 0)
        self.assertEqual(doc.balance_due, 200_000)
        self.assertEqual(doc.status, InvoiceStatus.PARTIAL)
        self.assertEqual(sum(line.amount for line in doc.lines), doc.subtotal)

    def test_an_invoice_not_on_the_pupils_account_has_no_document(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        port = DjangoStudentPaymentAdapter()

        self.assertIsNone(port.invoice_document("stu-ada", self.green_invoice.pk).value)
        self.assertIsNone(port.invoice_document("stu-ada", 9_999_999).value)
        self.assertIsNone(port.invoice_document("stu-nobody", self.ada_invoice.pk).value)

    def test_the_invoice_document_names_the_branchs_collection_account(self):
        from vs_finance.models import Account, BankAccount

        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        port = DjangoStudentPaymentAdapter()
        self.assertIsNone(port.invoice_document("stu-ada", self.ada_invoice.pk).value.bank)

        cash = Account.objects.create(
            entity_id=self.corona_books.entity_ref, code="1109", name="Cash - Ikeja",
            account_type="ASSET",
        )
        BankAccount.objects.create(
            entity_id=self.corona_books.entity_ref, branch=self.ikeja, name="Corona Ikeja",
            bank_name="GTBank", account_number="0123456789", gl_account=cash,
            is_active=True, is_primary_collection=True,
        )
        bank = port.invoice_document("stu-ada", self.ada_invoice.pk).value.bank

        self.assertEqual((bank.bank_name, bank.account_number), ("GTBank", "0123456789"))

    def test_the_receipt_carries_its_letterhead_and_amount_in_words(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        payment = self.pay(self.corona_books, self.ada.customer_ref, 100_000, invoice=self.ada_invoice)
        doc = DjangoStudentPaymentAdapter().receipt("stu-ada", payment.pk).value

        self.assertEqual(doc.letterhead.name, "Corona Secondary")
        self.assertEqual(doc.amount_in_words, "One thousand naira only")
        self.assertEqual(doc.customer_code, self.ada_code())
