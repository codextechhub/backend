"""A pupil's own fees, payments and receipts in the portal, and paying.

Tunde is a pupil at Corona with a posted ₦3,000.00 tuition invoice; Bola, at
the same school, has one of her own. Corona's smallest part payment is the
default ₦1,000.00. Paying goes through the fake payment provider, which stands
in for Paystack: Tunde is handed a checkout, the provider later confirms it,
and his history and receipt follow.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.tests.base import FALFixture
from schools.core.fal.testing import unavailable_finance_reader
from schools.vs_students.services import portal_access
from vs_payments import services as payment_services
from vs_payments.constants import CollectionOrigin
from vs_payments.models import CollectionIntent
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider
from vs_user.models import User


def set_school_config(tenant, actor, key, value):
    """Set one ``vs_config`` value for *tenant*, seeding the catalogue first."""
    from django.core.management import call_command
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import set_value

    call_command("seed_config_catalogue", verbosity=0)
    definition = ConfigurationDefinition.objects.get(key=key)
    set_value(definition=definition, value=value, actor=actor, tenant=tenant)


def give_access(pupil, number):
    pupil.student_number = number
    pupil.email = f"pupil{pupil.pk}@example.ng"
    pupil.save(update_fields=["student_number", "email"])
    portal_access.issue(pupil, actor=None)
    pupil.refresh_from_db()
    User.objects.filter(pk=pupil.user_id).update(status=User.Status.ACTIVE, is_active=True)
    return User.objects.get(pk=pupil.user_id)


class _FeesFixture(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        bridge.link_term(cls.structure.pk, cls.session.pk, cls.term.pk)

        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.bola = cls.student(cls.corona, cls.ikeja, first="Bola", last="Okafor")
        for pupil in (cls.tunde, cls.bola):
            cls.student_customer(cls.corona_books, str(pupil.pk), branch=cls.ikeja)
        bridge.generate_cohort_invoices(cls.structure.pk, (str(cls.tunde.pk), str(cls.bola.pk)))
        cls.tunde_invoice = Invoice.objects.get(customer__source_id=str(cls.tunde.pk))
        cls.bola_invoice = Invoice.objects.get(customer__source_id=str(cls.bola.pk))
        cls.tunde_user = give_access(cls.tunde, "COR/001")
        # Card is off unless a school switches it on; these tests pay by card.
        set_school_config(
            cls.corona.tenant, cls.bursar, "fees.family_card_payments_enabled", True,
        )

    def setUp(self):
        super().setUp()
        cache.clear()  # Request limits count in the cache; each test starts at zero.
        self.addCleanup(cache.clear)
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)
        self.addCleanup(fal_registry.reset)

    def as_pupil(self, user=None):
        return TenantAPIClient(user or self.tunde_user)

    def pay(self, amount, invoice=None):
        url = reverse("student-portal-pay", kwargs={"invoice_ref": (invoice or self.tunde_invoice).pk})
        return self.as_pupil().post(url, {"amount": amount}, format="json")

def hold_credit(books, customer_ref, amount, *, branch_id=None):
    """A posted receipt that settles nothing, so the school holds it as the family's credit."""
    from vs_config.clock import tenant_today
    from vs_finance.models import Account, Customer, Payment
    from vs_finance.receivables import post_payment

    customer = Customer.objects.get(pk=customer_ref)
    payment = Payment.objects.create(
        entity_id=books.entity_ref, customer=customer,
        payment_date=tenant_today(customer.entity.tenant),
        branch_id=branch_id or customer.branch_id, amount=amount,
        deposit_account=Account.objects.get(entity_id=books.entity_ref, code="1100"),
    )
    post_payment(payment, allocations=[], auto_allocate=False)
    return payment


