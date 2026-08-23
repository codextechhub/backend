"""Regression coverage for expense-claim approval hand-off and notices."""
import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.approvals import ensure_tenant_expense_claim_template
from vs_finance.constants import DocumentStatus, PeriodStatus
from vs_finance.models import ExpenseClaim, FiscalPeriod, FiscalYear, LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
from vs_tenants.models import Tenant
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStageApprover
from vs_workflow.services.actions import record_action


class ExpenseClaimWorkflowTests(TestCase):
    def setUp(self):
        tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        User = get_user_model()
        self.requester = User.objects.create_user(
            tenant=tenant,
            email="expense-requester@test.com",
            password="testpass123",
            status="ACTIVE",
            first_name="Rita",
            last_name="Requester",
        )
        admin_role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant,
            key="xvs_super_admin",
            defaults={"name": "Super Admin", "status": "ACTIVE"},
        )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant,
            user=self.requester,
            role=admin_role,
            assignment_status="ACTIVE",
        )
        self.approver = User.objects.create_user(
            tenant=tenant,
            email="expense-approver@test.com",
            password="testpass123",
            status="ACTIVE",
            first_name="Ada",
            last_name="Approver",
        )

        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            tenant=tenant,
            name="Expense Workflow Books",
            code="EXWF",
            kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(self.entity)
        year = FiscalYear.objects.create(
            entity=self.entity,
            year=2026,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=self.entity,
            fiscal_year=year,
            period_no=1,
            name="2026-01",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )

        template, _created = ensure_tenant_expense_claim_template(tenant)
        approval_role = TenantRoleTemplate.objects.get(
            tenant=tenant,
            key=template.stages.get().approver_role_key,
        )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant,
            user=self.approver,
            role=approval_role,
            assignment_status="ACTIVE",
        )
        self.client = TenantAPIClient(user=self.requester)

    def _create_claim(self):
        return self.client.post(
            f"/v1/finance/expense-claims/?entity={self.entity.code}",
            {
                "claimant_name": "Jane Staff",
                "claim_date": "2026-01-10",
                "title": "School visit",
                "lines": [{
                    "description": "Taxi",
                    "expense_account": "5300",
                    "quantity": 1,
                    "unit_price": 100_000,
                }],
            },
            format="json",
        )

    def test_submit_notifies_approver_and_final_approval_posts_claim(self):
        created = self._create_claim()
        self.assertEqual(created.status_code, 201, created.content)
        claim_id = created.json()["data"]["id"]
        self.assertTrue(created.json()["data"]["approval_required"])

        direct_post = self.client.post(
            f"/v1/finance/expense-claims/{claim_id}/post/?entity={self.entity.code}",
            {},
            format="json",
        )
        self.assertEqual(direct_post.status_code, 400, direct_post.content)

        with patch("vs_workflow.tasks.dispatch_notification.delay") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                submitted = self.client.post(
                    f"/v1/finance/expense-claims/{claim_id}/submit/?entity={self.entity.code}",
                    {},
                    format="json",
                )
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.assertEqual(submitted.json()["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        line_id = submitted.json()["data"]["lines"][0]["id"]

        pending = self.client.get(
            f"/v1/finance/expense-claims/?entity={self.entity.code}&display_status=PENDING",
        )
        self.assertEqual(pending.status_code, 200, pending.content)
        self.assertEqual([row["id"] for row in pending.json()["data"]], [claim_id])

        evidence_change = self.client.delete(
            f"/v1/finance/expense-claims/{claim_id}/lines/{line_id}/receipt/"
            f"?entity={self.entity.code}",
        )
        self.assertEqual(evidence_change.status_code, 400, evidence_change.content)

        instance = WorkflowInstance.objects.for_document(
            ExpenseClaim.objects.get(pk=claim_id),
        ).get()
        self.assertEqual(
            [str(user_id) for user_id in WorkflowStageApprover.objects.filter(
                stage_instance__instance=instance,
            ).values_list("user_id", flat=True)],
            [str(self.approver.id)],
        )
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(
            [str(user_id) for user_id in dispatch.call_args.kwargs["recipient_user_ids"]],
            [str(self.approver.id)],
        )

        record_action(instance.id, self.approver, WorkflowStageAction.APPROVED)
        claim = ExpenseClaim.objects.get(pk=claim_id)
        self.assertEqual(claim.status, DocumentStatus.POSTED)
        self.assertIsNotNone(claim.journal_id)
        self.assertEqual(claim.journal.created_by_id, self.approver.id)
