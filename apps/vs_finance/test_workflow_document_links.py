from types import SimpleNamespace

from django.test import SimpleTestCase

from .workflow_handlers import _console_document_link


class WorkflowDocumentLinkTests(SimpleTestCase):
    def test_console_link_uses_the_document_reference(self):
        document = SimpleNamespace(
            pk=17,
            document_number="CN 17/2026",
            entity=SimpleNamespace(code="TES"),
        )

        self.assertEqual(
            _console_document_link("/finance/receivables/credit-notes", document),
            "/finance/receivables/credit-notes?search=CN+17%2F2026&entity=TES",
        )

    def test_console_link_falls_back_to_the_object_id(self):
        document = SimpleNamespace(
            pk=17,
            document_number="",
            entity=SimpleNamespace(code="TES"),
        )

        self.assertEqual(
            _console_document_link("/finance/ledger", document),
            "/finance/ledger?search=17&entity=TES",
        )
