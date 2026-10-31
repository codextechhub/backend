"""Purchase-order email subjects use the same issuer as the document."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .po_email import _subject


class PurchaseOrderIssuerTests(SimpleTestCase):
    def test_platform_subject_uses_the_configured_issuer(self):
        entity = SimpleNamespace(
            tenant=SimpleNamespace(school_profile=None),
            is_platform=True, name="Platform Books",
        )
        po = SimpleNamespace(entity=entity, document_number="PO-42")
        with patch("vs_config.platform_settings.get_platform_profile",
                   return_value={"name": "CodeX Finance"}):
            self.assertEqual(_subject(po), "Purchase order PO-42 from CodeX Finance")
