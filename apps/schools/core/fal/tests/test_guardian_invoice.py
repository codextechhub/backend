"""A pupil's bills and statements reach the pupil's primary guardian.

Corona bills Ikeja's pupils for the term. Tunde's mother, Ngozi, is his primary
guardian. The invoice email reaches her with its PDF, greeted by her own name
and told it is Tunde's invoice; Email invoice and Email statement reach her
too. When she has a parent app account she is told of the bill there as well,
with a push that opens Tunde's Fees. His father, a second guardian, is sent
nothing; he sees the bill in his own parent app. Receipts, debit and credit
notes and overdue reminders reach her as well, each telling her it is about
Tunde's account. A finance reader
whose role may not read a guardian's email sees "Tunde's primary guardian"
where her address would be.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage

from schools.core.fal.guardian_invoices import EVENT_GUARDIAN_INVOICE_ISSUED, GUARDIAN_EMAIL_FIELD
from schools.core.fal.tests.test_transfer_account_on_first_bill import _FirstBillFixture, give_guardian
from vs_finance.constants import FinanceDeliveryDocument
from vs_finance.document_email import resolve_contacts
from vs_finance.field_guards import shown_recipients
from vs_finance.models import FinanceDocumentDelivery
from vs_notifications.constants import EVENT_TYPE_REGISTRY, ChannelChoices
from vs_notifications.services.render import render_template
from vs_notifications.services.seed import _build_default_templates

_SEND = "vs_notifications.notify.send_notification"


class GuardianInvoiceTests(_FirstBillFixture):
    def setUp(self):
        super().setUp()
        self.ngozi = give_guardian(self.tunde, "ngozi@example.ng")
        mock.patch("vs_finance.pdf.invoice_pdf", return_value=b"%PDF-1.4 test").start()
        mock.patch.object(default_storage, "save", side_effect=lambda name, *a, **k: name).start()

    def billed(self, *pupils, dry_run=False):
        """Bill ``pupils``; return every send's keyword arguments, by event key."""
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.bill(*pupils, dry_run=dry_run)
        sent = {}
        for call in send.call_args_list:
            sent.setdefault(call.kwargs["event_key"], []).append(call.kwargs)
        return sent

    def give_parent_app(self, guardian):
        user = get_user_model().objects.create_user(
            email=guardian.email, password="x", tenant=self.tunde.tenant, status="ACTIVE",
            first_name="Ngozi", last_name="Okoro",
        )
        type(guardian).all_objects.filter(pk=guardian.pk).update(user=user)
        return user

    def test_the_invoice_email_reaches_the_primary_guardian_by_her_own_name(self):
        [email] = self.billed(self.tunde)["billing.invoice_issued"]

        self.assertEqual([r.email for r in email["unregistered_recipients"]], ["ngozi@example.ng"])
        self.assertEqual(email["context"]["recipient_name"], "Ngozi Okoro")
        self.assertFalse(email["context"]["recipient_is_customer"])
        self.assertEqual(email["metadata"]["attachments"][0]["content_type"], "application/pdf")

    def test_the_email_reads_as_her_childs_invoice(self):
        [email] = self.billed(self.tunde)["billing.invoice_issued"]
        template = _build_default_templates()[("billing.invoice_issued", ChannelChoices.EMAIL)]

        body = render_template(template["body"], {**email["context"], "email_brand": "XVS"})

        self.assertTrue(body.startswith("Hello Ngozi Okoro,"))
        self.assertIn(f"issued for {email['context']['customer_name']}.", body)
        self.assertNotIn("your account", body)

    def test_a_customer_reading_their_own_invoice_reads_it_as_before(self):
        template = _build_default_templates()[("billing.invoice_issued", ChannelChoices.EMAIL)]
        body = render_template(template["body"], {
            "customer_name": "Brightline Logistics", "recipient_name": "Brightline Logistics",
            "recipient_is_customer": True,
        })
        self.assertTrue(body.startswith("Hello Brightline Logistics,"))
        self.assertIn("issued to your account.", body)

    def test_the_delivery_records_who_she_is(self):
        self.billed(self.tunde)

        delivery = FinanceDocumentDelivery.objects.get(
            customer=self.tunde_customer, document_type=FinanceDeliveryDocument.INVOICE,
        )
        self.assertEqual(delivery.recipients, ["ngozi@example.ng"])
        self.assertEqual(delivery.recipient_contacts["ngozi@example.ng"]["field"], GUARDIAN_EMAIL_FIELD)

    def test_a_guardian_with_the_parent_app_is_told_there_too(self):
        user = self.give_parent_app(self.ngozi)

        [notice] = self.billed(self.tunde)[EVENT_GUARDIAN_INVOICE_ISSUED]

        self.assertEqual(notice["recipients"], [user])
        self.assertEqual(notice["metadata"]["student_id"], self.tunde.pk)

    def test_a_guardian_without_the_parent_app_has_only_the_email(self):
        sent = self.billed(self.tunde)
        self.assertNotIn(EVENT_GUARDIAN_INVOICE_ISSUED, sent)
        self.assertIn("billing.invoice_issued", sent)

    def test_a_second_guardian_is_sent_nothing(self):
        from schools.vs_students.models import Guardian, StudentGuardian

        father = Guardian.all_objects.create(
            tenant=self.tunde.tenant, full_name="Femi Okoro", phone="08039876543", email="femi@example.ng",
        )
        StudentGuardian.all_objects.create(
            tenant=self.tunde.tenant, student=self.tunde, guardian=father, relationship="FATHER",
            is_primary=False,
        )

        self.assertEqual(
            resolve_contacts(self.tunde_customer, FinanceDeliveryDocument.INVOICE),
            [{"email": "ngozi@example.ng", "name": "Ngozi Okoro", "field": GUARDIAN_EMAIL_FIELD,
              "label": "Tunde's primary guardian"}],
        )

    def test_statements_and_receipts_reach_her_too(self):
        for document_type in (FinanceDeliveryDocument.STATEMENT, FinanceDeliveryDocument.RECEIPT):
            with self.subTest(document_type=document_type):
                self.assertEqual(
                    [c["email"] for c in resolve_contacts(self.tunde_customer, document_type)],
                    ["ngozi@example.ng"],
                )

    def test_a_debit_note_tells_her_it_is_on_tunde_s_account(self):
        from vs_finance.constants import CreditNoteKind
        from vs_finance.models import Account, CreditNote, CreditNoteLine
        from vs_finance.notifications import notify_credit_note_issued
        from vs_finance.credit_notes import post_credit_note

        self.billed(self.tunde)
        note = CreditNote.objects.create(
            entity=self.tunde_customer.entity, customer=self.tunde_customer, kind=CreditNoteKind.DEBIT,
            note_date=self.term.start_date, reason="Late transport charge",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, quantity=1, unit_price=20_000,
            revenue_account=Account.objects.get(entity=self.tunde_customer.entity, code="4100"),
        )
        with mock.patch(_SEND, return_value=["sent"]):
            post_credit_note(note)
        with mock.patch(_SEND, return_value=["sent"]) as send:
            notify_credit_note_issued(note)

        [call] = send.call_args_list
        self.assertEqual([r.email for r in call.kwargs["unregistered_recipients"]], ["ngozi@example.ng"])
        context = call.kwargs["context"]
        self.assertEqual(context["recipient_name"], "Ngozi Okoro")
        self.assertIn(f"{self.tunde_customer.name}'s balance", context["summary"])
        self.assertNotIn("your", context["summary"])

    def test_an_overdue_reminder_reaches_her(self):
        from types import SimpleNamespace

        from vs_finance.dunning import _dispatch_notice

        invoice = self.manual_invoice(self.tunde_customer)
        notice = SimpleNamespace(
            entity=invoice.entity, customer=self.tunde_customer, invoice=invoice,
            amount_due=20_000, days_overdue=14, message="Second reminder.", level=2,
            document_number="DN-1", pk=1,
        )
        with mock.patch(_SEND, return_value=["sent"]) as send:
            _dispatch_notice(notice)

        self.assertEqual(
            [r.email for r in send.call_args.kwargs["unregistered_recipients"]], ["ngozi@example.ng"],
        )

    def test_a_preview_tells_nobody(self):
        self.assertEqual(self.billed(self.tunde, dry_run=True), {})

    def test_a_pupil_with_no_primary_guardian_has_nobody_to_send_to(self):
        self.assertEqual(resolve_contacts(self.tunde_customer.__class__.objects.get(
            source_id=str(self.bola.pk)), FinanceDeliveryDocument.INVOICE), [])

    def test_a_reader_who_may_not_read_her_email_sees_who_she_is(self):
        details = {"ngozi@example.ng": {"name": "Ngozi Okoro", "field": GUARDIAN_EMAIL_FIELD,
                                        "label": "Tunde's primary guardian"}}
        with mock.patch("vs_finance.field_guards.can_read", return_value=False):
            hidden = shown_recipients(object(), ["family@example.ng", "ngozi@example.ng"], details)
        with mock.patch("vs_finance.field_guards.can_read", return_value=True):
            open_ = shown_recipients(object(), ["family@example.ng", "ngozi@example.ng"], details)

        self.assertEqual(hidden, ["family@example.ng", "Tunde's primary guardian"])
        self.assertEqual(open_, ["family@example.ng", "ngozi@example.ng"])

    def test_the_app_notice_carries_no_email_and_its_push_no_figure(self):
        event = next(e for e in EVENT_TYPE_REGISTRY if e["key"] == EVENT_GUARDIAN_INVOICE_ISSUED)
        self.assertEqual(set(event["supported_channels"]), {ChannelChoices.IN_APP, ChannelChoices.PUSH})
        templates = {
            channel: template for (key, channel), template in _build_default_templates().items()
            if key == EVENT_GUARDIAN_INVOICE_ISSUED
        }
        self.assertEqual(set(templates), {ChannelChoices.IN_APP, ChannelChoices.PUSH})
        push = " ".join(templates[ChannelChoices.PUSH].values())
        self.assertNotIn("invoice_amount", push)
        self.assertNotIn("due_date", push)
