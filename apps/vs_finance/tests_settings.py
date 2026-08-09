from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.account_mappings import resolve_mapped_account
from vs_finance.constants import AccountMappingKey, FinanceAuditAction
from vs_finance.models import (
    Account,
    BankAccount,
    Customer,
    FinanceAuditLog,
    FinanceBankingSettings,
    FinanceDocumentSettings,
    Invoice,
    LedgerEntity,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_schools.models import School


class FinanceAccountSettingsAPITests(TestCase):
    def setUp(self):
        seed_currencies()
        self.school = School.objects.create(
            name="Settings School", slug="settings-school", code="SETSC", status="ACTIVE",
        )
        self.entity = LedgerEntity.objects.create(
            name="Settings Books", code="SETBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.school.tenant,
        )
        seed_chart_of_accounts(self.entity)
        self.user = get_user_model().objects.create_user(
            email="finance-settings@test.com", password="pw", tenant=self.school.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Finance", last_name="Settings",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/finance/settings/account-mappings/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_read_and_update_require_settings_permission(self, _permission):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(self.url, {"mappings": {"CASH_BANK": "1300"}}, format="json").status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_defaults_are_resolved_and_options_are_bounded_to_entity(self, _permission):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        cash = next(row for row in response.data["data"]["mappings"] if row["key"] == "CASH_BANK")
        self.assertEqual(cash["account"]["code"], "1100")
        self.assertEqual(cash["source"], "DEFAULT")
        self.assertTrue(all(row["account_type"] for row in response.data["data"]["account_options"]))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_update_changes_runtime_resolution_and_writes_audit(self, _permission):
        replacement = Account.objects.get(entity=self.entity, code="1300")
        response = self.client.patch(
            self.url, {"mappings": {"CASH_BANK": replacement.id}}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(resolve_mapped_account(self.entity, AccountMappingKey.CASH_BANK), replacement)
        audit = FinanceAuditLog.objects.get(action=FinanceAuditAction.FINANCE_SETTINGS_UPDATED)
        self.assertEqual(audit.before["CASH_BANK"], "1100")
        self.assertEqual(audit.after["CASH_BANK"], "1300")
        self.assertEqual(response.data["data"]["history"][0]["id"], audit.id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_wrong_account_type_and_cross_tenant_account_are_rejected(self, _permission):
        liability = Account.objects.get(entity=self.entity, code="2100")
        response = self.client.patch(self.url, {"mappings": {"CASH_BANK": liability.id}}, format="json")
        self.assertEqual(response.status_code, 400)

        other_school = School.objects.create(
            name="Other School", slug="other-settings-school", code="OTHSC", status="ACTIVE",
        )
        other_entity = LedgerEntity.objects.create(
            name="Other Books", code="OTHBK", kind=LedgerEntity.Kind.TENANT,
            tenant=other_school.tenant,
        )
        seed_chart_of_accounts(other_entity)
        other_asset = Account.objects.get(entity=other_entity, code="1300")
        response = self.client.patch(self.url, {"mappings": {"CASH_BANK": other_asset.id}}, format="json")
        self.assertEqual(response.status_code, 400)

        response = self.client.get(f"/v1/finance/settings/account-mappings/?entity={other_entity.code}")
        self.assertEqual(response.status_code, 404)


class FinanceDocumentSettingsAPITests(TestCase):
    def setUp(self):
        seed_currencies()
        self.school = School.objects.create(
            name="Document Settings School", slug="document-settings-school",
            code="DOCSC", status="ACTIVE",
        )
        self.entity = LedgerEntity.objects.create(
            name="Document Settings Books", code="DOCBK",
            kind=LedgerEntity.Kind.TENANT, tenant=self.school.tenant,
        )
        seed_chart_of_accounts(self.entity)
        self.user = get_user_model().objects.create_user(
            email="document-settings@test.com", password="pw",
            tenant=self.school.tenant, user_type="CX_STAFF", status="ACTIVE",
            first_name="Document", last_name="Settings",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/finance/settings/documents/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_read_and_update_require_settings_permission(self, _permission):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(
            self.url, {"default_invoice_due_days": 14}, format="json",
        ).status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_get_returns_defaults_without_creating_a_row(self, _permission):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["settings"]["default_invoice_due_days"], 30)
        self.assertTrue(response.data["data"]["settings"]["auto_post_manual_invoices"])
        self.assertFalse(FinanceDocumentSettings.objects.filter(entity=self.entity).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_update_is_typed_entity_scoped_and_audited(self, _permission):
        bank = BankAccount.objects.create(
            entity=self.entity,
            gl_account=Account.objects.get(entity=self.entity, code="1100"),
            name="Collections", bank_name="Test Bank",
        )
        response = self.client.patch(self.url, {
            "default_invoice_due_days": 14,
            "default_invoice_narration": "School fees",
            "auto_post_manual_invoices": False,
            "allow_customer_opening_balances": False,
            "primary_collection_bank_account": bank.id,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        settings = FinanceDocumentSettings.objects.get(entity=self.entity)
        self.assertEqual(settings.default_invoice_due_days, 14)
        self.assertFalse(settings.auto_post_manual_invoices)
        bank.refresh_from_db()
        self.assertTrue(bank.is_primary_collection)
        audit = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
        )
        self.assertEqual(audit.before["default_invoice_due_days"], 30)
        self.assertEqual(audit.after["primary_collection_bank_account"]["name"], "Collections")
        self.assertEqual(response.data["data"]["history"][0]["id"], audit.id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_invalid_days_and_another_entity_bank_are_rejected(self, _permission):
        self.assertEqual(self.client.patch(
            self.url, {"default_invoice_due_days": 366}, format="json",
        ).status_code, 400)
        other_entity = LedgerEntity.objects.create(
            name="Sibling Books", code="SIBBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.school.tenant,
        )
        seed_chart_of_accounts(other_entity)
        other_bank = BankAccount.objects.create(
            entity=other_entity,
            gl_account=Account.objects.get(entity=other_entity, code="1100"),
            name="Sibling Collections",
        )
        self.assertEqual(self.client.patch(
            self.url, {"primary_collection_bank_account": other_bank.id}, format="json",
        ).status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_saved_document_policy_drives_invoice_and_customer_writes(self, _permission):
        self.client.patch(self.url, {
            "default_invoice_due_days": 14,
            "default_invoice_narration": "Default billing note",
            "auto_post_manual_invoices": False,
            "allow_customer_opening_balances": False,
        }, format="json")
        customer = Customer.objects.create(
            entity=self.entity, code="CUST", name="Customer",
            billing_email="customer@example.com", billing_phone="08000000000",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
        )
        invoice_response = self.client.post(
            f"/v1/finance/invoices/?entity={self.entity.code}",
            {
                "customer": customer.id,
                "invoice_date": "2026-08-01",
                "lines": [{
                    "description": "Tuition", "revenue_account": "4100",
                    "quantity": 1, "unit_price": 100000,
                }],
            },
            format="json",
        )
        self.assertEqual(invoice_response.status_code, 201)
        invoice = Invoice.objects.get(pk=invoice_response.data["data"]["id"])
        self.assertEqual(str(invoice.due_date), "2026-08-15")
        self.assertEqual(invoice.narration, "Default billing note")
        self.assertEqual(invoice.status, "DRAFT")

        customer_response = self.client.post(
            f"/v1/finance/customers/?entity={self.entity.code}",
            {
                "name": "Imported Customer", "billing_email": "import@example.com",
                "billing_phone": "08000000001", "opening_balance": 5000,
            },
            format="json",
        )
        self.assertEqual(customer_response.status_code, 400)
        self.assertFalse(Customer.objects.filter(name="Imported Customer").exists())


class FinanceBankingSettingsAPITests(TestCase):
    def setUp(self):
        seed_currencies()
        self.school = School.objects.create(
            name="Banking Settings School", slug="banking-settings-school",
            code="BNKSC", status="ACTIVE",
        )
        self.entity = LedgerEntity.objects.create(
            name="Banking Settings Books", code="BNKBK",
            kind=LedgerEntity.Kind.TENANT, tenant=self.school.tenant,
        )
        seed_chart_of_accounts(self.entity)
        self.user = get_user_model().objects.create_user(
            email="banking-settings@test.com", password="pw",
            tenant=self.school.tenant, user_type="CX_STAFF", status="ACTIVE",
            first_name="Banking", last_name="Settings",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/finance/settings/banking/?entity={self.entity.code}"
        self.bank = BankAccount.objects.create(
            entity=self.entity,
            gl_account=Account.objects.get(entity=self.entity, code="1100"),
            name="Operating Bank",
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_read_and_update_require_settings_permission(self, _permission):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(
            self.url, {"default_group_reconciliation_matches": False}, format="json",
        ).status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_defaults_are_returned_without_creating_a_row(self, _permission):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        values = response.data["data"]["settings"]
        self.assertEqual(values["default_bank_reconciliation_tolerance_days"], 4)
        self.assertTrue(values["default_group_reconciliation_matches"])
        self.assertEqual(values["default_receipt_allocation_strategy"], "oldest")
        self.assertFalse(FinanceBankingSettings.objects.filter(entity=self.entity).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_update_is_typed_and_audited(self, _permission):
        response = self.client.patch(self.url, {
            "default_bank_reconciliation_tolerance_days": 0,
            "default_group_reconciliation_matches": False,
            "default_receipt_allocation_strategy": "largest",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        settings = FinanceBankingSettings.objects.get(entity=self.entity)
        self.assertEqual(settings.default_bank_reconciliation_tolerance_days, 0)
        self.assertFalse(settings.default_group_reconciliation_matches)
        self.assertEqual(settings.default_receipt_allocation_strategy, "largest")
        audit = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
        )
        self.assertEqual(
            audit.before["default_bank_reconciliation_tolerance_days"], 4,
        )
        self.assertEqual(
            audit.after["default_receipt_allocation_strategy"], "largest",
        )
        self.assertEqual(response.data["data"]["history"][0]["id"], audit.id)

        audit_count = FinanceAuditLog.objects.count()
        same = self.client.patch(self.url, {
            "default_receipt_allocation_strategy": "largest",
        }, format="json")
        self.assertEqual(same.status_code, 200)
        self.assertEqual(FinanceAuditLog.objects.count(), audit_count)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_invalid_ranges_and_strategy_are_rejected(self, _permission):
        self.assertEqual(self.client.patch(self.url, {
            "default_bank_reconciliation_tolerance_days": 31,
        }, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {
            "default_group_reconciliation_matches": "false",
        }, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {
            "default_receipt_allocation_strategy": "fifo",
        }, format="json").status_code, 400)

    @patch("vs_finance.banking.auto_reconcile", return_value=[])
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_saved_reconciliation_defaults_drive_auto_match(
        self, _permission, auto_reconcile,
    ):
        FinanceBankingSettings.objects.create(
            entity=self.entity,
            default_bank_reconciliation_tolerance_days=0,
            default_group_reconciliation_matches=False,
        )
        response = self.client.post(
            f"/v1/finance/bank-accounts/{self.bank.id}/auto-reconcile/"
            f"?entity={self.entity.code}",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        auto_reconcile.assert_called_once_with(
            self.bank, tolerance_days=0, group=False, actor_user=self.user,
        )

    @patch("vs_finance.receivables.post_payment")
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_saved_allocation_strategy_drives_customer_receipts(
        self, _permission, post_payment,
    ):
        FinanceBankingSettings.objects.create(
            entity=self.entity, default_receipt_allocation_strategy="largest",
        )
        customer = Customer.objects.create(
            entity=self.entity, code="BANK-CUST", name="Banking Customer",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
        )
        response = self.client.post(
            f"/v1/finance/customers/{customer.code}/receipt/?entity={self.entity.code}",
            {
                "amount": 10000,
                "payment_date": "2026-08-01",
                "deposit_account": "1100",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(post_payment.call_args.kwargs["strategy"], "largest")
