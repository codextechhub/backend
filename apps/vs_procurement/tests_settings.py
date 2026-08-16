import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import Account, FinanceAuditLog, LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.constants import MatchStatus, VendorKycStatus
from vs_procurement.models import (
    ProcurementSettings,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    RequestForQuotation,
    Vendor,
    VendorContract,
    VendorInvoice,
    VendorInvoiceLine,
)
from vs_procurement.payables import match_vendor_invoice
from vs_procurement.purchasing import vendor_purchase_block_reason
from vs_procurement.settings import SETTING_FIELDS
from vs_procurement.settings_ownership import PROCUREMENT_SETTING_CONSUMERS
from schools.vs_schools.models import School


class ProcurementSettingsAPITests(TestCase):
    def setUp(self):
        seed_currencies()
        self.school = School.objects.create(
            name="Proc Settings School", slug="proc-settings-school", code="PRSSC", status="ACTIVE",
        )
        self.entity = LedgerEntity.objects.create(
            name="Proc Settings Books", code="PRSBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.school.tenant,
        )
        seed_chart_of_accounts(self.entity)
        self.user = get_user_model().objects.create_user(
            email="proc-settings@test.com", password="pw", tenant=self.school.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Proc", last_name="Settings",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/procurement/settings/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_read_and_update_require_settings_permission(self, _permission):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(self.url, {"default_payment_terms": "NET_60"}, format="json").status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_get_returns_defaults_without_creating_a_row(self, _permission):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["settings"]["default_payment_terms"], "NET_30")
        self.assertEqual(response.data["data"]["settings"]["default_rfq_response_days"], 14)
        self.assertEqual(response.data["data"]["settings"]["rfq_closing_soon_days"], 7)
        self.assertEqual(response.data["data"]["settings"]["minimum_rfq_invited_vendors"], 1)
        self.assertEqual(
            response.data["data"]["settings"]["minimum_submitted_quotations_before_award"],
            1,
        )
        self.assertEqual(set(response.data["data"]["consumers"]), set(SETTING_FIELDS))
        self.assertEqual(set(PROCUREMENT_SETTING_CONSUMERS), set(SETTING_FIELDS))
        self.assertFalse(ProcurementSettings.objects.filter(entity=self.entity).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_update_is_typed_entity_scoped_and_audited(self, _permission):
        response = self.client.patch(self.url, {
            "default_payment_terms": "NET_60",
            "default_delivery_address": "Central receiving dock",
            "quantity_tolerance_bps": 500,
            "price_tolerance_bps": 250,
            "allow_non_po_invoices": False,
            "vendor_purchase_kyc_requirement": "VERIFIED_ONLY",
            "require_purchase_order_for_receipts": True,
            "default_requisition_lead_days": 7,
            "contract_renewal_notice_days": 45,
            "default_rfq_response_days": 21,
            "rfq_closing_soon_days": 5,
            "minimum_rfq_invited_vendors": 3,
            "minimum_submitted_quotations_before_award": 2,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        settings = ProcurementSettings.objects.get(entity=self.entity)
        self.assertEqual(settings.quantity_tolerance_bps, 500)
        self.assertFalse(settings.allow_non_po_invoices)
        self.assertEqual(settings.vendor_purchase_kyc_requirement, "VERIFIED_ONLY")
        self.assertTrue(settings.require_purchase_order_for_receipts)
        self.assertEqual(settings.default_requisition_lead_days, 7)
        self.assertEqual(settings.contract_renewal_notice_days, 45)
        self.assertEqual(settings.default_rfq_response_days, 21)
        self.assertEqual(settings.rfq_closing_soon_days, 5)
        self.assertEqual(settings.minimum_rfq_invited_vendors, 3)
        self.assertEqual(settings.minimum_submitted_quotations_before_award, 2)
        audit = FinanceAuditLog.objects.get(action=FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED)
        self.assertEqual(audit.before["default_payment_terms"], "NET_30")
        self.assertEqual(audit.after["default_payment_terms"], "NET_60")
        self.assertEqual(response.data["data"]["history"][0]["id"], audit.id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_invalid_tolerance_and_other_tenant_are_rejected(self, _permission):
        self.assertEqual(self.client.patch(self.url, {"quantity_tolerance_bps": 10001}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"default_requisition_lead_days": 366}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"default_rfq_response_days": 366}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"minimum_rfq_invited_vendors": 0}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"minimum_submitted_quotations_before_award": 51}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"vendor_purchase_kyc_requirement": "ANY"}, format="json").status_code, 400)
        other = School.objects.create(name="Other Proc", slug="other-proc", code="OPRSC", status="ACTIVE")
        other_entity = LedgerEntity.objects.create(name="Other Proc Books", code="OPRBK", kind=LedgerEntity.Kind.TENANT, tenant=other.tenant)
        response = self.client.get(f"/v1/procurement/settings/?entity={other_entity.code}")
        self.assertEqual(response.status_code, 404)

    def test_matching_tolerances_and_non_po_rule_drive_match_status(self):
        settings = ProcurementSettings.objects.create(
            entity=self.entity, quantity_tolerance_bps=500, price_tolerance_bps=500,
            allow_non_po_invoices=False,
        )
        expense = Account.objects.get(entity=self.entity, code="5300")
        vendor = Vendor.objects.create(
            entity=self.entity, code="SUP", name="Supplier",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
            default_expense_account=expense,
        )
        po = PurchaseOrder.objects.create(
            entity=self.entity, vendor=vendor, order_date=datetime.date(2026, 1, 1),
            status=DocumentStatus.APPROVED,
        )
        po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, description="Paper", expense_account=expense,
            quantity=Decimal("10"), received_qty=Decimal("10"), unit_price=10000, line_no=1,
        )
        invoice = VendorInvoice.objects.create(
            entity=self.entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 2), due_date=datetime.date(2026, 1, 2),
        )
        line = VendorInvoiceLine.objects.create(
            vendor_invoice=invoice, po_line=po_line, expense_account=expense,
            quantity=Decimal("10.5"), unit_price=10500, line_no=1,
        )
        self.assertEqual(match_vendor_invoice(invoice), MatchStatus.AUTO_MATCHED)

        line.quantity = Decimal("10.5001")
        line.save(update_fields=["quantity", "updated_at"])
        self.assertEqual(match_vendor_invoice(invoice), MatchStatus.OVER_BILLED)

        non_po = VendorInvoice.objects.create(
            entity=self.entity, vendor=vendor,
            invoice_date=datetime.date(2026, 1, 3), due_date=datetime.date(2026, 1, 3),
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=non_po, expense_account=expense, quantity=1, unit_price=10000, line_no=1,
        )
        self.assertEqual(match_vendor_invoice(non_po), MatchStatus.NON_PO_BLOCKED)

        settings.allow_non_po_invoices = True
        settings.save(update_fields=["allow_non_po_invoices", "updated_at"])
        self.assertEqual(match_vendor_invoice(non_po), MatchStatus.AUTO_MATCHED)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_saved_purchasing_policy_drives_new_records(self, _permission):
        settings_response = self.client.patch(self.url, {
            "default_payment_terms": "NET_60",
            "vendor_purchase_kyc_requirement": "VERIFIED_ONLY",
            "require_purchase_order_for_receipts": True,
            "default_requisition_lead_days": 7,
            "contract_renewal_notice_days": 45,
        }, format="json")
        self.assertEqual(settings_response.status_code, 200)

        pending_vendor = Vendor.objects.create(
            entity=self.entity, code="PENDING", name="Pending Vendor",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
            default_expense_account=Account.objects.get(entity=self.entity, code="5300"),
            kyc_status=VendorKycStatus.PENDING,
        )
        self.assertIn("verified KYC", vendor_purchase_block_reason(pending_vendor))
        rejected_contract = self.client.post(
            f"/v1/procurement/contracts/?entity={self.entity.code}",
            {"vendor": pending_vendor.id, "title": "Blocked agreement"}, format="json",
        )
        self.assertEqual(rejected_contract.status_code, 400)

        requisition_response = self.client.post(
            f"/v1/procurement/requisitions/?entity={self.entity.code}",
            {
                "title": "Paper", "request_date": "2026-08-01",
                "lines": [{
                    "description": "Paper", "quantity": 1,
                    "estimated_unit_price": 1000, "expense_account": "5300",
                }],
            },
            format="json",
        )
        self.assertEqual(requisition_response.status_code, 201)
        requisition = PurchaseRequisition.objects.get(
            pk=requisition_response.data["data"]["id"],
        )
        self.assertEqual(str(requisition.needed_by), "2026-08-08")

        receipt_response = self.client.post(
            f"/v1/procurement/goods-receipts/?entity={self.entity.code}",
            {"lines": [{"description": "Paper", "quantity": 1}]}, format="json",
        )
        self.assertEqual(receipt_response.status_code, 400)
        self.assertIn("purchase_order", receipt_response.data["error"]["detail"])

        verified_vendor = Vendor.objects.create(
            entity=self.entity, code="VERIFIED", name="Verified Vendor",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
            default_expense_account=Account.objects.get(entity=self.entity, code="5300"),
            kyc_status=VendorKycStatus.VERIFIED,
        )
        contract_response = self.client.post(
            f"/v1/procurement/contracts/?entity={self.entity.code}",
            {"vendor": verified_vendor.id, "title": "Supply agreement"}, format="json",
        )
        self.assertEqual(contract_response.status_code, 201)
        contract = VendorContract.objects.get(pk=contract_response.data["data"]["id"])
        self.assertEqual(contract.payment_terms, "NET_60")
        self.assertEqual(contract.renewal_notice_days, 45)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_saved_sourcing_defaults_drive_rfq_dates_and_summary(self, _permission):
        ProcurementSettings.objects.create(
            entity=self.entity,
            default_rfq_response_days=14,
            rfq_closing_soon_days=7,
        )
        create_url = f"/v1/procurement/rfqs/?entity={self.entity.code}"
        created = self.client.post(create_url, {
            "title": "Default response window",
            "issue_date": "2026-08-01",
            "lines": [{
                "description": "Paper", "quantity": 1,
                "expense_account": "5300",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        rfq = RequestForQuotation.objects.get(pk=created.data["data"]["id"])
        self.assertEqual(str(rfq.response_due_date), "2026-08-15")

        explicit = self.client.post(create_url, {
            "title": "Explicit response window",
            "issue_date": "2026-08-01",
            "response_due_date": "2026-08-03",
            "lines": [{
                "description": "Ink", "quantity": 1,
                "expense_account": "5300",
            }],
        }, format="json")
        self.assertEqual(explicit.status_code, 201, explicit.content)
        explicit_rfq = RequestForQuotation.objects.get(pk=explicit.data["data"]["id"])
        self.assertEqual(str(explicit_rfq.response_due_date), "2026-08-03")

        today = datetime.date.today()
        RequestForQuotation.objects.create(
            entity=self.entity, title="Within horizon", issue_date=today,
            response_due_date=today + datetime.timedelta(days=7), rfq_status="ISSUED",
        )
        RequestForQuotation.objects.create(
            entity=self.entity, title="Outside horizon", issue_date=today,
            response_due_date=today + datetime.timedelta(days=8), rfq_status="ISSUED",
        )
        summary = self.client.get(
            f"/v1/procurement/rfqs/summary/?entity={self.entity.code}",
        )
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["data"]["closing_soon"], 1)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_contract_expiring_filter_uses_each_contract_notice_window(self, _permission):
        vendor = Vendor.objects.create(
            entity=self.entity, code="NOTICE", name="Notice Vendor",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
            default_expense_account=Account.objects.get(entity=self.entity, code="5300"),
            kyc_status=VendorKycStatus.VERIFIED,
        )
        today = datetime.date.today()
        VendorContract.objects.create(
            entity=self.entity, vendor=vendor, reference="LONG-NOTICE",
            title="Long notice", start_date=today - datetime.timedelta(days=30),
            end_date=today + datetime.timedelta(days=60), renewal_notice_days=90,
            status="ACTIVE",
        )
        VendorContract.objects.create(
            entity=self.entity, vendor=vendor, reference="SHORT-NOTICE",
            title="Short notice", start_date=today - datetime.timedelta(days=30),
            end_date=today + datetime.timedelta(days=10), renewal_notice_days=5,
            status="ACTIVE",
        )
        response = self.client.get(
            f"/v1/procurement/contracts/?expiring=1&entity={self.entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["reference"] for row in response.data["data"]], ["LONG-NOTICE"],
        )
