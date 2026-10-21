"""A pupil's primary guardian is sent each new bill.

Corona bills Ikeja's pupils for the term. Tunde's mother, Ngozi, is his primary
guardian: she is emailed the invoice as a PDF, and when she has a parent app
account she is told there too, with a push that opens Tunde's Fees. His father,
a second guardian, is sent nothing; he sees the bill in his own parent app. A
preview bills nobody and tells nobody, and a school that typed Ngozi's own
address as Tunde's billing email has already sent her the finance copy, so she
is not sent it twice.
"""
from __future__ import annotations

import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage

from schools.core.fal.guardian_invoices import EVENT_GUARDIAN_INVOICE_ISSUED
from schools.core.fal.tests.test_transfer_account_on_first_bill import _FirstBillFixture, give_guardian
from vs_notifications.constants import EVENT_TYPE_REGISTRY, ChannelChoices
from vs_notifications.services.render import render_template
from vs_notifications.services.seed import _build_default_templates

_SEND = "vs_notifications.notify.send_notification"
_VARIABLE = re.compile(r"\{\{\s*([a-z_0-9]+)|\{%\s*if\s+([a-z_0-9]+)")


class GuardianInvoiceTests(_FirstBillFixture):
    def setUp(self):
        super().setUp()
        self.ngozi = give_guardian(self.tunde, "ngozi@example.ng")
        mock.patch("vs_finance.pdf.invoice_pdf", return_value=b"%PDF-1.4 test").start()
        mock.patch.object(default_storage, "save", side_effect=lambda name, content: name).start()

    def billed(self, *pupils, dry_run=False):
        """Bill ``pupils`` and return the guardian sends, as their keyword arguments."""
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.bill(*pupils, dry_run=dry_run)
        return [c.kwargs for c in send.call_args_list if c.kwargs["event_key"] == EVENT_GUARDIAN_INVOICE_ISSUED]

    def give_parent_app(self, guardian):
        user = get_user_model().objects.create_user(
            email=guardian.email, password="x", tenant=self.tunde.tenant, status="ACTIVE",
            first_name="Ngozi", last_name="Okoro",
        )
        type(guardian).all_objects.filter(pk=guardian.pk).update(user=user)
        return user

    def test_the_primary_guardian_is_emailed_the_invoice_with_its_pdf(self):
        [sent] = self.billed(self.tunde)

        self.assertEqual(sent["recipients"], [])
        self.assertEqual([r.email for r in sent["unregistered_recipients"]], ["ngozi@example.ng"])
        [attachment] = sent["metadata"]["attachments"]
        self.assertEqual(attachment["content_type"], "application/pdf")
        self.assertTrue(attachment["name"].startswith("Invoice-"))
        self.assertEqual(sent["context"]["student_first_name"], "Tunde")
        self.assertEqual(sent["context"]["invoice_amount"], "3,000.00")
        self.assertFalse(sent["context"]["has_parent_app"])

    def test_a_guardian_with_the_parent_app_is_told_there_and_sent_to_the_childs_fees(self):
        user = self.give_parent_app(self.ngozi)

        [sent] = self.billed(self.tunde)

        self.assertEqual(sent["recipients"], [user])
        self.assertIsNone(sent["unregistered_recipients"])
        self.assertEqual(sent["metadata"]["student_id"], self.tunde.pk)
        self.assertTrue(sent["context"]["has_parent_app"])

    def test_a_second_guardian_is_sent_nothing(self):
        from schools.vs_students.models import Guardian, StudentGuardian

        father = Guardian.all_objects.create(
            tenant=self.tunde.tenant, full_name="Femi Okoro", phone="08039876543", email="femi@example.ng",
        )
        StudentGuardian.all_objects.create(
            tenant=self.tunde.tenant, student=self.tunde, guardian=father, relationship="FATHER",
            is_primary=False,
        )

        sent = self.billed(self.tunde)

        self.assertEqual(
            [r.email for s in sent for r in (s["unregistered_recipients"] or [])], ["ngozi@example.ng"],
        )

    def test_a_preview_tells_nobody(self):
        self.assertEqual(self.billed(self.tunde, dry_run=True), [])

    def test_a_pupil_with_no_primary_guardian_tells_nobody(self):
        self.assertEqual(self.billed(self.bola), [])

    def test_the_guardians_own_address_as_billing_email_is_not_sent_twice(self):
        type(self.tunde_customer).objects.filter(pk=self.tunde_customer.pk).update(
            billing_email="Ngozi@Example.ng",
        )
        self.assertEqual(self.billed(self.tunde), [])

    def test_the_notice_is_built_only_from_what_it_is_given(self):
        templates = {
            channel: template for (key, channel), template in _build_default_templates().items()
            if key == EVENT_GUARDIAN_INVOICE_ISSUED
        }
        self.assertEqual(set(templates), {ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH})
        event = next(e for e in EVENT_TYPE_REGISTRY if e["key"] == EVENT_GUARDIAN_INVOICE_ISSUED)
        self.assertEqual(set(event["supported_channels"]), set(templates))

        [sent] = self.billed(self.tunde)
        context = {**sent["context"], "email_brand": "XVS"}
        allowed = set(context)
        for channel, template in templates.items():
            for text in template.values():
                used = {a or b for a, b in _VARIABLE.findall(text)}
                self.assertLessEqual(used, allowed, channel)
                render_template(text, context)
        # A push shows on a locked screen, so it carries no figure.
        push = " ".join(templates[ChannelChoices.PUSH].values())
        self.assertNotIn("invoice_amount", push)
        self.assertNotIn("due_date", push)
