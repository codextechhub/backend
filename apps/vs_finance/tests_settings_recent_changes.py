"""The settings history endpoints combine sections without using the full audit trail."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from schools.vs_schools.models import School
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import FinanceAuditLog, LedgerEntity
from vs_finance.seed import seed_currencies


class SettingsRecentChangesAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.school = School.objects.create(
            name="Bright Star School", slug="bright-star-history", code="BSHIS",
            status="ACTIVE",
        )
        cls.entity = LedgerEntity.objects.create(
            name="Bright Star Books", code="BSHBK",
            kind=LedgerEntity.Kind.TENANT, tenant=cls.school.tenant,
        )
        cls.user = get_user_model().objects.create_user(
            email="ada@brightstar.test", password="pw", tenant=cls.school.tenant,
            status="ACTIVE", first_name="Ada", last_name="Obi",
        )

    def setUp(self):
        self.client = TenantAPIClient(user=self.user)

    def change(self, action, before, after, target_type):
        return FinanceAuditLog.objects.create(
            entity=self.entity,
            actor=self.user,
            action=action,
            target_type=target_type,
            target_id=str(self.entity.pk),
            message="Settings saved.",
            before=before,
            after=after,
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_finance_history_combines_sections_newest_first(self, _permission):
        older = self.change(
            FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
            {"default_invoice_due_days": 30},
            {"default_invoice_due_days": 14},
            "FinanceDocumentSettings",
        )
        newer = self.change(
            FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
            {"default_bank_reconciliation_tolerance_days": 3},
            {"default_bank_reconciliation_tolerance_days": 5},
            "FinanceBankingSettings",
        )

        response = self.client.get(
            f"/v1/finance/settings/history/?entity={self.entity.code}",
        )

        self.assertEqual(response.status_code, 200)
        rows = response.data["data"]["history"]
        self.assertEqual([row["id"] for row in rows], [newer.id, older.id])
        self.assertEqual(rows[0]["changes"][0]["label"], "Reconciliation date window")
        self.assertEqual(rows[1]["changes"][0]["label"], "Days until an invoice is due")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_procurement_history_uses_its_settings_labels(self, _permission):
        row = self.change(
            FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED,
            {"default_requisition_lead_days": 3},
            {"default_requisition_lead_days": 5},
            "ProcurementSettings",
        )

        response = self.client.get(
            f"/v1/procurement/settings/history/?entity={self.entity.code}",
        )

        self.assertEqual(response.status_code, 200)
        history = response.data["data"]["history"]
        self.assertEqual([entry["id"] for entry in history], [row.id])
        self.assertEqual(history[0]["changes"][0]["label"], "Default requisition lead time")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_each_history_endpoint_requires_its_settings_view_permission(self, _permission):
        finance = self.client.get(
            f"/v1/finance/settings/history/?entity={self.entity.code}",
        )
        procurement = self.client.get(
            f"/v1/procurement/settings/history/?entity={self.entity.code}",
        )

        self.assertEqual(finance.status_code, 403)
        self.assertEqual(procurement.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_history_cannot_cross_into_another_tenant(self, _permission):
        other_school = School.objects.create(
            name="Greenfield School", slug="greenfield-history", code="GFHIS",
            status="ACTIVE",
        )
        other_entity = LedgerEntity.objects.create(
            name="Greenfield Books", code="GFHBK",
            kind=LedgerEntity.Kind.TENANT, tenant=other_school.tenant,
        )

        finance = self.client.get(
            f"/v1/finance/settings/history/?entity={other_entity.code}",
        )
        procurement = self.client.get(
            f"/v1/procurement/settings/history/?entity={other_entity.code}",
        )

        self.assertEqual(finance.status_code, 404)
        self.assertEqual(procurement.status_code, 404)
