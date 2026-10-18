"""A pupil's transfer account is issued when their first bill posts.

Tunde, at Corona's Ikeja branch, is billed for First Term. By the time his
parents open the bill his account number exists, without anybody pressing
"Get my account number", and every later bill is paid into the same number.
Ada at Greenfield, a school with one branch, gets hers the same way.

Where Corona cannot issue one (the provider is off, pupils may not pay, the
family's details are missing, the provider is down) the bill posts all the
same and Tunde's Pay screen keeps its button. Payments go through the fake
provider, which stands in for Paystack.
"""
from __future__ import annotations

import datetime
import threading
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase, override_settings, tag
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal import transfer_accounts
from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.tasks import TRANSFER_ACCOUNT_RETRIES, issue_transfer_account
from schools.core.fal.tests.base import FALFixture
from schools.vs_student_portal.fees import transfer_account
from schools.vs_student_portal.tests.test_fees import give_access
from vs_payments.constants import PaymentAuditAction
from vs_payments.custody import SubaccountMissingError
from vs_payments.exceptions import ProviderError
from vs_payments.models import PaymentEvent, VirtualAccount
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider


def _pupils_accounts(pupil):
    return VirtualAccount.objects.filter(customer__source_id=str(pupil.pk))


class _FirstBillFixture(FALFixture):
    """Corona and Greenfield with a term to bill, and pupils not yet billed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Customer

        cls.bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.tuition = cls.fee_structure(cls.corona_books, amount=300_000)
        cls.bridge.link_term(cls.tuition.pk, cls.session.pk, cls.term.pk)
        cls.bus = cls.fee_structure(cls.corona_books, code="BUS", amount=50_000)
        cls.bridge.link_term(cls.bus.pk, cls.session.pk, cls.term.pk)

        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.bola = cls.student(cls.corona, cls.lekki, first="Bola", last="Okoro")
        cls.tunde_customer = Customer.objects.get(pk=cls.student_customer(
            cls.corona_books, str(cls.tunde.pk), branch=cls.ikeja).customer_ref)
        cls.student_customer(cls.corona_books, str(cls.bola.pk), branch=cls.lekki)

        g_session, g_term = cls.session_and_term(cls.greenfield)
        cls.g_tuition = cls.fee_structure(cls.greenfield_books, amount=200_000)
        cls.bridge.link_term(cls.g_tuition.pk, g_session.pk, g_term.pk)
        cls.ada = cls.student(cls.greenfield, cls.greenfield_main, first="Ada", last="Eze")
        cls.student_customer(cls.greenfield_books, str(cls.ada.pk))

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)
        self.addCleanup(fal_registry.reset)
        self.issued = mock.patch.object(
            self.fake, "create_virtual_account", wraps=self.fake.create_virtual_account,
        ).start()
        self.addCleanup(mock.patch.stopall)

    def bill(self, *pupils, structure=None, dry_run=False):
        """Run the term's fee run for ``pupils`` and let its commit go through."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.bridge.generate_cohort_invoices(
                (structure or self.tuition).pk, tuple(str(p.pk) for p in pupils),
                dry_run=dry_run,
            ).unwrap()

    def manual_invoice(self, customer, amount=20_000):
        """A bursar's own invoice for ``customer``, posted outside any fee run."""
        from vs_finance.models import Invoice, InvoiceLine
        from vs_finance.receivables import post_invoice

        today = datetime.date.today()
        invoice = Invoice.objects.create(
            entity=customer.entity, customer=customer, branch=self.ikeja,
            invoice_date=today, due_date=today + datetime.timedelta(days=30),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=amount,
            revenue_account=self.account(customer.entity_id, "4100"),
        )
        with self.captureOnCommitCallbacks(execute=True):
            post_invoice(invoice, actor_user=self.bursar)
        return invoice

    def not_issued_events(self):
        return PaymentEvent.objects.filter(action=PaymentAuditAction.VIRTUAL_ACCOUNT_NOT_ISSUED)


