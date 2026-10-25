"""A family's invoice says how to pay, the way the school asked.

Corona bills Tunde (Ikeja) and Bola (Lekki); Greenfield bills Ada. At a school
that has not switched card payments on, Tunde's invoice and the email that
carries it name his own account number where "Pay online" used to be; while he
has no account number they say whom to ask. At a school that takes card, the
link and the QR code stay. A staff reader whose role may not read an account
number is shown the invoice without it.

While the school shows the pupil's own account number it does not also print
its own bank block, so the family transfers only to the account that is matched
automatically; with no number yet, or with card on, the school's bank stays.
"""
from __future__ import annotations

from unittest import mock

from django.core.files.storage import default_storage
from django.test import override_settings

from core.test_utils import TenantAPIClient
from schools.core.fal.family_payments import NO_ACCOUNT_MESSAGE
from schools.core.fal.tests.test_transfer_account_on_first_bill import (
    _FirstBillFixture,
    _pupils_accounts,
    give_guardian,
)
from schools.vs_student_portal.tests.test_fees import set_school_config
from vs_finance.document_email import _invoice_context
from vs_finance.documents import invoice_document_context, render_invoice_document_html
from vs_finance.models import Invoice
from vs_notifications.constants import ChannelChoices
from vs_notifications.services.render import render_template
from vs_notifications.services.seed import _build_default_templates
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

_SEND = "vs_notifications.notify.send_notification"
ACCOUNT_NUMBER = "payments.virtual_account.account_number"
ACCOUNT_NAME = "payments.virtual_account.account_name"


class _Delivery:
    note = ""


def school_bank(fixture, branch, number="0123456789"):
    """A collection account for the school's books at *branch*, so its documents print a pay-to block."""
    from vs_finance.models import Account, BankAccount

    entity_id = branch_books(fixture, branch).entity_ref
    gl = Account.objects.create(
        entity_id=entity_id, code=f"119{number[-1]}", name=f"Cash {number}", account_type="ASSET",
    )
    return BankAccount.objects.create(
        entity_id=entity_id, branch=branch, name="School collection", bank_name="GTBank",
        account_number=number, gl_account=gl, is_active=True, is_primary_collection=True,
    )


def branch_books(fixture, branch):
    """The books a branch of the fixture's schools keeps."""
    return fixture.greenfield_books if branch == fixture.greenfield_main else fixture.corona_books


