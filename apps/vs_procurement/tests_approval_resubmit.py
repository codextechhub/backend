"""A procurement document returned and resumed from the approvals screen waits again.

Mrs Bello sends Ikeja's chairs requisition and a stationery bill for approval,
and Mr Eze returns each to her. A returned procurement document stays with its
approval request (``approval_state`` PENDING), and when she resumes the request
from her approvals screen it is still waiting for a decision: the requisition
reads PENDING_APPROVAL, neither can be edited, and neither can be sent a second
time through its own ``submit/``.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from vs_finance.constants import DocumentStatus
from vs_procurement.approvals import submit_for_approval
from vs_procurement.constants import (
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    ProcApprovalState,
)
from vs_procurement.models import PurchaseRequisition, VendorInvoice, VendorInvoiceLine
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action

from .tests_single_live_sourcing import _SingleSourcingFixture


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class ResumedFromApprovalsTests(_SingleSourcingFixture):

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import TenantRoleTemplate

        from .tests import _BranchTenantsFixture

        super().setUpTestData()
        tenant = cls.school.tenant
        for document_type in (WF_DOCTYPE_REQUISITION, WF_DOCTYPE_VENDOR_INVOICE):
            template, _ = WorkflowTemplate.objects.update_or_create(
                tenant=tenant, branch=None, document_type=document_type, code="standard",
                defaults={"name": "Second signature", "is_active": True})
            template.stages.all().delete()
            WorkflowStage.objects.create(
                template=template, code="signer", label="Second signature", order=10,
                approver_role_key="proc-signer")
        from django.contrib.auth import get_user_model

        cls.eze = get_user_model().objects.create_user(
            email="eze-signer@test.com", password="pw", tenant=tenant, status="ACTIVE",
            first_name="Eze", last_name="Signer")
        _BranchTenantsFixture.grant(cls.eze, "procurement.requisition.view", tenant=tenant,
                                    role_key="proc-signer")
        TenantRoleTemplate.objects.filter(tenant=tenant, key="proc-signer").update(
            is_system_role=True)

    def return_and_resume(self, document):
        instance = WorkflowInstance.all_objects.filter(
            document_object_id=str(document.pk), document_type=document.workflow_document_type,
        ).order_by("-created_at").first()
        record_action(instance.id, self.eze, WorkflowStageAction.RETURNED, comment="Recheck")
        resumed = self.bello.post(f"/v1/workflow/instances/{instance.id}/resubmit/", {},
                                  format="json")
        self.assertEqual(resumed.status_code, 200, resumed.data)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        document.refresh_from_db()
        self.assertEqual(document.approval_state, ProcApprovalState.PENDING)

    def test_a_resumed_requisition_waits_and_cannot_be_edited(self, _permission):
        req = self.stored(self.multi, self.ikeja, 12, approved=False)
        submit_for_approval(req, actor_user=self.bello.test_user)

        self.return_and_resume(req)

        self.assertEqual(req.status, DocumentStatus.PENDING_APPROVAL)
        url = f"/v1/procurement/requisitions/{req.pk}/?entity={self.multi.entity.code}"
        edited = self.bello.patch(url, {"title": "More chairs"}, format="json")
        self.assertIn(edited.status_code, (400, 409, 422), edited.data)
        again = self.bello.post(
            f"/v1/procurement/requisitions/{req.pk}/submit/?entity={self.multi.entity.code}",
            {}, format="json")
        self.assertNotEqual(again.status_code, 200, again.data)
        self.assertEqual(PurchaseRequisition.objects.get(pk=req.pk).title, "Chairs")

    def test_a_resumed_vendor_bill_waits_and_cannot_be_edited(self, _permission):
        bill = VendorInvoice.objects.create(
            entity=self.multi.entity, vendor=self.multi.vendor, branch=self.ikeja,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
            vendor_reference="INV-77",
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, description="Exercise books", line_no=1,
            expense_account=self.acc(self.multi.entity, "5300"), quantity=1,
            unit_price=50_000,
        )
        submit_for_approval(bill, actor_user=self.bello.test_user)

        self.return_and_resume(bill)

        self.assertEqual(bill.status, DocumentStatus.DRAFT)
        url = f"/v1/procurement/vendor-invoices/{bill.pk}/?entity={self.multi.entity.code}"
        edited = self.bello.patch(url, {"vendor_reference": "INV-78"}, format="json")
        self.assertEqual(edited.status_code, 400, edited.data)
        self.assertEqual(VendorInvoice.objects.get(pk=bill.pk).vendor_reference, "INV-77")