class FirstBillIssuesTheAccountTests(_FirstBillFixture):

    def test_the_first_bill_issues_one_account_the_pupil_sees_at_once(self):
        self.bill(self.tunde)

        self.assertEqual(_pupils_accounts(self.tunde).count(), 1)
        user = give_access(self.tunde, "COR/001")
        body = TenantAPIClient(user).get(reverse("student-portal-transfer-account")).data["data"]
        self.assertEqual(body["state"], "ISSUED")
        self.assertEqual(body["account_number"], _pupils_accounts(self.tunde).get().account_number)

    def test_a_second_bill_issues_none(self):
        self.bill(self.tunde)
        number = _pupils_accounts(self.tunde).get().account_number

        self.bill(self.tunde, structure=self.bus)
        self.manual_invoice(self.tunde_customer)

        self.assertEqual(self.issued.call_count, 1)
        self.assertEqual(_pupils_accounts(self.tunde).get().account_number, number)

    def test_a_manual_invoice_issues_it_when_it_is_the_first_bill(self):
        self.manual_invoice(self.tunde_customer)
        self.assertEqual(_pupils_accounts(self.tunde).count(), 1)

    def test_a_rerun_of_the_task_issues_nothing_more(self):
        self.bill(self.tunde)
        outcome = issue_transfer_account.apply(args=[self.tunde_customer.pk]).get()
        self.assertEqual(outcome, transfer_accounts.ALREADY_ISSUED)
        self.assertEqual(self.issued.call_count, 1)

    def test_each_pupil_in_a_fee_run_gets_their_own_at_either_branch(self):
        self.bill(self.tunde, self.bola)
        numbers = {_pupils_accounts(p).get().account_number for p in (self.tunde, self.bola)}
        self.assertEqual(len(numbers), 2)
        bola_account = _pupils_accounts(self.bola).get()
        self.assertEqual(bola_account.branch_id, self.lekki.pk)

    def test_a_school_with_one_branch_issues_it_too(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.bridge.generate_cohort_invoices(self.g_tuition.pk, (str(self.ada.pk),)).unwrap()
        account = _pupils_accounts(self.ada).get()
        self.assertEqual(account.entity_id, self.greenfield_books.entity_ref)

    def test_a_fee_run_preview_issues_nothing(self):
        self.bill(self.tunde, dry_run=True)
        self.assertFalse(_pupils_accounts(self.tunde).exists())
        self.issued.assert_not_called()


class NoAccountIsIssuedTests(_FirstBillFixture):

    def test_a_customer_with_no_pupil_behind_it_gets_none(self):
        from vs_finance.models import Customer

        family = Customer.objects.create(
            entity=self.tunde_customer.entity, branch=self.ikeja, code="FAM-OKORO",
            name="Okoro family", receivable_account=self.tunde_customer.receivable_account,
        )
        with mock.patch.object(issue_transfer_account, "delay") as queued:
            self.manual_invoice(family)
        queued.assert_not_called()
        self.assertFalse(VirtualAccount.objects.filter(customer=family).exists())

    def test_a_pupil_reference_naming_nobody_on_the_roll_gets_none(self):
        from vs_finance.models import Customer

        stray = Customer.objects.create(
            entity=self.tunde_customer.entity, branch=self.ikeja, code="STU-GONE",
            name="Imported debtor", source_type=self.tunde_customer.source_type,
            source_id="999999", receivable_account=self.tunde_customer.receivable_account,
        )
        self.manual_invoice(stray)
        self.assertFalse(VirtualAccount.objects.filter(customer=stray).exists())
        self.issued.assert_not_called()

    @override_settings(PAYSTACK_SECRET_KEY="")
    def test_a_provider_that_is_off_issues_none_and_the_screen_says_unavailable(self):
        provider_registry.unregister()
        self.bill(self.tunde)

        self.assertFalse(_pupils_accounts(self.tunde).exists())
        self.assertEqual(transfer_account(self.tunde)["state"], "NONE")
        user = give_access(self.tunde, "COR/001")
        body = TenantAPIClient(user).post(reverse("student-portal-transfer-account")).data["data"]
        self.assertEqual(body["state"], "UNAVAILABLE")
        self.assertIn("bank transfers", body["message"])

    def test_a_provider_that_issues_no_dedicated_accounts_issues_none(self):
        self.issued.side_effect = NotImplementedError
        self.bill(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())
        self.assertFalse(self.not_issued_events().exists())

    def test_a_branch_not_taking_payments_directly_issues_none(self):
        with mock.patch("vs_payments.custody.is_direct", return_value=True), \
                mock.patch("vs_payments.custody.branch_subaccount", side_effect=SubaccountMissingError()):
            self.bill(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

    def test_a_school_that_turns_pupil_payments_off_issues_none(self):
        from django.core.management import call_command
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key="fees.student_payment_enabled")
        set_value(definition=definition, value=False, actor=self.bursar, tenant=self.corona.tenant)
        self.bill(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())
        self.issued.assert_not_called()

    def test_missing_family_details_issue_none_and_say_why_once(self):
        self.fake.virtual_account_requires = ("name", "email", "phone")
        logger = "schools.core.fal.transfer_accounts"
        with self.assertLogs(logger, "WARNING") as logged:
            self.bill(self.tunde)
            self.bill(self.tunde, structure=self.bus)

        self.assertFalse(_pupils_accounts(self.tunde).exists())
        self.issued.assert_not_called()
        event = self.not_issued_events().get()
        self.assertEqual(event.metadata["reason"], "MISSING_DETAILS")
        self.assertEqual(event.metadata["missing"], ["email", "phone"])
        self.assertEqual(event.metadata["customer_id"], self.tunde_customer.pk)
        self.assertFalse(event.succeeded)
        self.assertIn("missing email, phone", event.message)
        # The log names ids and field names, never the family.
        text = "\n".join(logged.output)
        self.assertIn(f"customer={self.tunde_customer.pk}", text)
        self.assertNotIn(self.tunde_customer.name, text)
        self.assertNotIn("Tunde", text)

    def test_details_filled_in_later_let_the_next_bill_issue_it(self):
        self.fake.virtual_account_requires = ("name", "email")
        self.bill(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

        self.tunde_customer.billing_email = "adeyemi.family@example.ng"
        self.tunde_customer.save(update_fields=["billing_email"])
        self.bill(self.tunde, structure=self.bus)
        self.assertEqual(_pupils_accounts(self.tunde).count(), 1)

    def test_the_pay_screen_still_issues_one_for_a_pupil_who_has_none(self):
        self.fake.virtual_account_requires = ("name", "email")
        self.bill(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

        user = give_access(self.tunde, "COR/001")
        url = reverse("student-portal-transfer-account")
        self.assertEqual(TenantAPIClient(user).get(url).data["data"]["state"], "NONE")
        body = TenantAPIClient(user).post(url).data["data"]
        self.assertEqual(body["state"], "ISSUED")
        self.assertEqual(_pupils_accounts(self.tunde).count(), 1)


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class ProviderFailureTests(_FirstBillFixture):
    """The provider is down: the task waits longer each time, then gives up."""

    def run_attempt(self, retries):
        issue_transfer_account.push_request(retries=retries)
        try:
            return issue_transfer_account.run(self.tunde_customer.pk)
        finally:
            issue_transfer_account.pop_request()

    def test_a_provider_error_retries_with_backoff_then_leaves_none(self):
        self.issued.side_effect = ProviderError("Paystack is down")
        waits = []

        class Retrying(Exception):
            pass

        def retry(exc=None, countdown=None, **kwargs):
            waits.append(countdown)
            return Retrying()

        with mock.patch.object(issue_transfer_account, "retry", side_effect=retry):
            for attempt in range(TRANSFER_ACCOUNT_RETRIES):
                with self.assertRaises(Retrying):
                    self.run_attempt(attempt)
                self.assertFalse(_pupils_accounts(self.tunde).exists())
            outcome = self.run_attempt(TRANSFER_ACCOUNT_RETRIES)

        self.assertEqual(waits, [60, 120, 240, 480, 960])
        self.assertEqual(outcome, transfer_accounts.PROVIDER_FAILED)
        self.assertEqual(self.issued.call_count, TRANSFER_ACCOUNT_RETRIES + 1)
        self.assertEqual(transfer_account(self.tunde)["state"], "NONE")
        self.assertEqual(self.not_issued_events().get().metadata["reason"], "PROVIDER_FAILED")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_where_tasks_run_inline_there_is_one_attempt(self):
        self.issued.side_effect = ProviderError("Paystack is down")
        self.bill(self.tunde)
        self.assertEqual(self.issued.call_count, 1)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_bill_posts_whatever_the_provider_does(self):
        from vs_finance.constants import DocumentStatus
        from vs_finance.models import Invoice

        self.issued.side_effect = RuntimeError("the provider's library broke")
        with self.assertLogs("vs_finance.receivables", "ERROR"):
            result = self.bill(self.tunde)

        self.assertEqual(len(result.invoices_created), 1)
        invoice = Invoice.objects.get(customer=self.tunde_customer)
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        self.assertIsNotNone(invoice.journal_id)
        self.assertFalse(_pupils_accounts(self.tunde).exists())


@tag("slow")
class ConcurrentFirstBillsTests(TransactionTestCase):
    """Two of Tunde's bills post at once; he still gets one account."""

    serialized_rollback = True

    def setUp(self):
        from schools.core.fal.adapters.django_finance import DjangoEntityResolverAdapter
        from schools.vs_schools.models import School
        from vs_finance.models import Customer
        from vs_tenants.models import Branch

        corona = School.objects.create(
            slug="corona-secondary", name="Corona Secondary", code="COR-1", status="ACTIVE",
        )
        ikeja = Branch.objects.create(tenant=corona.tenant, name="Ikeja", is_main=True, status="ACTIVE")
        books = DjangoEntityResolverAdapter().provision_entity(
            corona.pk, code="CORONA", name="Corona Secondary",
        ).unwrap()
        self.tunde = FALFixture.student(corona, ikeja)
        self.customer = Customer.objects.get(pk=FALFixture.student_customer(
            books, str(self.tunde.pk), branch=ikeja).customer_ref)
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        self.addCleanup(provider_registry.unregister)
        self.addCleanup(fal_registry.reset)

    def test_two_tasks_racing_issue_one_account(self):
        """The second waits on the first's lock, then finds its account."""
        first_in_provider, release_first = threading.Event(), threading.Event()
        create = self.fake.create_virtual_account
        calls, outcomes = [], {}

        def slow_create(**kwargs):
            calls.append(kwargs["reference"])
            first_in_provider.set()
            release_first.wait(10)
            return create(**kwargs)

        def run(name):
            try:
                outcomes[name] = transfer_accounts.issue_after_first_bill(self.customer.pk)
            finally:
                connections.close_all()

        with mock.patch.object(self.fake, "create_virtual_account", side_effect=slow_create):
            first = threading.Thread(target=run, args=("first",))
            first.start()
            self.assertTrue(first_in_provider.wait(10))
            second = threading.Thread(target=run, args=("second",))
            second.start()
            second.join(1.0)
            self.assertTrue(second.is_alive(), "The second issue did not wait for the lock.")
            release_first.set()
            first.join(10)
            second.join(10)

        self.assertEqual(len(calls), 1)
        self.assertEqual(VirtualAccount.objects.filter(customer=self.customer).count(), 1)
        self.assertEqual(outcomes, {"first": transfer_accounts.ISSUED, "second": transfer_accounts.ISSUED})