@override_settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com", PLATFORM_PAY_BASE_URL="")
class FamilyInvoicePaymentBlockTests(_FirstBillFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("payments.virtual_account")

    def setUp(self):
        super().setUp()
        mock.patch("vs_finance.pdf.invoice_pdf", return_value=b"%PDF-1.4 test").start()
        mock.patch.object(default_storage, "save", side_effect=lambda name, *a, **k: name).start()

    def billed_invoice(self, pupil):
        self.bill(pupil, structure=self.g_tuition if pupil == self.ada else None)
        return Invoice.objects.filter(customer__source_id=str(pupil.pk)).latest("pk")

    def body(self, context):
        template = _build_default_templates()[("billing.invoice_issued", ChannelChoices.EMAIL)]
        return render_template(template["body"], {**context, "email_brand": "XVS"})

    # -- card off: the pupil's own number -------------------------------------- #

    def test_the_email_names_the_pupils_account_instead_of_the_link(self):
        invoice = self.billed_invoice(self.tunde)
        account = _pupils_accounts(self.tunde).get()

        context = _invoice_context(invoice, _Delivery())
        body = self.body(context)

        self.assertEqual(context["payment_link"], "")
        for part in (account.bank_name, account.account_number, account.account_name):
            self.assertIn(part, context["payment_instructions"])
        self.assertIn("matched to the invoice automatically", body)
        self.assertNotIn("Pay online", body)

    def test_the_invoice_prints_the_number_and_no_pay_block_or_qr_code(self):
        invoice = self.billed_invoice(self.bola)
        account = _pupils_accounts(self.bola).get()

        block = invoice_document_context(invoice)["invoice"]
        html = render_invoice_document_html(invoice)

        self.assertEqual((block["pay_url"], block["qr_image"]), ("", ""))
        self.assertEqual(block["payer_transfer"]["account_number"], account.account_number)
        self.assertIn(account.account_number, html)
        self.assertNotIn("Or pay online", html)

    def test_a_one_branch_school_prints_it_the_same_way(self):
        invoice = self.billed_invoice(self.ada)
        account = _pupils_accounts(self.ada).get()
        self.assertEqual(
            invoice_document_context(invoice)["invoice"]["payer_transfer"]["account_number"],
            account.account_number,
        )

    def test_the_real_send_carries_the_instruction_variable(self):
        give_guardian(self.tunde, "ngozi@example.ng")
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.bill(self.tunde)
        [email] = [c.kwargs for c in send.call_args_list if c.kwargs["event_key"] == "billing.invoice_issued"]
        self.assertIn("payment_instructions", email["context"])
        self.assertEqual(email["context"]["payment_link"], "")

    # -- the school's own bank ------------------------------------------------ #

    def test_the_schools_bank_is_left_off_once_the_pupil_has_an_account(self):
        school_bank(self, self.ikeja)
        invoice = self.billed_invoice(self.tunde)
        self.assertTrue(_pupils_accounts(self.tunde).exists())

        context = invoice_document_context(invoice)
        html = render_invoice_document_html(invoice)

        self.assertEqual(context["issuer"]["bank"]["account_number"], "")
        self.assertNotIn("0123456789", html)
        self.assertNotIn("Payment by bank transfer", html)
        self.assertIn(_pupils_accounts(self.tunde).get().account_number, html)

    def test_a_one_branch_school_leaves_its_bank_off_the_same_way(self):
        school_bank(self, self.greenfield_main, number="9876543210")
        invoice = self.billed_invoice(self.ada)
        context = invoice_document_context(invoice)
        self.assertEqual(context["issuer"]["bank"]["account_number"], "")
        self.assertNotIn("9876543210", render_invoice_document_html(invoice))

    def test_the_pdf_and_the_family_screen_leave_it_off_too(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter
        from vs_finance.pdf import _pay_to, _styles

        school_bank(self, self.ikeja)
        invoice = self.billed_invoice(self.tunde)

        self.assertIsNone(_pay_to(invoice_document_context(invoice)["issuer"], _styles()))
        document = DjangoStudentPaymentAdapter().invoice_document(str(self.tunde.pk), invoice.pk).value
        self.assertIsNone(document.bank)

    def test_the_schools_bank_stays_while_the_pupil_has_no_account_yet(self):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter
        from vs_finance.pdf import _pay_to, _styles

        school_bank(self, self.ikeja)
        self.issued.side_effect = NotImplementedError  # The provider issues no accounts.
        invoice = self.billed_invoice(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

        context = invoice_document_context(invoice)
        self.assertEqual(context["issuer"]["bank"]["account_number"], "0123456789")
        self.assertIn("0123456789", render_invoice_document_html(invoice))
        self.assertIsNotNone(_pay_to(context["issuer"], _styles()))
        document = DjangoStudentPaymentAdapter().invoice_document(str(self.tunde.pk), invoice.pk).value
        self.assertEqual(document.bank.account_number, "0123456789")

    def test_the_schools_bank_stays_where_the_school_takes_card(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.family_card_payments_enabled", True)
        school_bank(self, self.ikeja)
        invoice = self.billed_invoice(self.tunde)
        context = invoice_document_context(invoice)
        self.assertEqual(context["issuer"]["bank"]["account_number"], "0123456789")
        self.assertIn("0123456789", render_invoice_document_html(invoice))

    def test_the_schools_bank_stays_on_an_invoice_that_is_not_a_pupils(self):
        from vs_finance.models import Customer

        school_bank(self, self.ikeja)
        invoice = self.billed_invoice(self.tunde)
        hall = Customer.objects.create(
            entity_id=self.corona_books.entity_ref, code="HALL-2", name="Hall hire",
            receivable_account=self.tunde_customer.receivable_account, branch=self.ikeja,
        )
        Invoice.objects.filter(pk=invoice.pk).update(customer=hall)
        invoice.refresh_from_db()
        self.assertEqual(
            invoice_document_context(invoice)["issuer"]["bank"]["account_number"], "0123456789",
        )

    # -- no number yet -------------------------------------------------------- #

    def test_a_pupil_with_no_account_yet_is_told_to_contact_the_office(self):
        self.issued.side_effect = NotImplementedError  # The provider issues no accounts.
        invoice = self.billed_invoice(self.tunde)
        self.assertFalse(_pupils_accounts(self.tunde).exists())

        context = _invoice_context(invoice, _Delivery())
        block = invoice_document_context(invoice)["invoice"]

        self.assertEqual(context["payment_instructions"], NO_ACCOUNT_MESSAGE)
        self.assertEqual(context["payment_link"], "")
        self.assertIsNone(block["payer_transfer"])
        self.assertEqual(block["payer_note"], NO_ACCOUNT_MESSAGE)
        self.assertIn("school office", self.body(context))
        self.assertIn("school office", render_invoice_document_html(invoice))

    # -- card on: as before --------------------------------------------------- #

    def test_a_school_that_takes_card_keeps_the_link_and_the_qr_code(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.family_card_payments_enabled", True)
        invoice = self.billed_invoice(self.tunde)

        context = _invoice_context(invoice, _Delivery())
        block = invoice_document_context(invoice)["invoice"]

        self.assertTrue(context["payment_link"].startswith("https://corona-secondary.xvs.codexng.com/pay/"))
        self.assertEqual(context["payment_instructions"], f"Pay online: {context['payment_link']}")
        self.assertIn("Pay online: https://", self.body(context))
        self.assertTrue(block["pay_url"])
        self.assertTrue(block["qr_image"])
        self.assertIsNone(block["payer_transfer"])
        self.assertIn("Or pay online", render_invoice_document_html(invoice))

    def test_one_school_taking_card_does_not_change_another_school_s_invoice(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.family_card_payments_enabled", True)
        invoice = self.billed_invoice(self.ada)
        self.assertEqual(invoice_document_context(invoice)["invoice"]["pay_url"], "")

    def test_a_customer_who_is_not_a_pupil_keeps_the_link(self):
        from vs_finance.models import Customer

        invoice = self.billed_invoice(self.tunde)
        hall = Customer.objects.create(
            entity_id=self.corona_books.entity_ref, code="HALL-1", name="Hall hire",
            receivable_account=self.tunde_customer.receivable_account, branch=self.ikeja,
        )
        Invoice.objects.filter(pk=invoice.pk).update(customer=hall)
        invoice.refresh_from_db()
        self.assertTrue(invoice_document_context(invoice)["invoice"]["pay_url"])
        self.assertTrue(_invoice_context(invoice, _Delivery())["payment_link"])


class StaffReaderTests(_FirstBillFixture):
    """The staff screen shows an account number only to a reader whose role may read it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields("payments.virtual_account")
        cls.reader = cls.staff_reader("reader@corona.test", hide=())
        cls.no_number = cls.staff_reader("nonumber@corona.test", hide=(ACCOUNT_NUMBER,))
        cls.no_name = cls.staff_reader("noname@corona.test", hide=(ACCOUNT_NAME,))

    @classmethod
    def staff_reader(cls, email, hide):
        user = cls.user_for(cls.corona, email)
        role = cls.grant(user, "finance.invoice.view")
        set_field_access(role, *cls.keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        return user

    def setUp(self):
        super().setUp()
        self.bill(self.tunde)
        self.invoice = Invoice.objects.get(customer__source_id=str(self.tunde.pk))
        self.account = _pupils_accounts(self.tunde).get()

    def document(self, user):
        response = TenantAPIClient(user).get(
            f"/v1/finance/invoices/{self.invoice.pk}/document/?entity={self.corona_books.code}",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.content.decode()

    def test_a_reader_who_may_read_it_sees_the_number_and_the_name(self):
        html = self.document(self.reader)
        self.assertIn(self.account.account_number, html)
        self.assertIn('Account name</span><span class="pb-v">', html)

    def test_a_reader_who_may_not_read_the_number_sees_no_account_at_all(self):
        html = self.document(self.no_number)
        self.assertNotIn(self.account.account_number, html)
        self.assertNotIn("Pay by bank transfer", html)

    def test_the_schools_bank_is_left_off_the_staff_view_too(self):
        school_bank(self, self.ikeja)
        for user in (self.reader, self.no_number):
            self.assertNotIn("0123456789", self.document(user))

    def test_a_reader_who_may_not_read_the_name_sees_the_number_without_it(self):
        html = self.document(self.no_name)
        self.assertIn(self.account.account_number, html)
        self.assertIn("Account number", html)
        self.assertNotIn('Account name</span><span class="pb-v">', html)

    def test_the_email_and_the_pdf_are_built_for_nobody_and_carry_it(self):
        context = invoice_document_context(self.invoice)["invoice"]
        self.assertEqual(context["payer_transfer"]["account_number"], self.account.account_number)