class OwingAndCreditTests(_FeesFixture):
    """Credit is shown beside what is owed and never hides an unpaid bill.

    Tunde owes ₦3,000.00 on his term bill while the school holds ₦4,500.00 of
    his credit: the headline reads OWING ₦3,000.00 with the credit alongside,
    where a single netted figure used to read "in credit".
    """

    def fees(self):
        return self.as_pupil().get(reverse("student-portal-fees")).data["data"]

    def home_fees(self):
        response = self.as_pupil().get(reverse("student-portal-home"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["fees"]

    def test_an_open_bill_beside_larger_credit_still_reads_owing(self):
        hold_credit(self.corona_books, self.tunde_invoice.customer_id, 450_000)
        data = self.fees()
        self.assertEqual((data["owing"], data["credit"], data["balance"]), (300_000, 450_000, -150_000))
        self.assertEqual(data["owing"] - data["credit"], data["balance"])
        home = self.home_fees()
        self.assertEqual((home["owing"], home["credit"], home["balance"]), (300_000, 450_000, -150_000))
        self.assertEqual(home["status"], "OWING")
        self.assertEqual(home["open_invoice_count"], 1)

    def test_credit_held_at_another_branch_does_not_net_off_the_bill(self):
        hold_credit(
            self.corona_books, self.tunde_invoice.customer_id, 300_000, branch_id=self.lekki.pk,
        )
        home = self.home_fees()
        self.assertEqual((home["owing"], home["credit"], home["balance"]), (300_000, 300_000, 0))
        self.assertEqual(home["status"], "OWING")

    def test_credit_with_nothing_owed_reads_in_credit(self):
        FALFixture.pay(self.corona_books, self.bola_invoice.customer_id, 300_000, invoice=self.bola_invoice)
        hold_credit(self.corona_books, self.bola_invoice.customer_id, 80_000)
        bola = TenantAPIClient(give_access(self.bola, "COR/002"))
        home = bola.get(reverse("student-portal-home")).data["data"]["fees"]
        self.assertEqual((home["owing"], home["credit"], home["balance"]), (0, 80_000, -80_000))
        self.assertEqual(home["status"], "IN_CREDIT")

    def test_nothing_owed_and_no_credit_reads_clear(self):
        FALFixture.pay(self.corona_books, self.tunde_invoice.customer_id, 300_000, invoice=self.tunde_invoice)
        home = self.home_fees()
        self.assertEqual((home["owing"], home["credit"], home["balance"], home["status"]), (0, 0, 0, "CLEAR"))

    def test_a_debit_note_counts_in_owing_before_any_credit(self):
        from vs_finance.constants import CreditNoteKind
        from vs_finance.credit_notes import post_credit_note
        from vs_finance.models import CreditNote, CreditNoteLine

        note = CreditNote.objects.create(
            entity=self.tunde_invoice.entity, customer=self.tunde_invoice.customer,
            branch=self.tunde_invoice.branch, kind=CreditNoteKind.DEBIT,
            note_date=self.tunde_invoice.invoice_date, reason="Replacement ID card",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, quantity=1, unit_price=25_000,
            revenue_account=self.tunde_invoice.lines.first().revenue_account,
        )
        post_credit_note(note)
        hold_credit(self.corona_books, self.tunde_invoice.customer_id, 100_000)
        data = self.fees()
        self.assertEqual((data["owing"], data["credit"], data["balance"]), (325_000, 100_000, 225_000))


class PortalFeesTests(_FeesFixture):
    # ── reading ──────────────────────────────────────────────────────────

    def test_the_pupil_reads_their_balance_and_invoice(self):
        data = self.as_pupil().get(reverse("student-portal-fees")).data["data"]
        self.assertEqual(data["balance"], 300_000)
        self.assertTrue(data["can_pay"])
        self.assertEqual(data["min_part_payment"], 100_000)
        invoice = data["invoices"][0]
        self.assertEqual(invoice["number"], self.tunde_invoice.document_number)
        self.assertEqual(invoice["status_label"], "Owing")
        self.assertEqual(invoice["lines"][0]["amount"], 300_000)

    def test_paged_bills_keep_the_balance_and_only_return_the_requested_slice(self):
        url = reverse("student-portal-fees")
        first = self.as_pupil().get(url, {"page": 1}).data["data"]
        self.assertEqual(first["balance"], 300_000)
        self.assertTrue(first["has_bills"])
        self.assertEqual(first["pagination"]["total"], 1)
        self.assertEqual(first["items"][0]["invoice_ref"], self.tunde_invoice.pk)
        self.assertEqual(first["items"][0]["kind"], "invoice")
        self.assertNotIn("invoices", first)
        self.assertEqual(self.as_pupil().get(url, {"page": 2}).data["data"]["items"], [])
        self.assertEqual(self.as_pupil().get(url, {"page": 0}).status_code, 400)

    def test_the_pupil_reads_a_charge_added_after_the_bill(self):
        from vs_finance.constants import CreditNoteKind
        from vs_finance.credit_notes import post_credit_note
        from vs_finance.models import CreditNote, CreditNoteLine

        note = CreditNote.objects.create(
            entity=self.tunde_invoice.entity, customer=self.tunde_invoice.customer, branch=self.tunde_invoice.branch,
            kind=CreditNoteKind.DEBIT, note_date=self.tunde_invoice.invoice_date, reason="Replacement ID card",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, quantity=1, unit_price=25_000,
            revenue_account=self.tunde_invoice.lines.first().revenue_account,
        )
        post_credit_note(note)

        data = self.as_pupil().get(reverse("student-portal-fees")).data["data"]
        self.assertEqual((data["balance"], data["total_billed"], data["total_paid"]), (325_000, 325_000, 0))
        [charge] = data["debit_notes"]
        self.assertEqual(charge["reason"], "Replacement ID card")
        self.assertEqual((charge["total"], charge["paid"], charge["balance"]), (25_000, 0, 25_000))
        self.assertEqual(charge["status_label"], "Owing")
        self.assertEqual(charge["number"], note.document_number)

        bola = TenantAPIClient(give_access(self.bola, "COR/002")).get(reverse("student-portal-fees")).data["data"]
        self.assertEqual(bola["debit_notes"], [])

    def test_unreadable_books_say_so_rather_than_zero(self):
        fal_registry.set_finance_reader(unavailable_finance_reader())
        response = self.as_pupil().get(reverse("student-portal-fees"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "FINANCE_UNAVAILABLE")

    def test_staff_cannot_read_the_portal_fees(self):
        response = TenantAPIClient(self.bursar).get(reverse("student-portal-fees"))
        self.assertEqual(response.status_code, 404)

    # ── paying ───────────────────────────────────────────────────────────

    def test_a_part_payment_hands_back_a_checkout_started_by_the_pupil(self):
        response = self.pay(200_000)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["checkout_url"])
        self.assertEqual(response.data["data"]["balance_after"], 100_000)
        intent = CollectionIntent.objects.get(reference=response.data["data"]["reference"])
        self.assertEqual(intent.origin, CollectionOrigin.PORTAL)
        self.assertEqual(intent.created_by_id, self.tunde_user.pk)
        self.assertEqual(intent.invoice_id, self.tunde_invoice.pk)

    @override_settings(
        SCHOOL_APP_BASE_URL="https://xvs.codexng.com", STUDENT_APP_BASE_URL="",
    )
    def test_the_checkout_returns_the_pupil_to_the_student_apps_paid_page(self):
        with mock.patch.object(
            self.fake, "create_checkout", wraps=self.fake.create_checkout,
        ) as checkout:
            self.assertEqual(self.pay(200_000).status_code, 201)
        self.assertEqual(
            checkout.call_args.kwargs["callback_url"],
            f"https://{self.corona.tenant.slug}.xvs.codexng.com/student/fees/paid",
        )

    def test_the_whole_balance_may_always_be_paid(self):
        self.assertEqual(self.pay(300_000).status_code, 201)

    def test_a_part_payment_below_the_schools_minimum_is_refused(self):
        response = self.pay(50_000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_BELOW_MINIMUM")
        self.assertFalse(CollectionIntent.objects.exists())

    def test_more_than_the_balance_is_refused(self):
        response = self.pay(400_000)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_ABOVE_BALANCE")

    def test_another_pupils_invoice_is_not_found(self):
        response = self.pay(100_000, invoice=self.bola_invoice)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_school_that_turns_pupil_payments_off_refuses_them(self):
        from django.core.management import call_command
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key="fees.student_payment_enabled")
        set_value(definition=definition, value=False, actor=self.bursar, tenant=self.corona.tenant)

        response = self.pay(300_000)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "STUDENT_PAYMENT_OFF")
        self.assertFalse(self.as_pupil().get(reverse("student-portal-fees")).data["data"]["can_pay"])

    # ── after paying ─────────────────────────────────────────────────────

    def test_a_payment_waiting_for_the_provider_is_pending_with_no_receipt(self):
        self.pay(200_000)
        rows = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertFalse(rows[0]["has_receipt"])

    def test_payment_history_can_be_read_one_page_at_a_time(self):
        self.pay(200_000)
        data = self.as_pupil().get(reverse("student-portal-payments"), {"page": 1}).data["data"]
        self.assertEqual(data["pagination"]["total"], 1)
        self.assertEqual(data["items"][0]["status"], "PENDING")
        self.assertEqual(self.as_pupil().get(reverse("student-portal-payments"), {"page": 2}).data["data"]["items"], [])

    def test_a_confirmed_payment_has_a_receipt_with_the_balance_it_left(self):
        reference = self.pay(200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        payment_services.confirm_collection(intent)

        rows = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        row = rows[0]
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertEqual(row["method"], "CARD")
        self.assertEqual(row["payer_name"], self.tunde_user.full_name)
        self.assertEqual((row["source"], row["source_label"]), ("ONLINE", "Paid online"))

        url = reverse("student-portal-receipt", kwargs={"payment_ref": row["payment_ref"]})
        response = self.as_pupil().get(url)
        self.assertEqual(response.status_code, 200, response.data)
        receipt = response.data["data"]
        self.assertEqual(receipt["amount"], 200_000)
        self.assertEqual(receipt["lines"][0]["balance_after"], 100_000)
        pdf = self.as_pupil().get(url, {"as": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_another_pupils_receipt_is_not_found(self):
        reference = self.pay(200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        payment_services.confirm_collection(intent)
        intent.refresh_from_db()

        bola_user = give_access(self.bola, "COR/002")
        url = reverse("student-portal-receipt", kwargs={"payment_ref": intent.payment_id})
        self.assertEqual(TenantAPIClient(bola_user).get(url).status_code, 404)


class PortalTransferTests(_FeesFixture):
    """Tunde pays by bank transfer into his own permanent account.

    He asks for his account once and gets the same number every time after.
    His mother transfers ₦2,500.00 into it: his page sees it arriving as soon
    as the provider reports it, and received once its receipt is booked. Bola
    sees nothing of Tunde's account or his transfers.
    """

    account_url = "student-portal-transfer-account"
    deposits_url = "student-portal-transfer-deposits"

    def transfer_account(self, method="get", user=None):
        client = self.as_pupil(user)
        return getattr(client, method)(reverse(self.account_url), format="json")

    def deposits(self, since, user=None):
        return self.as_pupil(user).get(reverse(self.deposits_url), {"since": since})

    def deposit(self, amount, reference="DEP-001"):
        from vs_payments.models import VirtualAccount

        account = VirtualAccount.objects.get(customer__source_id=str(self.tunde.pk))
        intent, _ = payment_services.record_virtual_account_deposit(
            virtual_account=account, reference=reference, amount=amount,
        )
        return intent

    def test_a_pupil_with_no_account_is_issued_one_and_keeps_it(self):
        self.assertEqual(self.transfer_account().data["data"]["state"], "NONE")
        first = self.transfer_account("post").data["data"]
        self.assertEqual(first["state"], "ISSUED")
        self.assertTrue(first["account_number"])
        again = self.transfer_account("post").data["data"]
        self.assertEqual(again["account_number"], first["account_number"])
        self.assertEqual(self.transfer_account().data["data"]["account_number"], first["account_number"])

    def test_a_deposit_is_seen_arriving_then_received(self):
        as_of = self.transfer_account("post").data["data"]["as_of"]
        intent = self.deposit(250_000)

        rows = self.deposits(as_of).data["data"]["deposits"]
        self.assertEqual([(r["amount"], r["status"]) for r in rows], [(250_000, "PENDING")])

        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self.fake.forced_amount[intent.reference] = 250_000
        payment_services.confirm_collection(intent)
        row = self.deposits(as_of).data["data"]["deposits"][0]
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertTrue(row["has_receipt"])
        # The school applies transfers to open bills, so the invoice moved.
        fees = self.as_pupil().get(reverse("student-portal-fees")).data["data"]
        self.assertEqual(fees["balance"], 50_000)

    def test_the_payment_history_lists_a_transfer_as_arriving_then_received(self):
        self.transfer_account("post")
        intent = self.deposit(300_000)
        history = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        row = next(r for r in history if r["reference"] == intent.reference)
        self.assertEqual((row["status"], row["method"]), ("PENDING", "BANK_TRANSFER"))
        self.assertEqual(row["source"], "TRANSFER_ACCOUNT")
        self.assertEqual(row["source_label"], "Transfer into your account")

        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self.fake.forced_amount[intent.reference] = 300_000
        payment_services.confirm_collection(intent)
        history = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        row = next(r for r in history if r["reference"] == intent.reference)
        self.assertEqual((row["status"], row["method"], row["method_label"]),
                         ("CONFIRMED", "BANK_TRANSFER", "Bank transfer"))
        self.assertEqual(row["payer_name"], "")
        self.assertEqual(row["source"], "TRANSFER_ACCOUNT")

    def test_a_deposit_before_the_page_opened_is_not_listed(self):
        self.transfer_account("post")
        self.deposit(100_000)
        later = self.transfer_account().data["data"]["as_of"]
        self.assertEqual(self.deposits(later).data["data"]["deposits"], [])

    def test_another_pupil_sees_nothing_of_tunde_s_account(self):
        as_of = self.transfer_account("post").data["data"]["as_of"]
        self.deposit(250_000)
        bola = give_access(self.bola, "COR/002")
        self.assertEqual(self.transfer_account(user=bola).data["data"]["state"], "NONE")
        self.assertEqual(self.deposits(as_of, user=bola).data["data"]["deposits"], [])

    def test_a_since_that_is_not_a_time_is_refused(self):
        response = self.deposits("yesterday")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "SINCE_INVALID")

    def test_a_provider_that_issues_no_accounts_says_so(self):
        from vs_payments.exceptions import ProviderNotConfiguredError

        with mock.patch.object(
            self.fake, "create_virtual_account", side_effect=ProviderNotConfiguredError("no"),
        ):
            data = self.transfer_account("post").data["data"]
        self.assertEqual(data["state"], "UNAVAILABLE")
        self.assertIn("bank transfers", data["message"])

    def test_a_school_that_turns_pupil_payments_off_shows_no_account(self):
        from django.core.management import call_command
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key="fees.student_payment_enabled")
        set_value(definition=definition, value=False, actor=self.bursar, tenant=self.corona.tenant)
        self.assertEqual(self.transfer_account().status_code, 403)

    def test_staff_cannot_read_a_pupils_transfer_account(self):
        response = TenantAPIClient(self.bursar).get(reverse(self.account_url))
        self.assertEqual(response.status_code, 404)
