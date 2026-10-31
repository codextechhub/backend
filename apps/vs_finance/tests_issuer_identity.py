"""Customer-facing finance wording names the same issuer as its document."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from vs_rbac.field_enforcement import SYSTEM_SURFACES, assert_system_surface

from .constants import FinanceDeliveryDocument
from .document_email import _subject
from .documents import issuer_name
from .payslips import _issuer_name as payslip_issuer_name
from .notifications import notify_credit_note_issued


class IssuerIdentityTests(SimpleTestCase):
    def test_issuer_name_is_a_declared_recipient_surface(self):
        self.assertIn("vs_finance.documents.issuer_name", SYSTEM_SURFACES)
        assert_system_surface("vs_finance.documents.issuer_name")

    def test_platform_email_and_payslip_use_the_configured_issuer(self):
        entity = SimpleNamespace(
            tenant=SimpleNamespace(school_profile=None),
            is_platform=True, name="Platform Books",
        )
        with patch("vs_config.platform_settings.get_platform_profile",
                   return_value={"name": "CodeX Finance"}):
            self.assertEqual(issuer_name(entity), "CodeX Finance")
            self.assertEqual(
                _subject(FinanceDeliveryDocument.INVOICE, entity, "INV-42"),
                "Invoice INV-42 from CodeX Finance",
            )
            self.assertEqual(payslip_issuer_name(entity), "CodeX Finance")

    def test_platform_credit_note_email_uses_the_configured_issuer(self):
        entity = SimpleNamespace(
            tenant=SimpleNamespace(school_profile=None),
            is_platform=True, name="Platform Books",
        )
        customer = SimpleNamespace(id=1, name="Bright Star School")
        note = SimpleNamespace(
            entity=entity, customer=customer, kind="CREDIT", total=2_000,
            invoice_id=None, document_number="CN-42", note_date=None,
            reason="Fee correction", branch=None, pk=42,
        )
        with (
            patch("vs_config.platform_settings.get_platform_profile",
                  return_value={"name": "CodeX Finance"}),
            patch("vs_finance.document_email.resolve_contacts", return_value=[{
                "email": "bursar@brightstar.test", "name": "Ada", "field": "", "label": "",
            }]),
            patch("vs_finance.views_ar._customer_ledger", return_value={1: {}}),
            patch("vs_finance.notifications.format_date", return_value="31 Oct 2026"),
            patch("vs_notifications.notify.send_notification", return_value=[]) as send,
        ):
            notify_credit_note_issued(note)
        self.assertEqual(send.call_args.kwargs["context"]["issuer_name"], "CodeX Finance")
