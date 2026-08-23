from types import SimpleNamespace

from django.test import SimpleTestCase

from .workflow_handlers import (
    PurchaseOrderApprovalHandler,
    RequisitionApprovalHandler,
    VendorInvoiceApprovalHandler,
    VendorPaymentApprovalHandler,
)


class ProcurementWorkflowDocumentLinkTests(SimpleTestCase):
    def test_every_procurement_approval_links_to_its_source_drawer(self):
        document = SimpleNamespace(
            pk=42,
            document_number="PROC-0042",
            workflow_amount_field="total",
            total=250_000,
            vendor=None,
            entity=SimpleNamespace(code="TES"),
        )

        cases = (
            (RequisitionApprovalHandler, "/procurement/requisitions?document=42&entity=TES"),
            (PurchaseOrderApprovalHandler, "/procurement/purchase-orders?document=42&entity=TES"),
            (VendorInvoiceApprovalHandler, "/procurement/vendor-invoices?document=42&entity=TES"),
            (VendorPaymentApprovalHandler, "/procurement/vendor-payments?document=42&entity=TES"),
        )

        for handler_class, expected in cases:
            with self.subTest(handler=handler_class.__name__):
                summary = handler_class().get_document_summary(document)
                self.assertEqual(summary["link"], expected)

    def test_detail_serialization_repairs_an_existing_snapshot_without_a_link(self):
        from vs_workflow.serializers import WorkflowInstanceDetailSerializer

        document = SimpleNamespace(
            pk=42,
            document_number="PR-0042",
            workflow_amount_field="total",
            total=250_000,
            vendor=None,
            entity=SimpleNamespace(code="TES"),
        )
        instance = SimpleNamespace(
            document_summary={"title": "PR-0042", "fields": []},
            document_type="procurement.requisition",
            document=document,
        )

        summary = WorkflowInstanceDetailSerializer().get_document_summary(instance)

        self.assertEqual(
            summary["link"],
            "/procurement/requisitions?document=42&entity=TES",
        )
