from types import SimpleNamespace

from django.test import SimpleTestCase

from .workflow_handlers import PayoutBatchApprovalHandler


class PayoutWorkflowDocumentLinkTests(SimpleTestCase):
    def test_summary_links_to_the_console_batch_drawer(self):
        batch = SimpleNamespace(
            pk=29,
            reference="BAT-0029",
            item_count=3,
            total_amount=250_000,
            provider="PAYSTACK",
        )

        summary = PayoutBatchApprovalHandler().get_document_summary(batch)

        self.assertEqual(
            summary["link"],
            "/finance/payments/batches?document=29",
        )
