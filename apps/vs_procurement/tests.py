"""Phase 3 tests - Procure-to-Pay / Accounts Payable.

Exercises the acceptance criteria: the full PR→PO→GRN→VendorInvoice→VendorPayment
chain posts correct journals, GR/IR nets to zero on a clean three-way match, the AP
sub-ledger reconciles to the AP control account, and vendor payments split AP / bank /
WHT correctly. Run against MySQL:

    ../cx/bin/python manage.py test vs_procurement --settings=apps.settings.local
"""
import datetime
import threading
from decimal import Decimal
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from vs_finance.constants import (
    DocumentStatus, FinanceAuditAction, FinanceAuditStatus, InvoicePaymentStatus,
)
from vs_finance.exceptions import PostingError
from vs_finance.models import (
    Account,
    BankAccount,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    TaxCode,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies

from vs_procurement.constants import (
    ContractStatus,
    MatchStatus,
    MilestoneStatus,
    ProcApprovalState,
    PurchaseOrderVendorDeliverySource,
    PurchaseOrderVendorDeliveryStatus,
    QuotationStatus,
    RfqStatus,
    VendorKycStatus,
)
from vs_procurement.exceptions import ContractError, RequisitionError, SourcingError, ThreeWayMatchError
from vs_procurement.models import (
    CatalogItem,
    ContractMilestone,
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderVendorDelivery,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    ProcurementSettings,
    RequestForQuotation,
    RfqInvitation,
    RfqLine,
    StockItem,
    StockMovement,
    Vendor,
    VendorContact,
    VendorAssessment,
    VendorContract,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
    VendorQuotation,
    VendorQuotationLine,
)
from vs_procurement.contracts import (
    activate_contract,
    complete_milestone,
    expiring_contracts,
    flag_missed_milestones,
    mark_expired,
    renew_contract,
    terminate_contract,
)
from vs_procurement.sourcing import (
    award_quotation,
    cancel_rfq,
    close_rfq,
    issue_rfq,
    set_rfq_invitations,
    submit_quotation,
)
from vs_procurement.payables import (
    post_vendor_invoice,
    post_vendor_payment,
    reverse_vendor_payment,
)
from vs_procurement.purchasing import (
    approve_purchase_order,
    approve_requisition,
    create_po_from_requisition,
    post_grn,
    submit_requisition,
)
from vs_procurement.reports import (
    ap_aging,
    grir_balance,
    procurement_cycle_time,
    reconcile_ap,
    spend_analysis,
    vendor_performance,
)
from vs_procurement.models import VendorCategory
from vs_procurement.stock import (
    adjust_stock,
    issue_stock,
    reorder_report,
    stock_valuation,
)
from vs_procurement.exceptions import InsufficientStockError, StockError


class _P2PFixtureMixin:
    """Builds an entity (seeded chart + open period), a vendor and tax codes."""

    def build_p2p(self):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        period = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        vendor = Vendor.objects.create(
            entity=entity, code="ACME", name="Acme Supplies",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        input_vat = TaxCode.objects.create(
            entity=entity, code="VAT-IN", name="Input VAT 7.5%", rate_bps=750,
            paid_account=self.acc(entity, "1300"),
        )
        wht = TaxCode.objects.create(
            entity=entity, code="WHT-5", name="WHT 5%", rate_bps=500,
            collected_account=self.acc(entity, "2300"),
        )
        return entity, period, vendor, input_vat, wht

    @staticmethod
    def acc(entity, code):
        return Account.objects.get(entity=entity, code=code)

    # --- builders ---------------------------------------------------------- #

    def make_po(self, entity, vendor, lines):
        """lines: [(expense_code, qty, unit_price_kobo, tax_code|None)]."""
        po = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2026, 1, 5),
            status=DocumentStatus.APPROVED,
        )
        for i, (code, qty, price, tax) in enumerate(lines, start=1):
            PurchaseOrderLine.objects.create(
                purchase_order=po, description=f"item {i}",
                expense_account=self.acc(entity, code), quantity=qty,
                unit_price=price, tax_code=tax, line_no=i,
            )
        from vs_procurement.purchasing import price_po
        price_po(po)
        return po

    def make_grn(self, entity, vendor, po, accepts):
        """accepts: [(po_line, accepted_qty)] - unit price taken from the PO line."""
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            received_date=datetime.date(2026, 1, 8),
        )
        for i, (po_line, qty) in enumerate(accepts, start=1):
            GoodsReceivedNoteLine.objects.create(
                grn=grn, po_line=po_line, expense_account=po_line.expense_account,
                accepted_qty=qty, unit_price=po_line.unit_price, line_no=i,
            )
        return grn

    def make_bill(self, entity, vendor, lines, *, po=None, date=datetime.date(2026, 1, 10)):
        """lines: [(expense_code, qty, unit_price, tax_code|None, po_line|None)]."""
        vi = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=date, due_date=date,
            # Service-level posting now requires the same approved governance
            # state as the API. Tests that exercise approval start from an
            # explicitly-created NOT_SUBMITTED invoice instead.
            approval_state=ProcApprovalState.APPROVED,
        )
        for i, (code, qty, price, tax, po_line) in enumerate(lines, start=1):
            VendorInvoiceLine.objects.create(
                vendor_invoice=vi, po_line=po_line,
                expense_account=self.acc(entity, code), quantity=qty,
                unit_price=price, tax_code=tax, line_no=i,
            )
        return vi


class VendorQuotationPortalTests(_P2PFixtureMixin, TestCase):
    """External vendor drafts remain private while firm revisions stay immutable."""

    def setUp(self):
        from vs_procurement.models import VendorContact

        self.entity, _, self.vendor, _, _ = self.build_p2p()
        self.vendor.email = "quotes@acme.test"
        self.vendor.save(update_fields=["email", "updated_at"])
        VendorContact.objects.create(
            vendor=self.vendor, name="Amina Vendor", email=self.vendor.email,
            is_primary=True, receives_rfqs=True,
        )
        self.rfq = RequestForQuotation.objects.create(
            entity=self.entity, title="Office supplies", rfq_status=RfqStatus.ISSUED,
            issue_date=timezone.localdate(),
            response_due_date=timezone.localdate() + datetime.timedelta(days=5),
            response_due_at=timezone.now() + datetime.timedelta(days=5),
        )
        self.line = RfqLine.objects.create(
            rfq=self.rfq, description="Printer paper", quantity=2,
            expense_account=self.acc(self.entity, "5300"), line_no=1,
        )
        self.invitation = RfqInvitation.objects.create(rfq=self.rfq, vendor=self.vendor)

    def _verified(self):
        from django.contrib.auth.hashers import make_password
        from vs_procurement.models import RfqInvitationVerification
        from vs_procurement.vendor_portal import prepare_invitation, verify_code

        with patch("vs_procurement.vendor_portal._safe_notify"):
            raw = prepare_invitation(self.invitation)
        RfqInvitationVerification.objects.create(
            invitation=self.invitation, email=self.vendor.email,
            code_hash=make_password("123456"),
            expires_at=timezone.now() + datetime.timedelta(minutes=10),
        )
        session, _ = verify_code(raw, self.vendor.email, "123456")
        self.invitation.refresh_from_db()
        return raw, session

    def test_signed_invitation_and_session_are_bound_to_one_vendor(self):
        from rest_framework.exceptions import NotFound, ValidationError
        from vs_procurement.models import RfqInvitationSession
        from vs_procurement.vendor_portal import invitation_from_session, invitation_from_token

        raw, session = self._verified()
        invitation, email = invitation_from_session(raw, session)
        self.assertEqual(invitation.pk, self.invitation.pk)
        self.assertEqual(email, self.vendor.email)
        with self.assertRaises(NotFound):
            invitation_from_token(f"{raw}tampered")
        RfqInvitationSession.objects.filter(invitation=self.invitation).update(
            expires_at=timezone.now() - datetime.timedelta(seconds=1),
        )
        with self.assertRaises(ValidationError):
            invitation_from_session(raw, session)

    def test_draft_is_private_and_two_submissions_keep_distinct_receipts(self):
        from vs_procurement.vendor_portal import revise, save_draft, submit
        from vs_procurement.views.orders import _quotation_detail_queryset

        raw, _ = self._verified()
        first = save_draft(self.invitation, self.vendor.email, {
            "reference": "ACME-001",
            "lines": [{
                "rfq_line": self.line.pk, "response_type": "QUOTED",
                "quantity": "2", "unit_price": 100_000,
            }],
        })
        quote_id = first["quotation"]["id"]
        self.assertFalse(_quotation_detail_queryset(self.entity).filter(pk=quote_id).exists())

        submitted = submit(self.invitation, self.vendor.email, raw)
        self.assertEqual(submitted["submission_revision"], 1)
        self.assertTrue(_quotation_detail_queryset(self.entity).filter(pk=quote_id).exists())
        quote = VendorQuotation.objects.get(pk=quote_id)
        first_snapshot = quote.submissions.get(revision=1).snapshot

        revise(self.invitation)
        save_draft(self.invitation, self.vendor.email, {
            "lines": [{
                "rfq_line": self.line.pk, "response_type": "ALTERNATIVE",
                "description": "Recycled printer paper", "quantity": "2",
                "unit_price": 120_000,
            }],
        })
        submitted = submit(self.invitation, self.vendor.email, raw)
        self.assertEqual(submitted["submission_revision"], 2)
        quote.refresh_from_db()
        self.assertEqual(quote.submissions.count(), 2)
        self.assertEqual(quote.submissions.get(revision=1).snapshot, first_snapshot)
        self.assertNotEqual(
            quote.submissions.get(revision=1).snapshot["total"],
            quote.submissions.get(revision=2).snapshot["total"],
        )

    def test_submission_requires_every_current_rfq_line(self):
        from rest_framework.exceptions import ValidationError
        from vs_procurement.vendor_portal import save_draft, submit

        raw, _ = self._verified()
        second = RfqLine.objects.create(
            rfq=self.rfq, description="Toner", quantity=1,
            expense_account=self.acc(self.entity, "5300"), line_no=2,
        )
        save_draft(self.invitation, self.vendor.email, {
            "lines": [{
                "rfq_line": self.line.pk, "response_type": "NO_BID",
                "quantity": "2", "unit_price": 0,
            }],
        })
        with self.assertRaises(ValidationError):
            submit(self.invitation, self.vendor.email, raw)
        self.assertTrue(second.is_active)

    def test_latest_amendment_must_be_acknowledged_before_submission(self):
        from rest_framework.exceptions import ValidationError
        from vs_procurement.vendor_portal import acknowledge_amendment, save_draft, submit

        raw, _ = self._verified()
        self.rfq.version = 2
        self.rfq.save(update_fields=["version", "updated_at"])
        self.invitation.refresh_from_db()
        save_draft(self.invitation, self.vendor.email, {
            "lines": [{
                "rfq_line": self.line.pk, "response_type": "QUOTED",
                "quantity": "2", "unit_price": 100_000,
            }],
        })
        with self.assertRaises(ValidationError):
            submit(self.invitation, self.vendor.email, raw)
        acknowledge_amendment(self.invitation)
        submitted = submit(self.invitation, self.vendor.email, raw)
        self.assertEqual(submitted["submission_revision"], 1)

    def test_vendor_email_uses_procurement_cc_without_duplicate_invitees(self):
        from vs_procurement.vendor_portal import _safe_notify

        self._verified()
        with self.settings(PROCUREMENT_VENDOR_EMAIL_CC=["backend-test@codexng.com"]):
            with patch("vs_procurement.vendor_portal.send_notification") as send:
                self.assertTrue(_safe_notify(
                    event_key="procurement.rfq_invitation",
                    context={},
                    invitation=self.invitation,
                ))
        self.assertEqual(send.call_args.kwargs["metadata"]["cc"], ["backend-test@codexng.com"])

        self.invitation.recipients.create(
            name="Backend Test", email="backend-test@codexng.com",
        )
        with self.settings(PROCUREMENT_VENDOR_EMAIL_CC=["backend-test@codexng.com"]):
            with patch("vs_procurement.vendor_portal.send_notification") as send:
                _safe_notify(
                    event_key="procurement.rfq_invitation",
                    context={},
                    invitation=self.invitation,
                )
        self.assertEqual(send.call_args.kwargs["metadata"]["cc"], [])

    def test_expired_invitation_blocks_draft_but_preserves_existing_data(self):
        from rest_framework.exceptions import ValidationError
        from vs_procurement.vendor_portal import save_draft

        self.invitation.extended_deadline = timezone.now() - datetime.timedelta(seconds=1)
        self.invitation.save(update_fields=["extended_deadline", "updated_at"])
        with self.assertRaises(ValidationError):
            save_draft(self.invitation, self.vendor.email, {"reference": "LATE"})
        self.assertFalse(VendorQuotation.objects.filter(rfq=self.rfq, vendor=self.vendor).exists())


class VendorConsoleAPITests(_P2PFixtureMixin, TestCase):
    def _client(self, entity, email="vendor-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Vendor", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_list_requires_vendor_view_permission(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        response = self._client(entity).get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_list_is_safe_searchable_and_empty_shape_is_stable(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        vendor.email = "accounts@example.com"
        vendor.bank_account_number = "0123456789"
        vendor.save(update_fields=["email", "bank_account_number", "updated_at"])
        client = self._client(entity)
        response = client.get(f"/v1/procurement/vendors/?entity={entity.code}&search=acme")
        self.assertEqual(response.status_code, 200)
        row = response.data["data"][0]
        self.assertEqual(row["code"], "ACME")
        self.assertNotIn("email", row)
        self.assertNotIn("bank_account_number", row)
        Vendor.objects.filter(entity=entity).delete()
        empty = client.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertIn(empty.data["data"], ({}, []))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_active_po_count_excludes_drafts_and_fully_received_orders(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5300", 2, 100_000, None)])
        po.status = DocumentStatus.DRAFT
        po.save(update_fields=["status", "updated_at"])
        client = self._client(entity)

        draft = client.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertEqual(draft.data["data"][0]["active_po_count"], 0)
        approve_purchase_order(po)
        issued = client.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertEqual(issued.data["data"][0]["active_po_count"], 1)
        po.lines.update(received_qty=2)
        complete = client.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertEqual(complete.data["data"][0]["active_po_count"], 0)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_rbac.fls.FieldSecurityMixin._resolve_user_permissions", return_value=set())
    def test_detail_strips_sensitive_fields_and_is_entity_scoped(self, _fls, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        vendor.email = "accounts@example.com"
        vendor.tax_id = "TIN-123"
        vendor.bank_account_number = "0123456789"
        vendor.save(update_fields=["email", "tax_id", "bank_account_number", "updated_at"])
        client = self._client(entity)
        response = client.get(f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("email", response.data["data"])
        self.assertIn("email", response.data["data"]["_stripped_fields"])
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        cross = client.get(f"/v1/procurement/vendors/{vendor.id}/?entity={other.code}")
        self.assertEqual(cross.status_code, 404)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch(
        "vs_rbac.fls.FieldSecurityMixin._resolve_user_permissions",
        return_value={"procurement.vendor.view_sensitive"},
    )
    def test_detail_includes_sensitive_fields_with_exact_grant(self, _fls, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        vendor.email = "accounts@example.com"
        vendor.save(update_fields=["email", "updated_at"])
        response = self._client(entity).get(f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}")
        self.assertEqual(response.data["data"]["email"], "accounts@example.com")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors._has_sensitive_access", return_value=True)
    def test_create_normalizes_identifiers_defaults_governance_and_rejects_duplicates(self, _sensitive, _permission):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        generated = client.post(
            f"/v1/procurement/vendors/?entity={entity.code}",
            {"name": "Generated Vendor"},
            format="json",
        )
        self.assertEqual(generated.status_code, 201)
        self.assertTrue(generated.data["data"]["code"].startswith(f"VN-{entity.tenant_id}"))
        payload = {
            "code": " new-vendor ", "name": " New Vendor ", "tax_id": " tin-22 33 ",
            "email": " Accounts@Example.COM ", "payable_account": "2100",
            "default_expense_account": "5300", "kyc_status": "VERIFIED", "on_hold": True,
        }
        response = client.post(f"/v1/procurement/vendors/?entity={entity.code}", payload, format="json")
        self.assertEqual(response.status_code, 201)
        vendor = Vendor.objects.get(code="NEW-VENDOR")
        self.assertEqual(vendor.tax_id_normalized, "TIN2233")
        self.assertEqual(vendor.email, "accounts@example.com")
        self.assertEqual(vendor.kyc_status, VendorKycStatus.PENDING)
        self.assertFalse(vendor.on_hold)
        duplicate_code = client.post(
            f"/v1/procurement/vendors/?entity={entity.code}",
            {"code": "new-vendor", "name": "Duplicate"}, format="json",
        )
        self.assertEqual(duplicate_code.status_code, 400)
        duplicate_tax = client.post(
            f"/v1/procurement/vendors/?entity={entity.code}",
            {"code": "OTHER", "name": "Duplicate Tax", "tax_id": "TIN 22-33"}, format="json",
        )
        self.assertEqual(duplicate_tax.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors._has_sensitive_access", return_value=False)
    def test_sensitive_update_requires_sensitive_grant(self, _sensitive, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        response = self._client(entity).patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"bank_account_number": "0123456789"}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.vendors.user_has_rbac_permission", return_value=False)
    def test_compliance_update_requires_vendor_manage_in_entity_context(
        self, mock_has_permission, _super_admin, _update_permission,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        response = self._client(entity).patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"kyc_status": "REJECTED", "risk": "HIGH", "on_hold": True}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        vendor.refresh_from_db()
        self.assertEqual(vendor.kyc_status, VendorKycStatus.VERIFIED)
        self.assertEqual(vendor.risk, "LOW")
        self.assertFalse(vendor.on_hold)
        mock_has_permission.assert_called_once()
        self.assertEqual(mock_has_permission.call_args.args[1], "procurement.vendor.manage")
        self.assertEqual(mock_has_permission.call_args.kwargs["tenant"], entity.tenant)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.vendors.user_has_rbac_permission", return_value=True)
    def test_compliance_update_succeeds_with_update_and_manage_permissions(
        self, mock_has_permission, _super_admin, _update_permission,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        response = self._client(entity).patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"kyc_status": "REJECTED", "risk": "HIGH", "on_hold": True}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        vendor.refresh_from_db()
        self.assertEqual(vendor.kyc_status, VendorKycStatus.REJECTED)
        self.assertEqual(vendor.risk, "HIGH")
        self.assertTrue(vendor.on_hold)
        self.assertEqual(mock_has_permission.call_args.args[1], "procurement.vendor.manage")
        self.assertEqual(mock_has_permission.call_args.kwargs["tenant"], entity.tenant)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.vendors.user_has_rbac_permission", return_value=False)
    def test_non_compliance_update_does_not_require_vendor_manage(
        self, mock_has_permission, mock_super_admin, _update_permission,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        response = self._client(entity).patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"name": "Renamed vendor", "is_active": False}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        vendor.refresh_from_db()
        self.assertEqual(vendor.name, "Renamed vendor")
        self.assertFalse(vendor.is_active)
        mock_has_permission.assert_not_called()
        mock_super_admin.assert_not_called()

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.vendors._has_vendor_manage_access", return_value=True)
    def test_update_preserves_states_and_cross_entity_ids_are_hidden(
        self, _manage_permission, _update_permission,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        response = client.patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"kyc_status": "REJECTED", "risk": "HIGH", "on_hold": True, "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        vendor.refresh_from_db()
        self.assertEqual(vendor.kyc_status, "REJECTED")
        self.assertTrue(vendor.on_hold)
        self.assertFalse(vendor.is_active)
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        cross = client.patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={other.code}",
            {"risk": "LOW"}, format="json",
        )
        self.assertEqual(cross.status_code, 404)
        invalid_bool = client.patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"on_hold": "false"}, format="json",
        )
        self.assertEqual(invalid_bool.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_insights_are_entity_scoped_and_authoritative(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self.make_bill(entity, vendor, [("5300", 1, 250_000, None, None)])
        post_vendor_invoice(invoice)
        response = self._client(entity).get(
            f"/v1/procurement/vendors/{vendor.id}/insights/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["spend_ytd"], 250_000)
        self.assertEqual(response.data["data"]["invoice_count"], 1)
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        cross = self._client(entity, "vendor-cross@test.com").get(
            f"/v1/procurement/vendors/{vendor.id}/insights/?entity={other.code}",
        )
        self.assertEqual(cross.status_code, 404)


class SeedProcurementPermissionsTests(TestCase):
    """Procurement permission seeding includes compliance governance idempotently."""

    def test_vendor_manage_is_sensitive_and_granted_to_platform_roles_idempotently(self):
        from io import StringIO

        from django.core.management import call_command
        from vs_rbac.models import Permission, PermissionAction, TenantRolePermission

        output = StringIO()
        call_command("seed_actions", verbosity=0, stdout=output)
        call_command("seed_procurement_permissions", verbosity=0, stdout=output)
        call_command("seed_procurement_permissions", verbosity=0, stdout=output)

        action = PermissionAction.objects.get(name="override_variance")
        self.assertEqual(
            action.description,
            "Override a blocking vendor-invoice match variance for an audited post.",
        )
        permission = Permission.objects.get(key="procurement.vendor.manage")
        self.assertEqual(permission.sensitivity_level, Permission.Sensitivity.SENSITIVE)
        self.assertTrue(permission.is_restricted)
        override = Permission.objects.get(key="procurement.vendor_invoice.override_variance")
        self.assertEqual(override.sensitivity_level, Permission.Sensitivity.CRITICAL)
        self.assertTrue(override.is_restricted)
        competition_override = Permission.objects.get(key="procurement.competition.override")
        self.assertEqual(
            competition_override.sensitivity_level, Permission.Sensitivity.CRITICAL,
        )
        self.assertTrue(competition_override.is_restricted)
        settings_update = Permission.objects.get(key="procurement.settings.update")
        self.assertEqual(settings_update.sensitivity_level, Permission.Sensitivity.SENSITIVE)
        self.assertTrue(settings_update.is_restricted)
        for role_key in ("xvs_super_admin", "xvs_platform_admin"):
            links = TenantRolePermission.objects.filter(
                role__key=role_key, role__tenant__kind="PLATFORM",
                permission=permission, granted=True,
            )
            self.assertEqual(links.count(), 1, role_key)
            self.assertEqual(TenantRolePermission.objects.filter(
                role__key=role_key, role__tenant__kind="PLATFORM",
                permission=override, granted=True,
            ).count(), 1, role_key)
            self.assertEqual(TenantRolePermission.objects.filter(
                role__key=role_key, role__tenant__kind="PLATFORM",
                permission=competition_override, granted=True,
            ).count(), 1, role_key)


class VendorCategoryConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Security-first coverage for category master data and report aggregates."""

    def _client(self, entity, email="category-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Category", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def test_list_requires_authentication_and_category_view_permission(self):
        from rest_framework.test import APIClient

        entity, _, _, _, _ = self.build_p2p()
        unauthenticated = APIClient().get(f"/v1/procurement/categories/?entity={entity.code}")
        self.assertIn(unauthenticated.status_code, (401, 403))
        denied = self._client(entity).get(f"/v1/procurement/categories/?entity={entity.code}")
        self.assertEqual(denied.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_list_filters_searches_counts_vendors_and_keeps_empty_shape_stable(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        active = VendorCategory.objects.create(entity=entity, code=" CLOUD ", name="Cloud")
        VendorCategory.objects.create(entity=entity, code="OLD", name="Legacy", is_active=False)
        vendor.category = active
        vendor.save(update_fields=["category", "updated_at"])
        client = self._client(entity)
        response = client.get(
            f"/v1/procurement/categories/?entity={entity.code}&is_active=true&search=cloud",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["code"], "CLOUD")
        self.assertEqual(response.data["data"][0]["vendor_count"], 1)
        vendor.category = None
        vendor.save(update_fields=["category", "updated_at"])
        VendorCategory.objects.filter(entity=entity).delete()
        empty = client.get(f"/v1/procurement/categories/?entity={entity.code}")
        self.assertIn(empty.data["data"], ({}, []))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_normalizes_code_and_rejects_case_whitespace_duplicates(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        created = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": " cloud ", "name": " Cloud Infrastructure ", "default_expense_account": "5300"},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["data"]["code"], "CLOUD")
        duplicate = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "  cLoUd  ", "name": "Duplicate"}, format="json",
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(VendorCategory.objects.filter(entity=entity).count(), 1)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_hierarchy_derives_three_levels_and_rejects_a_fourth(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        root = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "TECH", "name": "Technology"}, format="json",
        ).data["data"]
        child = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "CLOUD", "name": "Cloud", "parent": root["id"]}, format="json",
        )
        self.assertEqual(child.status_code, 201)
        self.assertEqual(child.data["data"]["level"], 2)
        self.assertEqual(child.data["data"]["parent_code"], "TECH")
        grandchild = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "HOSTED", "name": "Hosted Cloud", "parent": child.data["data"]["id"]},
            format="json",
        )
        self.assertEqual(grandchild.status_code, 201)
        self.assertEqual(grandchild.data["data"]["level"], 3)
        fourth = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "FOURTH", "name": "Too Deep", "parent": grandchild.data["data"]["id"]},
            format="json",
        )
        self.assertEqual(fourth.status_code, 400)
        listed = client.get(f"/v1/procurement/categories/?entity={entity.code}&page_size=100")
        levels = {row["code"]: row["level"] for row in listed.data["data"]}
        self.assertEqual(levels, {"TECH": 1, "CLOUD": 2, "HOSTED": 3})

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_hierarchy_rejects_cross_entity_parent_cycles_and_overdeep_reparent(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        root = VendorCategory.objects.create(entity=entity, code="ROOT", name="Root")
        child = VendorCategory.objects.create(entity=entity, code="CHILD", name="Child", parent=root)
        grandchild = VendorCategory.objects.create(
            entity=entity, code="GRAND", name="Grandchild", parent=child,
        )
        other_root = VendorCategory.objects.create(entity=entity, code="OTHER", name="Other")
        other_child = VendorCategory.objects.create(
            entity=entity, code="OTHER-CHILD", name="Other Child", parent=other_root,
        )
        client = self._client(entity)
        cycle = client.patch(
            f"/v1/procurement/categories/{root.id}/?entity={entity.code}",
            {"parent": grandchild.id}, format="json",
        )
        self.assertEqual(cycle.status_code, 400)
        too_deep = client.patch(
            f"/v1/procurement/categories/{child.id}/?entity={entity.code}",
            {"parent": other_child.id}, format="json",
        )
        self.assertEqual(too_deep.status_code, 400)

        foreign_entity = LedgerEntity.objects.create(
            name="Foreign", code="CAT-PARENT-OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        foreign = VendorCategory.objects.create(entity=foreign_entity, code="FOREIGN", name="Foreign")
        cross = client.patch(
            f"/v1/procurement/categories/{grandchild.id}/?entity={entity.code}",
            {"parent": foreign.id}, format="json",
        )
        self.assertEqual(cross.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_active_hierarchy_requires_active_parent_and_child_first_deactivation(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        parent = VendorCategory.objects.create(entity=entity, code="PARENT", name="Parent")
        child = VendorCategory.objects.create(entity=entity, code="CHILD", name="Child", parent=parent)
        inactive = VendorCategory.objects.create(
            entity=entity, code="INACTIVE", name="Inactive", is_active=False,
        )
        client = self._client(entity)
        rejected_parent = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "ACTIVE", "name": "Active Child", "parent": inactive.id}, format="json",
        )
        self.assertEqual(rejected_parent.status_code, 400)
        rejected_deactivation = client.patch(
            f"/v1/procurement/categories/{parent.id}/?entity={entity.code}",
            {"is_active": False}, format="json",
        )
        self.assertEqual(rejected_deactivation.status_code, 400)
        self.assertEqual(client.patch(
            f"/v1/procurement/categories/{child.id}/?entity={entity.code}",
            {"is_active": False}, format="json",
        ).status_code, 200)
        self.assertEqual(client.patch(
            f"/v1/procurement/categories/{parent.id}/?entity={entity.code}",
            {"is_active": False}, format="json",
        ).status_code, 200)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_validates_lengths_boolean_and_expense_account_rules(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        base = {"code": "SERV", "name": "Services"}
        cases = [
            ({**base, "code": "X" * 33}, "code"),
            ({**base, "name": "X" * 161}, "name"),
            ({**base, "is_active": "false"}, "is_active"),
            ({**base, "default_expense_account": "1100"}, "default_expense_account"),
        ]
        for payload, field in cases:
            with self.subTest(field=field):
                response = client.post(
                    f"/v1/procurement/categories/?entity={entity.code}", payload, format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data["error"]["detail"])

        expense = self.acc(entity, "5300")
        expense.is_active = False
        expense.save(update_fields=["is_active", "updated_at"])
        inactive = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {**base, "default_expense_account": expense.code}, format="json",
        )
        self.assertEqual(inactive.status_code, 400)
        expense.is_active = True
        expense.is_postable = False
        expense.save(update_fields=["is_active", "is_postable", "updated_at"])
        non_postable = client.post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {**base, "default_expense_account": expense.code}, format="json",
        )
        self.assertEqual(non_postable.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_cross_entity_account(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(
            name="Other Books", code="CAT-OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        seed_chart_of_accounts(other)
        foreign = self.acc(other, "5300")
        response = self._client(entity).post(
            f"/v1/procurement/categories/?entity={entity.code}",
            {"code": "SERV", "name": "Services", "default_expense_account": foreign.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_detail_update_is_entity_scoped_code_immutable_and_non_destructive(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(
            entity=entity, code="SERV", name="Services", default_expense_account=self.acc(entity, "5300"),
        )
        vendor.category = category
        vendor.save(update_fields=["category", "updated_at"])
        po = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])
        original_line_ids = list(po.lines.values_list("id", flat=True))
        client = self._client(entity)
        detail = client.get(f"/v1/procurement/categories/{category.id}/?entity={entity.code}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["data"]["vendor_count"], 1)
        changed_code = client.patch(
            f"/v1/procurement/categories/{category.id}/?entity={entity.code}",
            {"code": "OTHER"}, format="json",
        )
        self.assertEqual(changed_code.status_code, 400)
        updated = client.patch(
            f"/v1/procurement/categories/{category.id}/?entity={entity.code}",
            {"name": "Advisory Services", "is_active": False}, format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(list(po.lines.values_list("id", flat=True)), original_line_ids)
        self.assertEqual(PurchaseOrder.objects.filter(pk=po.pk).count(), 1)

        other = LedgerEntity.objects.create(
            name="Other", code="CAT-CROSS", kind=LedgerEntity.Kind.TENANT, tenant=entity.tenant,
        )
        cross = client.patch(
            f"/v1/procurement/categories/{category.id}/?entity={other.code}",
            {"name": "Leaked"}, format="json",
        )
        self.assertEqual(cross.status_code, 404)

    def test_update_requires_exact_backend_permission(self):
        entity, _, _, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(entity=entity, code="SERV", name="Services")
        response = self._client(entity).patch(
            f"/v1/procurement/categories/{category.id}/?entity={entity.code}",
            {"name": "Changed"}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_insights_requires_report_permission(self):
        entity, _, _, _, _ = self.build_p2p()
        response = self._client(entity).get(
            f"/v1/procurement/categories/insights/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_vendor_assignment_rejects_inactive_but_preserves_existing_link(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        inactive = VendorCategory.objects.create(entity=entity, code="OLD", name="Legacy", is_active=False)
        client = self._client(entity)
        rejected = client.post(
            f"/v1/procurement/vendors/?entity={entity.code}",
            {"code": "NEW", "name": "New Vendor", "category": inactive.code}, format="json",
        )
        self.assertEqual(rejected.status_code, 400)
        vendor.category = inactive
        vendor.save(update_fields=["category", "updated_at"])
        preserved = client.patch(
            f"/v1/procurement/vendors/{vendor.id}/?entity={entity.code}",
            {"name": "Acme Updated", "category": inactive.code}, format="json",
        )
        self.assertEqual(preserved.status_code, 200)
        self.assertEqual(preserved.data["data"]["category_code"], "OLD")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_insights_are_report_gated_entity_scoped_and_use_posted_invoices(self, _permission):
        from django.utils import timezone

        entity, _, vendor, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(entity=entity, code="CLOUD", name="Cloud")
        vendor.category = category
        vendor.save(update_fields=["category", "updated_at"])
        invoice = self.make_bill(
            entity, vendor, [("5300", 1, 750_000, None, None)], date=timezone.localdate(),
        )
        invoice.status = DocumentStatus.POSTED
        invoice.subtotal = invoice.total = 750_000
        invoice.save(update_fields=["status", "subtotal", "total", "updated_at"])
        response = self._client(entity).get(
            f"/v1/procurement/categories/insights/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.data["data"] if item["category_id"] == category.id)
        self.assertEqual(row["spend_mtd"], 750_000)
        self.assertEqual(row["spend_ytd"], 750_000)

        other = LedgerEntity.objects.create(
            name="Other", code="INSIGHT-OTHER", kind=LedgerEntity.Kind.TENANT, tenant=entity.tenant,
        )
        cross = self._client(entity, "category-cross@test.com").get(
            f"/v1/procurement/categories/insights/?entity={other.code}",
        )
        self.assertEqual(cross.status_code, 200)
        self.assertIn(cross.data["data"], ({}, []))


class VendorEligibilityTests(_P2PFixtureMixin, TestCase):
    def _approved_requisition(self, entity):
        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 2), status=DocumentStatus.APPROVED,
        )
        PurchaseRequisitionLine.objects.create(
            requisition=req, description="Service", quantity=1, estimated_unit_price=100_000,
            expense_account=self.acc(entity, "5300"), line_no=1,
        )
        return req

    def test_new_po_blocks_inactive_on_hold_and_rejected_kyc_vendors(self):
        entity, _, vendor, _, _ = self.build_p2p()
        for field, value in (("is_active", False), ("on_hold", True), ("kyc_status", "REJECTED")):
            with self.subTest(field=field):
                vendor.is_active = True
                vendor.on_hold = False
                vendor.kyc_status = VendorKycStatus.VERIFIED
                setattr(vendor, field, value)
                vendor.save(update_fields=["is_active", "on_hold", "kyc_status", "updated_at"])
                with self.assertRaises(RequisitionError):
                    create_po_from_requisition(
                        self._approved_requisition(entity), vendor=vendor,
                        order_date=datetime.date(2026, 1, 5),
                    )

    def test_new_po_rejects_cross_entity_vendor(self):
        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        foreign_vendor = Vendor.objects.create(entity=other, code="FOREIGN", name="Foreign Vendor")

        with self.assertRaises(RequisitionError):
            create_po_from_requisition(
                self._approved_requisition(entity), vendor=foreign_vendor,
                order_date=datetime.date(2026, 1, 5),
            )

    def test_inactive_category_default_does_not_seed_new_commitment(self):
        entity, _, vendor, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(
            entity=entity, code="OLD", name="Legacy",
            default_expense_account=self.acc(entity, "5300"), is_active=False,
        )
        vendor.category = category
        vendor.default_expense_account = None
        vendor.save(update_fields=["category", "default_expense_account", "updated_at"])
        req = self._approved_requisition(entity)
        req.lines.update(expense_account=None)
        with self.assertRaises(RequisitionError):
            create_po_from_requisition(
                req, vendor=vendor, order_date=datetime.date(2026, 1, 5),
            )
        category.is_active = True
        category.save(update_fields=["is_active", "updated_at"])
        po = create_po_from_requisition(
            req, vendor=vendor, order_date=datetime.date(2026, 1, 5),
        )
        self.assertEqual(po.lines.get().expense_account.code, "5300")


class GoodsReceiptTests(_P2PFixtureMixin, TestCase):
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_fractional_or_over_remaining_item_counts(self, _permission):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None)])
        line = po.lines.first()
        user = get_user_model().objects.create_user(
            email="grn-quantity@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="GRN", last_name="Tester",
        )
        client = TenantAPIClient(user=user)
        base = {
            "vendor": vendor.code, "purchase_order": po.id,
            "received_date": "2026-01-08",
            "lines": [{
                "po_line": line.id, "description": line.description,
                "expense_account": "5100", "accepted_qty": 4.5,
                "rejected_qty": 0, "unit_price": line.unit_price,
            }],
        }
        fractional = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", base, format="json",
        )
        self.assertEqual(fractional.status_code, 400)
        self.assertEqual(GoodsReceivedNote.objects.filter(entity=entity).count(), 0)

        base["lines"][0].update({"accepted_qty": 6, "rejected_qty": 3})
        over_limit = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", base, format="json",
        )
        self.assertEqual(over_limit.status_code, 400)
        self.assertEqual(GoodsReceivedNote.objects.filter(entity=entity).count(), 0)

        base["lines"][0].update({"accepted_qty": 3, "rejected_qty": 5})
        valid = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", base, format="json",
        )
        self.assertEqual(valid.status_code, 201)
        data = valid.json()["data"]
        self.assertEqual(data["received_item_count"], "3.0000")
        self.assertEqual(data["ordered_item_count"], "8.0000")
        self.assertEqual(data["total_value"], 300_000)
        self.assertEqual(data["lines"][0]["value_amount"], 300_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_nonfinite_and_oversized_quantities_atomically(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None)])
        line = po.lines.get()
        client = self._api_client(entity, "grn-invalid-quantity@test.com")
        payload = {
            "vendor": vendor.code, "purchase_order": po.id,
            "received_date": "2026-01-08",
            "lines": [{"po_line": line.id, "expense_account": "5100",
                       "accepted_qty": 1, "rejected_qty": 0}],
        }
        for value in ("NaN", "Infinity", "10000000"):
            with self.subTest(value=value):
                payload["lines"][0]["accepted_qty"] = value
                response = client.post(
                    f"/v1/procurement/goods-receipts/?entity={entity.code}", payload, format="json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(GoodsReceivedNote.objects.filter(entity=entity).count(), 0)

    def _api_client(self, entity, email):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient
        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Quantity", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def _stock_item(self, entity, *, code="GRN-STOCK", is_active=True, expense=True):
        return StockItem.objects.create(
            entity=entity, code=code, name=f"Stock {code}",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5100") if expense else None,
            is_active=is_active,
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_direct_stock_line_resolves_code_exposes_fields_and_posts_inventory(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity, code="DIRECT-STOCK")
        client = self._api_client(entity, "grn-direct-stock@test.com")
        created = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}",
            {
                "vendor": vendor.code, "received_date": "2026-01-08",
                "lines": [{
                    "stock_item": "direct-stock", "accepted_qty": 2,
                    "rejected_qty": 0, "unit_price": 100_000,
                }],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        row = created.data["data"]["lines"][0]
        self.assertEqual(row["stock_item_id"], item.pk)
        self.assertEqual(row["stock_item_code"], "DIRECT-STOCK")
        self.assertEqual(row["stock_item_name"], "Stock DIRECT-STOCK")
        self.assertEqual(row["expense_code"], "5100")  # direct-line final fallback

        grn_id = created.data["data"]["id"]
        posted = client.post(
            f"/v1/procurement/goods-receipts/{grn_id}/post/?entity={entity.code}",
            {}, format="json",
        )
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.data["data"]["lines"][0]["stock_item_id"], item.pk)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 2)
        self.assertEqual(item.stock_value, 200_000)
        grn = GoodsReceivedNote.objects.get(pk=grn_id)
        journal = {line.account.code: line for line in grn.journal.lines.all()}
        self.assertEqual(journal["1400"].debit, 200_000)
        self.assertEqual(journal["2150"].credit, 200_000)
        self.assertNotIn("5100", journal)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_po_backed_stock_line_resolves_id_and_keeps_po_expense_fallback(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity, code="PO-STOCK", expense=False)
        po = self.make_po(entity, vendor, [("5100", 3, 75_000, None)])
        po_line = po.lines.get()
        client = self._api_client(entity, "grn-po-stock@test.com")
        created = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}",
            {
                "vendor": vendor.code, "purchase_order": po.pk,
                "received_date": "2026-01-08",
                "lines": [{
                    "po_line": po_line.pk, "stock_item": item.pk,
                    "accepted_qty": 3, "rejected_qty": 0,
                }],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        row = created.data["data"]["lines"][0]
        self.assertEqual(row["stock_item_id"], item.pk)
        self.assertEqual(row["expense_account_id"], po_line.expense_account_id)

        grn = GoodsReceivedNote.objects.get(pk=created.data["data"]["id"])
        post_grn(grn)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 3)
        self.assertEqual(item.stock_value, 225_000)
        self.assertEqual(grn.journal.lines.get(account__code="1400").debit, 225_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_stock_line_patch_replaces_and_invalid_refs_rollback_atomically(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity, code="PATCH-STOCK")
        inactive = self._stock_item(entity, code="INACTIVE-STOCK", is_active=False)
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign Stock Books", code="FOREIGN-STOCK",
            kind=LedgerEntity.Kind.TENANT, tenant=entity.tenant,
        )
        seed_chart_of_accounts(foreign_entity)
        foreign = self._stock_item(foreign_entity, code="FOREIGN-ITEM")
        client = self._api_client(entity, "grn-patch-stock@test.com")
        created = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}",
            {
                "vendor": vendor.code, "received_date": "2026-01-08",
                "lines": [{
                    "expense_account": "5300", "accepted_qty": 1,
                    "rejected_qty": 0, "unit_price": 50_000,
                }],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        grn_id = created.data["data"]["id"]
        url = f"/v1/procurement/goods-receipts/{grn_id}/?entity={entity.code}"
        replaced = client.patch(url, {"lines": [{
            "stock_item": "patch-stock", "accepted_qty": 2,
            "rejected_qty": 0, "unit_price": 60_000,
        }]}, format="json")
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(replaced.data["data"]["lines"][0]["stock_item_id"], item.pk)

        for invalid in (foreign.pk, inactive.code, "UNKNOWN-STOCK"):
            with self.subTest(stock_item=invalid):
                rejected = client.patch(url, {"lines": [{
                    "stock_item": invalid, "accepted_qty": 3,
                    "rejected_qty": 0, "unit_price": 70_000,
                }]}, format="json")
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("stock_item", str(rejected.data))
                line = GoodsReceivedNoteLine.objects.get(grn_id=grn_id)
                self.assertEqual(line.stock_item_id, item.pk)
                self.assertEqual(line.accepted_qty, 2)
                self.assertEqual(line.unit_price, 60_000)

        # The same invalid references on create roll back the header as well as its lines.
        before = GoodsReceivedNote.objects.filter(entity=entity).count()
        for index, invalid in enumerate((foreign.pk, inactive.pk, "MISSING-STOCK"), start=1):
            response = client.post(
                f"/v1/procurement/goods-receipts/?entity={entity.code}",
                {
                    "vendor": vendor.code, "received_date": "2026-01-08",
                    "reference": f"BAD-STOCK-{index}",
                    "lines": [{
                        "stock_item": invalid, "accepted_qty": 1,
                        "rejected_qty": 0, "unit_price": 10_000,
                    }],
                },
                format="json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("stock_item", str(response.data))
            self.assertEqual(GoodsReceivedNote.objects.filter(entity=entity).count(), before)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_unapproved_purchase_order(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 1, 100_000, None)])
        po.status = DocumentStatus.DRAFT
        po.save(update_fields=["status"])
        response = self._api_client(entity, "grn-unapproved@test.com").post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}",
            {"vendor": vendor.code, "purchase_order": po.id, "received_date": "2026-01-08",
             "lines": [{"po_line": po.lines.get().id, "expense_account": "5100",
                        "accepted_qty": 1, "rejected_qty": 0}]}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(GoodsReceivedNote.objects.filter(entity=entity).exists())

    def test_partial_receipt_status_compares_received_with_ordered_quantity(self):
        from vs_procurement.serializers import GoodsReceivedNoteSerializer

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 12, 100_000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 4)])
        data = GoodsReceivedNoteSerializer(grn).data

        self.assertEqual(data["received_item_count"], "4.0000")
        self.assertEqual(data["ordered_item_count"], "12.0000")
        self.assertEqual(data["receipt_status"], "PARTIAL")
        self.assertEqual(data["lines"][0]["description"], po.lines.first().description)

    def test_sequential_receipts_preserve_grn_history_and_complete_po_fulfilment(self):
        from vs_procurement.serializers import GoodsReceivedNoteSerializer

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 12, 100_000, None)])
        po_line = po.lines.get()

        first = self.make_grn(entity, vendor, po, [(po_line, 4)])
        first.lines.update(expected_qty=12)
        post_grn(first)
        first = GoodsReceivedNote.objects.prefetch_related("lines", "purchase_order__lines").get(pk=first.pk)
        partial = GoodsReceivedNoteSerializer(first).data

        self.assertEqual(partial["receipt_status"], "PARTIAL")
        self.assertEqual(partial["purchase_order_fulfilment_status"], "PARTIAL")
        self.assertEqual(partial["purchase_order_received_item_count"], "4.0000")
        self.assertEqual(partial["purchase_order_remaining_item_count"], "8.0000")

        second = self.make_grn(entity, vendor, po, [(po_line, 8)])
        second.lines.update(expected_qty=8)
        post_grn(second)
        first = GoodsReceivedNote.objects.prefetch_related("lines", "purchase_order__lines").get(pk=first.pk)
        second = GoodsReceivedNote.objects.prefetch_related("lines", "purchase_order__lines").get(pk=second.pk)
        first_after_completion = GoodsReceivedNoteSerializer(first).data
        completed = GoodsReceivedNoteSerializer(second).data

        # Each delivery keeps its receipt-time snapshot; PO fulfilment is current.
        self.assertEqual(first_after_completion["receipt_status"], "PARTIAL")
        self.assertEqual(first_after_completion["purchase_order_fulfilment_status"], "RECEIVED")
        self.assertEqual(first_after_completion["purchase_order_remaining_item_count"], "0.0000")
        self.assertEqual(completed["receipt_status"], "FULL")
        self.assertEqual(completed["purchase_order_fulfilment_status"], "RECEIVED")
        self.assertEqual(completed["purchase_order_received_item_count"], "12.0000")
        self.assertEqual(completed["purchase_order_ordered_item_count"], "12.0000")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_draft_edit_rewrites_lines_and_can_add_a_line(self, _permission):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None), ("5100", 5, 100_000, None)])
        line_a, line_b = list(po.lines.order_by("line_no"))
        user = get_user_model().objects.create_user(
            email="grn-edit@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="GRN", last_name="Editor",
        )
        client = TenantAPIClient(user=user)
        created = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}",
            {
                "vendor": vendor.code, "purchase_order": po.id, "received_date": "2026-01-08",
                "lines": [{"po_line": line_a.id, "expense_account": "5100", "accepted_qty": 3, "rejected_qty": 0}],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        grn_id = created.json()["data"]["id"]

        # Edit adjusts the first line AND adds a line that was never on the receipt -
        # the old edit path rejected new lines; the rewrite helper accepts them.
        edited = client.patch(
            f"/v1/procurement/goods-receipts/{grn_id}/?entity={entity.code}",
            {"lines": [
                {"po_line": line_a.id, "expense_account": "5100", "accepted_qty": 5, "rejected_qty": 0},
                {"po_line": line_b.id, "expense_account": "5100", "accepted_qty": 2, "rejected_qty": 0},
            ]},
            format="json",
        )
        self.assertEqual(edited.status_code, 200)
        data = edited.json()["data"]
        self.assertEqual(len(data["lines"]), 2)
        self.assertEqual(data["received_item_count"], "7.0000")
        self.assertEqual(data["total_value"], 700_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_draft_edit_rejects_over_remaining_and_posted_receipt(self, _permission):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient
        from vs_procurement.purchasing import post_grn

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None)])
        line = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(line, 3)])
        user = get_user_model().objects.create_user(
            email="grn-edit-guard@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="GRN", last_name="Guard",
        )
        client = TenantAPIClient(user=user)

        over = client.patch(
            f"/v1/procurement/goods-receipts/{grn.id}/?entity={entity.code}",
            {"lines": [{"po_line": line.id, "expense_account": "5100", "accepted_qty": 10, "rejected_qty": 0}]},
            format="json",
        )
        self.assertEqual(over.status_code, 400)

        post_grn(grn, actor_user=user)
        locked = client.patch(
            f"/v1/procurement/goods-receipts/{grn.id}/?entity={entity.code}",
            {"reference": "late edit"}, format="json",
        )
        self.assertEqual(locked.status_code, 400)

    def test_draft_edit_requires_update_permission(self):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 3)])
        user = get_user_model().objects.create_user(
            email="grn-edit-nogrant@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="No", last_name="Grant",
        )
        response = TenantAPIClient(user=user).patch(
            f"/v1/procurement/goods-receipts/{grn.id}/?entity={entity.code}",
            {"reference": "no grant"}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_grn_posts_dr_expense_cr_grir(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 10)])
        returned = post_grn(grn)

        self.assertIs(returned, grn)
        self.assertEqual(grn.status, DocumentStatus.POSTED)
        self.assertIsNotNone(grn.journal_id)
        self.assertEqual(grn.total_value, 1_000_000)
        self.assertEqual(
            grn.document_number,
            f"GN-{grn.entity.tenant_id}{timezone.localdate():%y%m%d}1",
        )

        lines = {l.account.code: l for l in grn.journal.lines.all()}
        self.assertEqual(lines["5100"].debit, 1_000_000)
        self.assertEqual(lines["2150"].credit, 1_000_000)
        # GR/IR now holds the uninvoiced liability.
        self.assertEqual(grir_balance(entity), 1_000_000)
        # PO line received quantity advanced.
        self.assertEqual(po.lines.first().received_qty, 10)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_receipt_cost_center_source_validation_response_and_journal(self, _permission):
        from vs_finance.models import CostCenter

        entity, _, vendor, _, _ = self.build_p2p()
        operations = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        other = CostCenter.objects.create(entity=entity, code="OTHER", name="Other")
        po = self.make_po(entity, vendor, [("5100", 2, 100_000, None)])
        po_line = po.lines.get()
        po_line.cost_center = operations
        po_line.save(update_fields=["cost_center", "updated_at"])
        client = self._api_client(entity, "grn-cost-center@test.com")
        payload = {
            "vendor": vendor.code, "purchase_order": po.id,
            "received_date": "2026-01-08",
            "lines": [{
                "po_line": po_line.id, "expense_account": "5100",
                "accepted_qty": 2, "rejected_qty": 0, "cost_center": operations.code,
            }],
        }

        mismatch = {**payload, "lines": [{**payload["lines"][0], "cost_center": other.code}]}
        self.assertEqual(client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", mismatch, format="json",
        ).status_code, 400)

        response = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", payload, format="json",
        )
        self.assertEqual(response.status_code, 201)
        row = response.data["data"]["lines"][0]
        self.assertEqual(row["cost_center_id"], operations.id)
        self.assertEqual(row["cost_center_code"], "OPS")
        grn = GoodsReceivedNote.objects.get(pk=response.data["data"]["id"])
        post_grn(grn)
        expense_line = grn.journal.lines.get(account__code="5100")
        control_line = grn.journal.lines.get(account__code="2150")
        self.assertEqual(expense_line.cost_center_id, operations.id)
        self.assertIsNone(control_line.cost_center_id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_direct_receipt_cost_center_is_active_and_entity_scoped(self, _permission):
        from vs_finance.models import CostCenter

        entity, _, vendor, _, _ = self.build_p2p()
        own = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        inactive = CostCenter.objects.create(
            entity=entity, code="OLD", name="Inactive", is_active=False,
        )
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign Books", code="FOREIGN-CC", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        foreign = CostCenter.objects.create(
            entity=foreign_entity, code="FOREIGN", name="Foreign",
        )
        client = self._api_client(entity, "grn-direct-cost-center@test.com")
        payload = {
            "vendor": vendor.code, "received_date": "2026-01-08",
            "lines": [{
                "expense_account": "5300", "accepted_qty": 1,
                "rejected_qty": 0, "unit_price": 100_000,
            }],
        }
        for invalid in (inactive.id, foreign.id):
            with self.subTest(cost_center=invalid):
                invalid_payload = {
                    **payload,
                    "lines": [{**payload["lines"][0], "cost_center": invalid}],
                }
                self.assertEqual(client.post(
                    f"/v1/procurement/goods-receipts/?entity={entity.code}",
                    invalid_payload, format="json",
                ).status_code, 400)

        payload["lines"][0]["cost_center"] = own.code
        response = client.post(
            f"/v1/procurement/goods-receipts/?entity={entity.code}", payload, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["data"]["lines"][0]["cost_center_code"], "OPS")
        grn = GoodsReceivedNote.objects.get(pk=response.data["data"]["id"])
        post_grn(grn)
        self.assertEqual(grn.journal.lines.get(account__code="5300").cost_center_id, own.id)


class GoodsReceiptConcurrencyTests(_P2PFixtureMixin, TransactionTestCase):
    """Posting locks make the receipt and PO counters authoritative under races."""

    serialized_rollback = True

    def test_service_rejects_unapproved_po_without_accounting_effects(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 2, 100_000, None)])
        po.status = DocumentStatus.DRAFT
        po.save(update_fields=["status"])
        grn = self.make_grn(entity, vendor, po, [(po.lines.get(), 1)])
        item = StockItem.objects.create(
            entity=entity, code="REJECTED-STOCK", name="Rejected stock",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5100"),
        )
        grn.lines.update(stock_item=item)

        with self.assertRaisesMessage(PostingError, "must be approved"):
            post_grn(grn)

        grn.refresh_from_db()
        self.assertEqual(grn.status, DocumentStatus.DRAFT)
        self.assertIsNone(grn.journal_id)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.GRN_POST_REJECTED,
            target_id=str(grn.pk), status=FinanceAuditStatus.FAILED,
        ).exists())
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.STOCK_RECEIVED,
            target_type="StockItem", target_id=str(item.pk),
        ).exists())

    def test_one_grn_aggregates_duplicate_po_lines_before_overreceipt_check(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        grn = self.make_grn(entity, vendor, po, [(po_line, 6), (po_line, 6)])

        with self.assertRaisesMessage(PostingError, "exceed the ordered quantity"):
            post_grn(grn)

        po_line.refresh_from_db()
        grn.refresh_from_db()
        self.assertEqual(po_line.received_qty, 0)
        self.assertIsNone(grn.journal_id)

    def _race_posts(self, first_grn, second_grn):
        from vs_procurement import purchasing as purchasing_services

        first_in_audit = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_done = threading.Event()
        outcomes = {}

        def blocking_record(**kwargs):
            first_in_audit.set()
            if not release_first.wait(5):
                raise TimeoutError("receipt race did not release the first transaction")

        def worker(name, grn_id, *, attempting=None, done=None):
            close_old_connections()
            try:
                current = GoodsReceivedNote.objects.get(pk=grn_id)
                if attempting:
                    attempting.set()
                outcomes[name] = post_grn(current).status
            except Exception as exc:
                outcomes[f"{name}_error"] = exc
            finally:
                close_old_connections()
                if done:
                    done.set()

        first = threading.Thread(target=worker, args=("first", first_grn.pk), daemon=True)
        second = threading.Thread(
            target=worker, args=("second", second_grn.pk),
            kwargs={"attempting": second_attempting, "done": second_done}, daemon=True,
        )
        with patch.object(purchasing_services, "record", side_effect=blocking_record) as audit_record:
            first.start()
            try:
                self.assertTrue(first_in_audit.wait(5))
                second.start()
                self.assertTrue(second_attempting.wait(5))
                self.assertFalse(second_done.wait(0.25))
            finally:
                release_first.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        return outcomes, audit_record

    def test_same_grn_concurrent_post_has_one_journal_and_one_counter_advance(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        grn = self.make_grn(entity, vendor, po, [(po_line, 4)])

        outcomes, audit_record = self._race_posts(grn, grn)

        self.assertEqual(outcomes.get("first"), DocumentStatus.POSTED)
        self.assertIsInstance(outcomes.get("second_error"), PostingError)
        self.assertEqual(audit_record.call_count, 1)
        grn.refresh_from_db()
        po_line.refresh_from_db()
        self.assertIsNotNone(grn.journal_id)
        self.assertEqual(po_line.received_qty, 4)

    def test_competing_grns_cannot_over_receive_one_po_line(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        first_grn = self.make_grn(entity, vendor, po, [(po_line, 7)])
        second_grn = self.make_grn(entity, vendor, po, [(po_line, 5)])

        outcomes, audit_record = self._race_posts(first_grn, second_grn)

        self.assertEqual(outcomes.get("first"), DocumentStatus.POSTED)
        self.assertIsInstance(outcomes.get("second_error"), PostingError)
        self.assertEqual(audit_record.call_count, 1)
        po_line.refresh_from_db()
        second_grn.refresh_from_db()
        self.assertEqual(po_line.received_qty, 7)
        self.assertEqual(second_grn.status, DocumentStatus.DRAFT)
        self.assertIsNone(second_grn.journal_id)

    def test_concurrent_grns_for_one_stock_item_serialize_the_running_balance(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = StockItem.objects.create(
            entity=entity, code="RACE-STOCK", name="Concurrent stock",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5100"),
        )
        first_po = self.make_po(entity, vendor, [("5100", 2, 100_000, None)])
        second_po = self.make_po(entity, vendor, [("5100", 1, 200_000, None)])
        first_grn = self.make_grn(
            entity, vendor, first_po, [(first_po.lines.get(), 2)],
        )
        second_grn = self.make_grn(
            entity, vendor, second_po, [(second_po.lines.get(), 1)],
        )
        first_grn.lines.update(stock_item=item)
        second_grn.lines.update(stock_item=item)

        outcomes, audit_record = self._race_posts(first_grn, second_grn)

        self.assertEqual(outcomes.get("first"), DocumentStatus.POSTED)
        self.assertEqual(outcomes.get("second"), DocumentStatus.POSTED)
        self.assertEqual(audit_record.call_count, 2)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 3)
        self.assertEqual(item.stock_value, 400_000)
        self.assertEqual(item.movements.count(), 2)


class VendorInvoiceTests(_P2PFixtureMixin, TestCase):
    def test_vendor_reference_constraint_is_case_insensitive_scoped_and_allows_blanks(self):
        from django.db import IntegrityError, transaction

        entity, _, vendor, _, _ = self.build_p2p()
        other_vendor = Vendor.objects.create(
            entity=entity, code="OTHER-REF", name="Other Reference Vendor",
        )
        date = datetime.date(2026, 1, 10)
        VendorInvoice.objects.create(
            entity=entity, vendor=vendor, invoice_date=date, vendor_reference="Inv-001",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            VendorInvoice.objects.create(
                entity=entity, vendor=vendor, invoice_date=date, vendor_reference="INV-001",
            )
        VendorInvoice.objects.create(
            entity=entity, vendor=other_vendor, invoice_date=date, vendor_reference="INV-001",
        )
        VendorInvoice.objects.create(entity=entity, vendor=vendor, invoice_date=date)
        VendorInvoice.objects.create(entity=entity, vendor=vendor, invoice_date=date)

        other_entity = LedgerEntity.objects.create(
            name="Other Reference Books", code="OTHER-REF-BOOKS",
            kind=LedgerEntity.Kind.TENANT, tenant=entity.tenant,
        )
        foreign_vendor = Vendor.objects.create(
            entity=other_entity, code="FOREIGN-REF", name="Foreign Reference Vendor",
        )
        VendorInvoice.objects.create(
            entity=other_entity, vendor=foreign_vendor,
            invoice_date=date, vendor_reference="INV-001",
        )

    def test_post_requires_completed_approval(self):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(entity, vendor, [("5300", 1, 100_000, None, None)])
        vi.approval_state = ProcApprovalState.NOT_SUBMITTED
        vi.save(update_fields=["approval_state"])

        with self.assertRaisesMessage(PostingError, "must be approved"):
            post_vendor_invoice(vi)
        vi.refresh_from_db()
        self.assertEqual(vi.status, DocumentStatus.DRAFT)
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.VENDOR_INVOICE_POSTED,
        ).exists())

    def test_match_aggregates_split_invoice_rows_for_one_po_line(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 5, 100_000, None)])
        po_line = po.lines.first()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 5)]))
        vi = self.make_bill(entity, vendor, [
            ("5100", 3, 100_000, None, po_line),
            ("5100", 3, 100_000, None, po_line),
        ], po=po)

        with self.assertRaises(ThreeWayMatchError):
            post_vendor_invoice(vi)
        vi.refresh_from_db()
        self.assertEqual(vi.match_status, MatchStatus.OVER_BILLED)


    def test_matched_invoice_clears_grir_to_zero(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100000, None)])
        po_line = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        post_grn(grn)

        vi = self.make_bill(entity, vendor, [("5100", 10, 100000, None, po_line)], po=po)
        post_vendor_invoice(vi)

        vi.refresh_from_db()
        self.assertEqual(vi.status, DocumentStatus.POSTED)
        self.assertEqual(vi.match_status, MatchStatus.AUTO_MATCHED)
        lines = {l.account.code: l for l in vi.journal.lines.all()}
        self.assertEqual(lines["2150"].debit, 1_000_000)   # clears GR/IR
        self.assertEqual(lines["2100"].credit, 1_000_000)  # AP raised
        # Goods received AND invoiced → GR/IR nets to zero.
        self.assertEqual(grir_balance(entity), 0)

    def test_non_po_invoice_with_vat_books_input_vat(self):
        entity, _, vendor, input_vat, _ = self.build_p2p()
        vi = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, input_vat, None)])
        post_vendor_invoice(vi)

        vi.refresh_from_db()
        self.assertEqual(vi.subtotal, 1_000_000)
        self.assertEqual(vi.tax_total, 75_000)      # 7.5%
        self.assertEqual(vi.total, 1_075_000)
        lines = {l.account.code: l for l in vi.journal.lines.all()}
        self.assertEqual(lines["5300"].debit, 1_000_000)  # expense direct (no PO)
        self.assertEqual(lines["1300"].debit, 75_000)     # recoverable input VAT
        self.assertEqual(lines["2100"].credit, 1_075_000)
        self.assertNotIn("5160", lines)

    def test_direct_grn_bill_clears_grir_without_booking_expense_twice(self):
        entity, _, vendor, _, _ = self.build_p2p()
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, received_date=datetime.date(2026, 1, 8),
        )
        grn_line = GoodsReceivedNoteLine.objects.create(
            grn=grn, expense_account=self.acc(entity, "5300"), accepted_qty=2,
            unit_price=100_000, line_no=1,
        )
        post_grn(grn)
        invoice = self.make_bill(entity, vendor, [("5300", 2, 100_000, None, None)])
        invoice.lines.update(grn_line=grn_line)

        post_vendor_invoice(invoice)

        invoice.refresh_from_db()
        lines = {line.account.code: line for line in invoice.journal.lines.all()}
        self.assertEqual(lines["2150"].debit, 200_000)
        self.assertNotIn("5300", lines)
        self.assertEqual(grir_balance(entity), 0)

    def test_unfavorable_ppv_with_tax_balances_and_keeps_controls_unallocated(self):
        from vs_finance.models import CostCenter

        entity, _, vendor, input_vat, _ = self.build_p2p()
        center = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        po_line.cost_center = center
        po_line.save(update_fields=["cost_center", "updated_at"])
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        grn.lines.update(cost_center=center)
        post_grn(grn)
        invoice = self.make_bill(
            entity, vendor, [("5100", 10, 120_000, input_vat, po_line)], po=po,
        )
        invoice.lines.update(cost_center=center, grn_line=grn.lines.get())

        post_vendor_invoice(invoice)

        invoice.refresh_from_db()
        lines = {line.account.code: line for line in invoice.journal.lines.all()}
        self.assertEqual(lines["2150"].debit, 1_000_000)
        self.assertEqual(lines["5160"].debit, 200_000)
        self.assertEqual(lines["5160"].cost_center_id, center.id)
        self.assertEqual(lines["1300"].debit, 90_000)
        self.assertEqual(lines["2100"].credit, 1_290_000)
        for code in ("2150", "1300", "2100"):
            self.assertIsNone(lines[code].cost_center_id)
        self.assertEqual(grir_balance(entity), 0)

    def test_favorable_ppv_uses_linked_grn_price_before_po_price(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        # Receipt snapshot differs from both the PO and invoice. It is the historical
        # GR/IR basis and must win when both links are present.
        grn.lines.update(unit_price=110_000)
        grn_line = grn.lines.get()
        post_grn(grn)
        invoice = self.make_bill(
            entity, vendor, [("5100", 10, 90_000, None, po_line)], po=po,
        )
        invoice.lines.update(grn_line=grn_line)

        post_vendor_invoice(invoice)

        invoice.refresh_from_db()
        lines = {line.account.code: line for line in invoice.journal.lines.all()}
        self.assertEqual(lines["2150"].debit, 1_100_000)
        self.assertEqual(lines["5160"].credit, 200_000)
        self.assertEqual(lines["2100"].credit, 900_000)
        self.assertEqual(grir_balance(entity), 0)

    def test_linked_direct_grn_price_variance_posts_ppv(self):
        entity, _, vendor, _, _ = self.build_p2p()
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, received_date=datetime.date(2026, 1, 8),
        )
        grn_line = GoodsReceivedNoteLine.objects.create(
            grn=grn, expense_account=self.acc(entity, "5300"), accepted_qty=2,
            unit_price=100_000, line_no=1,
        )
        post_grn(grn)
        invoice = self.make_bill(entity, vendor, [("5300", 2, 125_000, None, None)])
        invoice.lines.update(grn_line=grn_line)

        post_vendor_invoice(invoice)

        invoice.refresh_from_db()
        lines = {line.account.code: line for line in invoice.journal.lines.all()}
        self.assertEqual(lines["2150"].debit, 200_000)
        self.assertEqual(lines["5160"].debit, 50_000)
        self.assertEqual(lines["2100"].credit, 250_000)
        self.assertEqual(grir_balance(entity), 0)

    def test_missing_ppv_account_is_not_resolved_without_variance(self):
        entity, _, vendor, _, _ = self.build_p2p()
        Account.objects.filter(entity=entity, code="5160").delete()
        po = self.make_po(entity, vendor, [("5100", 2, 100_000, None)])
        po_line = po.lines.get()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 2)]))
        invoice = self.make_bill(
            entity, vendor, [("5100", 2, 100_000, None, po_line)], po=po,
        )
        post_vendor_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.POSTED)

    def test_inactive_ppv_account_blocks_only_a_nonzero_variance(self):
        from vs_procurement.exceptions import MissingControlAccountError

        entity, _, vendor, _, _ = self.build_p2p()
        Account.objects.filter(entity=entity, code="5160").update(is_active=False)
        po = self.make_po(entity, vendor, [("5100", 2, 100_000, None)])
        po_line = po.lines.get()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 2)]))
        invoice = self.make_bill(
            entity, vendor, [("5100", 2, 120_000, None, po_line)], po=po,
        )
        with self.assertRaises(MissingControlAccountError):
            post_vendor_invoice(invoice)

    def test_over_billed_is_blocked_and_audited(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100000, None)])
        po_line = po.lines.first()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 10)]))

        # Bill 12 against an order of 10.
        vi = self.make_bill(entity, vendor, [("5100", 12, 100000, None, po_line)], po=po)
        with self.assertRaises(ThreeWayMatchError):
            post_vendor_invoice(vi)

        vi.refresh_from_db()
        self.assertEqual(vi.status, DocumentStatus.DRAFT)
        self.assertEqual(vi.match_status, MatchStatus.OVER_BILLED)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity, action="VENDOR_INVOICE_POST_REJECTED",
                status=FinanceAuditStatus.FAILED,
            ).exists()
        )

    def test_under_received_is_blocked(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100000, None)])
        po_line = po.lines.first()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 4)]))  # only 4 received

        vi = self.make_bill(entity, vendor, [("5100", 10, 100000, None, po_line)], po=po)
        with self.assertRaises(ThreeWayMatchError):
            post_vendor_invoice(vi)
        vi.refresh_from_db()
        self.assertEqual(vi.match_status, MatchStatus.UNDER_RECEIVED)


class VendorInvoiceConsoleAPITests(_P2PFixtureMixin, TestCase):
    def _client(self, entity):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient
        user = get_user_model().objects.create_user(
            email="vendor-invoice-console@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Invoice", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_summary_requires_vendor_invoice_view_permission(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        response = self._client(entity).get(
            f"/v1/procurement/vendor-invoices/summary/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_detail_does_not_cross_entity_scope(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self.make_bill(entity, vendor, [("5300", 1, 100_000, None, None)])
        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        response = self._client(entity).get(
            f"/v1/procurement/vendor-invoices/{invoice.id}/?entity={other.code}",
        )
        self.assertEqual(response.status_code, 404)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_nonfinite_oversized_and_overprecision_quantity_atomically(
        self, _permission,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        payload = {
            "vendor": vendor.code, "invoice_date": "2026-01-10",
            "vendor_reference": "INV-BAD-QTY",
            "lines": [{"expense_account": "5300", "quantity": 1, "unit_price": 100_000}],
        }
        for value in ("NaN", "Infinity", "10000000", "1.00001"):
            with self.subTest(value=value):
                payload["lines"][0]["quantity"] = value
                response = client.post(
                    f"/v1/procurement/vendor-invoices/?entity={entity.code}", payload, format="json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(VendorInvoice.objects.filter(entity=entity).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_invoice_cost_center_precedence_validation_and_response(self, _permission):
        from vs_finance.models import CostCenter

        entity, _, vendor, _, _ = self.build_p2p()
        own = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        other = CostCenter.objects.create(entity=entity, code="OTHER", name="Other")
        inactive = CostCenter.objects.create(
            entity=entity, code="OLD", name="Inactive", is_active=False,
        )
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign CC Books", code="FOREIGN-INV-CC",
            kind=LedgerEntity.Kind.TENANT, tenant=entity.tenant,
        )
        foreign = CostCenter.objects.create(
            entity=foreign_entity, code="FOREIGN", name="Foreign",
        )
        client = self._client(entity)
        direct = {
            "vendor": vendor.code, "invoice_date": "2026-01-10",
            "vendor_reference": "CC-DIRECT",
            "lines": [{
                "expense_account": "5300", "quantity": 1,
                "unit_price": 100_000, "cost_center": own.code,
            }],
        }
        response = client.post(
            f"/v1/procurement/vendor-invoices/?entity={entity.code}", direct, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["data"]["lines"][0]["cost_center_id"], own.id)
        self.assertEqual(response.data["data"]["lines"][0]["cost_center_code"], "OPS")
        direct_invoice = VendorInvoice.objects.get(pk=response.data["data"]["id"])
        direct_invoice.approval_state = ProcApprovalState.APPROVED
        direct_invoice.save(update_fields=["approval_state", "updated_at"])
        post_vendor_invoice(direct_invoice)
        direct_invoice.refresh_from_db()
        expense_line = direct_invoice.journal.lines.get(account__code="5300")
        ap_line = direct_invoice.journal.lines.get(account__code="2100")
        self.assertEqual(expense_line.cost_center_id, own.id)
        self.assertIsNone(ap_line.cost_center_id)

        for index, invalid in enumerate((inactive.id, foreign.id), start=1):
            invalid_payload = {
                **direct, "vendor_reference": f"CC-INVALID-{index}",
                "lines": [{**direct["lines"][0], "cost_center": invalid}],
            }
            self.assertEqual(client.post(
                f"/v1/procurement/vendor-invoices/?entity={entity.code}",
                invalid_payload, format="json",
            ).status_code, 400)

        po = self.make_po(entity, vendor, [("5100", 1, 100_000, None)])
        po_line = po.lines.get()
        po_line.cost_center = own
        po_line.save(update_fields=["cost_center", "updated_at"])
        po_payload = {
            "vendor": vendor.code, "purchase_order": po.id,
            "invoice_date": "2026-01-10", "vendor_reference": "CC-PO",
            "lines": [{
                "po_line": po_line.id, "expense_account": "5100",
                "quantity": 1, "unit_price": 100_000, "cost_center": own.id,
            }],
        }
        po_response = client.post(
            f"/v1/procurement/vendor-invoices/?entity={entity.code}", po_payload, format="json",
        )
        self.assertEqual(po_response.status_code, 201)
        self.assertEqual(po_response.data["data"]["lines"][0]["cost_center_code"], "OPS")
        mismatch = {
            **po_payload, "vendor_reference": "CC-PO-MISMATCH",
            "lines": [{**po_payload["lines"][0], "cost_center": other.id}],
        }
        self.assertEqual(client.post(
            f"/v1/procurement/vendor-invoices/?entity={entity.code}", mismatch, format="json",
        ).status_code, 400)

        direct_grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, received_date=datetime.date(2026, 1, 8),
        )
        grn_line = GoodsReceivedNoteLine.objects.create(
            grn=direct_grn, expense_account=self.acc(entity, "5300"),
            cost_center=own, accepted_qty=1, unit_price=100_000, line_no=1,
        )
        post_grn(direct_grn)
        grn_response = client.post(
            f"/v1/procurement/vendor-invoices/?entity={entity.code}",
            {
                "vendor": vendor.code, "invoice_date": "2026-01-10",
                "vendor_reference": "CC-GRN",
                "lines": [{
                    "grn_line": grn_line.id, "expense_account": "5300",
                    "quantity": 1, "unit_price": 100_000,
                }],
            }, format="json",
        )
        self.assertEqual(grn_response.status_code, 201)
        self.assertEqual(grn_response.data["data"]["lines"][0]["cost_center_code"], "OPS")

    @patch("vs_procurement.views.receiving._validate_vendor_reference")
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_reference_race_maps_named_constraint_to_field_error(
        self, _permission, validate_reference,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        VendorInvoice.objects.create(
            entity=entity, vendor=vendor, invoice_date=datetime.date(2026, 1, 10),
            vendor_reference="INV-RACE",
        )
        validate_reference.return_value = "inv-race"
        response = self._client(entity).post(
            f"/v1/procurement/vendor-invoices/?entity={entity.code}",
            {
                "vendor": vendor.code, "invoice_date": "2026-01-10",
                "vendor_reference": "inv-race",
                "lines": [{"expense_account": "5300", "quantity": 1, "unit_price": 100_000}],
            }, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("vendor_reference", str(response.data))

    @patch("vs_procurement.views.receiving._validate_vendor_reference")
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_patch_reference_race_maps_named_constraint_to_field_error(
        self, _permission, validate_reference,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        first = self.make_bill(entity, vendor, [("5300", 1, 100_000, None, None)])
        first.vendor_reference = "INV-FIRST"
        first.save(update_fields=["vendor_reference", "updated_at"])
        second = self.make_bill(entity, vendor, [("5300", 1, 100_000, None, None)])
        second.vendor_reference = "INV-SECOND"
        second.approval_state = ProcApprovalState.NOT_SUBMITTED
        second.save(update_fields=["vendor_reference", "approval_state", "updated_at"])
        validate_reference.return_value = "inv-first"
        response = self._client(entity).patch(
            f"/v1/procurement/vendor-invoices/{second.id}/?entity={entity.code}",
            {"vendor_reference": "inv-first"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("vendor_reference", str(response.data))
        second.refresh_from_db()
        self.assertEqual(second.vendor_reference, "INV-SECOND")

    def _blocking_invoice(self, entity, vendor):
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 4)]))
        return self.make_bill(entity, vendor, [("5100", 10, 100_000, None, po_line)], po=po)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_post_rejects_string_allow_variance(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self._blocking_invoice(entity, vendor)
        response = self._client(entity).post(
            f"/v1/procurement/vendor-invoices/{invoice.id}/post/?entity={entity.code}",
            {"allow_variance": "false"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.DRAFT)

    @patch("vs_procurement.views.receiving.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.receiving.user_has_rbac_permission", return_value=False)
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_post_variance_override_requires_dedicated_permission(
        self, _base_permission, _override_permission, _super_admin,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self._blocking_invoice(entity, vendor)
        response = self._client(entity).post(
            f"/v1/procurement/vendor-invoices/{invoice.id}/post/?entity={entity.code}",
            {"allow_variance": True}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.DRAFT)

    @patch("vs_procurement.views.receiving.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.receiving.user_has_rbac_permission", return_value=True)
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_authorized_variance_override_posts_and_audits_evidence(
        self, _base_permission, _override_permission, _super_admin,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self._blocking_invoice(entity, vendor)
        response = self._client(entity).post(
            f"/v1/procurement/vendor-invoices/{invoice.id}/post/?entity={entity.code}",
            {"allow_variance": True}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        audit = FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.VENDOR_INVOICE_POSTED,
            target_id=str(invoice.id),
        ).latest("id")
        self.assertIs(audit.metadata["allow_variance"], True)
        self.assertIs(audit.metadata["variance_override_used"], True)


class VendorPaymentTests(_P2PFixtureMixin, TestCase):
    def _posted_bill(self, entity, vendor, total=1_000_000):
        vi = self.make_bill(entity, vendor, [("5300", 1, total, None, None)])
        post_vendor_invoice(vi)
        vi.refresh_from_db()
        return vi

    def test_payment_with_wht_splits_ap_bank_wht(self):
        entity, _, vendor, _, wht = self.build_p2p()
        vi = self._posted_bill(entity, vendor, total=1_000_000)

        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=1_000_000, wht_amount=50_000,
            payment_account=self.acc(entity, "1100"), wht_tax_code=wht,
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)

        pay.refresh_from_db()
        self.assertEqual(pay.status, DocumentStatus.POSTED)
        self.assertEqual(pay.net_amount, 950_000)
        self.assertEqual(pay.allocated_amount, 1_000_000)
        lines = {l.account.code: l for l in pay.journal.lines.all()}
        self.assertEqual(lines["2100"].debit, 1_000_000)  # AP settled (gross)
        self.assertEqual(lines["1100"].credit, 950_000)   # cash out (net)
        self.assertEqual(lines["2300"].credit, 50_000)    # WHT payable
        vi.refresh_from_db()
        self.assertEqual(vi.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(vi.amount_paid, 1_000_000)

    def test_payment_requires_workflow_approval(self):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
        )

        with self.assertRaisesMessage(PostingError, "must be approved"):
            post_vendor_payment(pay)
        pay.refresh_from_db()
        self.assertIsNone(pay.journal_id)

    def test_explicit_allocation_rejects_another_vendor_invoice(self):
        entity, _, vendor, _, _ = self.build_p2p()
        other = Vendor.objects.create(
            entity=entity, code="OTHER", name="Other Vendor", kyc_status="VERIFIED",
            payable_account=self.acc(entity, "2100"), default_expense_account=self.acc(entity, "5300"),
        )
        invoice = self._posted_bill(entity, other)
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )

        with self.assertRaisesMessage(PostingError, "entity and vendor"):
            post_vendor_payment(pay, allocations=[(invoice, 100_000)])

    def test_explicit_allocation_validates_the_full_plan_before_posting(self):
        entity, _, vendor, _, _ = self.build_p2p()
        first = self._posted_bill(entity, vendor, total=100_000)
        second = self._posted_bill(entity, vendor, total=100_000)
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )

        with self.assertRaisesMessage(PostingError, "exceeds the payment gross"):
            post_vendor_payment(pay, allocations=[(first, 100_000), (second, 100_000)])

        pay.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(pay.journal_id)
        self.assertEqual(first.amount_paid, 0)
        self.assertEqual(second.amount_paid, 0)

    def test_reversal_restores_invoice_settlement(self):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self._posted_bill(entity, vendor)
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=1_000_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)

        reverse_vendor_payment(pay, date=datetime.date(2026, 1, 20))
        pay.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(pay.status, DocumentStatus.REVERSED)
        self.assertEqual(pay.journal.status, DocumentStatus.REVERSED)
        self.assertEqual(invoice.amount_paid, 0)
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)


class VendorPaymentConsoleAPITests(_P2PFixtureMixin, TestCase):
    def _posted_bill(self, entity, vendor, total=1_000_000):
        invoice = self.make_bill(entity, vendor, [("5300", 1, total, None, None)])
        post_vendor_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def _client(self, entity):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email="vendor-payment-console@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Payment", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_list_requires_vendor_payment_view_permission(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        response = self._client(entity).get(
            f"/v1/procurement/vendor-payments/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_every_vendor_payment_mutation_requires_backend_permission(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
        )
        client = self._client(entity)
        routes = [
            ("post", f"/v1/procurement/vendor-payments/?entity={entity.code}"),
            ("patch", f"/v1/procurement/vendor-payments/{payment.id}/?entity={entity.code}"),
            ("post", f"/v1/procurement/vendor-payments/{payment.id}/submit/?entity={entity.code}"),
            ("post", f"/v1/procurement/vendor-payments/{payment.id}/post/?entity={entity.code}"),
            ("post", f"/v1/procurement/vendor-payments/{payment.id}/cancel/?entity={entity.code}"),
            ("post", f"/v1/procurement/vendor-payments/{payment.id}/reverse/?entity={entity.code}"),
        ]
        for method, url in routes:
            with self.subTest(url=url):
                response = getattr(client, method)(url, {}, format="json")
                self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_detail_does_not_cross_entity_scope(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
        )
        other = LedgerEntity.objects.create(name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        response = self._client(entity).get(
            f"/v1/procurement/vendor-payments/{payment.id}/?entity={other.code}",
        )
        self.assertEqual(response.status_code, 404)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_persists_plan_without_settling_invoice(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, None, None)])
        post_vendor_invoice(invoice)
        invoice.refresh_from_db()
        bank = BankAccount.objects.create(
            entity=entity, gl_account=self.acc(entity, "1100"), name="Operating Bank",
        )
        response = self._client(entity).post(
            f"/v1/procurement/vendor-payments/?entity={entity.code}",
            {
                "vendor": vendor.code, "payment_date": "2026-01-15",
                "bank_account": bank.id, "method": "BANK_TRANSFER", "wht_amount": 50_000,
                "allocations": [{"vendor_invoice": invoice.id, "amount": 400_000}],
            }, format="json",
        )
        self.assertEqual(response.status_code, 201)
        invoice.refresh_from_db()
        payment = VendorPayment.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(payment.gross_amount, 400_000)
        self.assertEqual(payment.net_amount, 350_000)
        self.assertEqual(payment.allocated_amount, 0)
        self.assertEqual(invoice.amount_paid, 0)
        self.assertEqual(payment.allocations.get().amount, 400_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_detail_formats_payment_activity_in_naira(self, _permission):
        entity, _, vendor, _, _ = self.build_p2p()
        invoice = self._posted_bill(entity, vendor)
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=400_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(payment, allocations=[(invoice, 400_000)])

        response = self._client(entity).get(
            f"/v1/procurement/vendor-payments/{payment.id}/?entity={entity.code}",
        )

        self.assertEqual(response.status_code, 200)
        messages = " ".join(row["message"] for row in response.data["data"]["activity"])
        self.assertIn("₦", messages)
        self.assertNotIn("kobo", messages.lower())

    def test_partial_payment_marks_partial(self):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self._posted_bill(entity, vendor, total=1_000_000)

        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=400_000, wht_amount=0,
            payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)
        vi.refresh_from_db()
        self.assertEqual(vi.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(vi.amount_paid, 400_000)

    def test_on_hold_vendor_blocks_payment(self):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)
        vendor.on_hold = True
        vendor.save(update_fields=["on_hold"])

        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=100_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        from vs_finance.exceptions import PostingError
        with self.assertRaises(PostingError):
            post_vendor_payment(pay)
        pay.refresh_from_db()
        self.assertEqual(pay.status, DocumentStatus.DRAFT)


class APReconciliationTests(_P2PFixtureMixin, TestCase):
    def test_ap_reconciles_through_invoice_and_payment(self):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, None, None)])
        post_vendor_invoice(vi)

        rec = reconcile_ap(entity)
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.subledger_total, 1_000_000)
        self.assertEqual(rec.control_total, 1_000_000)

        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 20),
            gross_amount=600_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)

        rec = reconcile_ap(entity)
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.subledger_total, 400_000)
        self.assertEqual(ap_aging(entity).total_net, 400_000)


class FullChainTests(_P2PFixtureMixin, TestCase):
    def test_pr_to_payment_end_to_end(self):
        entity, _, vendor, _, _ = self.build_p2p()

        # Requisition → approve.
        pr = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 2),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=pr, description="chairs", quantity=5,
            estimated_unit_price=200_000, expense_account=self.acc(entity, "5100"),
            line_no=1,
        )
        submit_requisition(pr)
        approve_requisition(pr)
        pr.refresh_from_db()
        self.assertEqual(pr.status, DocumentStatus.APPROVED)
        self.assertEqual(pr.estimated_total, 1_000_000)
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity, action="REQUISITION_APPROVED",
            ).exists()
        )

        # PR → PO.
        po = create_po_from_requisition(
            pr, vendor=vendor, order_date=datetime.date(2026, 1, 5),
        )
        self.assertEqual(po.total, 1_000_000)
        approve_purchase_order(po)
        po.refresh_from_db()
        po_line = po.lines.first()

        # PO → GRN.
        post_grn(self.make_grn(entity, vendor, po, [(po_line, 5)]))
        self.assertEqual(grir_balance(entity), 1_000_000)

        # GRN → vendor invoice (clears GR/IR).
        vi = self.make_bill(entity, vendor, [("5100", 5, 200_000, None, po_line)], po=po)
        post_vendor_invoice(vi)
        self.assertEqual(grir_balance(entity), 0)

        # Invoice → payment (full).
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 25),
            gross_amount=1_000_000, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)

        vi.refresh_from_db()
        self.assertEqual(vi.payment_status, InvoicePaymentStatus.PAID)
        self.assertTrue(reconcile_ap(entity).is_reconciled)
        self.assertEqual(reconcile_ap(entity).control_total, 0)


class RequisitionConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Entity scoping and derived values for the rebuilt requisition console."""

    def client_for(self, entity):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=f"requisitions-{entity.code.lower()}@test.com", password="pw",
            tenant=entity.tenant, user_type="CX_STAFF", status="ACTIVE",
            first_name="Console", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    @patch("vs_procurement.views.requisitions.timezone.localdate", return_value=datetime.date(2026, 1, 20))
    def test_summary_uses_entity_scoped_server_aggregates(self, _today, _permission):
        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER-REQ", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        for target, status, amount, day in [
            (entity, DocumentStatus.PENDING_APPROVAL, 300_000, 5),
            (entity, DocumentStatus.APPROVED, 500_000, 10),
            (entity, DocumentStatus.DRAFT, 200_000, 12),
            (other, DocumentStatus.APPROVED, 99_000_000, 10),
        ]:
            PurchaseRequisition.objects.create(
                entity=target, request_date=datetime.date(2026, 1, day),
                status=status, estimated_total=amount,
            )

        response = self.client_for(entity).get(
            f"/v1/procurement/requisitions/summary/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["pending_approval"], {"count": 1, "amount": 300_000})
        self.assertEqual(data["approved_mtd"]["count"], 1)
        # One approved this month, none in the prior month → absolute delta of +1.
        self.assertEqual(data["approved_mtd"]["change"], 1)
        self.assertEqual(data["draft"], {"count": 1, "amount": 200_000})
        self.assertEqual(data["total_value_mtd"]["amount"], 1_000_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_persists_display_fields_and_rejects_foreign_cost_center(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        from vs_finance.models import CostCenter

        own_center = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        client = self.client_for(entity)
        payload = {
            "title": "Replace meeting-room chairs", "request_date": "2026-01-10",
            "needed_by": "2026-02-01", "cost_center": own_center.code,
            "justification": "Existing chairs are damaged.",
            "lines": [{
                "description": "Ergonomic chair", "quantity": 5, "unit": "Each",
                "estimated_unit_price": 200_000,
            }],
        }
        response = client.post(
            f"/v1/procurement/requisitions/?entity={entity.code}", payload, format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["title"], payload["title"])
        self.assertEqual(data["cost_center_code"], "OPS")
        self.assertEqual(data["estimated_total"], 1_000_000)
        self.assertEqual(data["lines"][0]["unit"], "Each")

        other = LedgerEntity.objects.create(
            name="Foreign Books", code="FOREIGN-REQ", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        foreign_center = CostCenter.objects.create(entity=other, code="FOREIGN", name="Foreign")
        payload["cost_center"] = foreign_center.id
        denied = client.post(
            f"/v1/procurement/requisitions/?entity={entity.code}", payload, format="json",
        )
        self.assertEqual(denied.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_and_patch_reject_invalid_quantities_without_surviving_rows(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        client = self.client_for(entity)
        create_url = f"/v1/procurement/requisitions/?entity={entity.code}"

        def payload(quantity, *, title="Quantity validation"):
            return {
                "title": title, "request_date": "2026-01-10",
                "lines": [{
                    "description": "Measured item", "quantity": quantity,
                    "estimated_unit_price": 100_000,
                }],
            }

        invalid_quantities = (0, -1, "NaN", "Infinity", "-Infinity", "10000000000.0000")
        for quantity in invalid_quantities:
            with self.subTest(method="POST", quantity=quantity):
                response = client.post(
                    create_url, payload(quantity, title="Invalid quantity"), format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("quantity", response.data["error"]["detail"])
                self.assertFalse(PurchaseRequisition.objects.filter(
                    entity=entity, title="Invalid quantity",
                ).exists())
                self.assertFalse(PurchaseRequisitionLine.objects.filter(
                    requisition__entity=entity,
                ).exists())

        created = client.post(create_url, payload("1.2500"), format="json")
        self.assertEqual(created.status_code, 201)
        requisition = PurchaseRequisition.objects.get(pk=created.data["data"]["id"])
        original_line = requisition.lines.get()
        self.assertEqual(str(original_line.quantity), "1.2500")

        patch_url = f"/v1/procurement/requisitions/{requisition.pk}/?entity={entity.code}"
        for quantity in invalid_quantities:
            with self.subTest(method="PATCH", quantity=quantity):
                response = client.patch(
                    patch_url, {"lines": payload(quantity)["lines"]}, format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("quantity", response.data["error"]["detail"])
                self.assertEqual(requisition.lines.count(), 1)
                self.assertEqual(requisition.lines.get().pk, original_line.pk)
                self.assertEqual(str(requisition.lines.get().quantity), "1.2500")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_rejected_filter_uses_approval_overlay(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        from vs_procurement.constants import ProcApprovalState

        rejected = PurchaseRequisition.objects.create(
            entity=entity, title="Rejected request", request_date=datetime.date(2026, 1, 2),
            status=DocumentStatus.CANCELLED, approval_state=ProcApprovalState.REJECTED,
        )
        PurchaseRequisition.objects.create(
            entity=entity, title="Ordinary cancellation", request_date=datetime.date(2026, 1, 3),
            status=DocumentStatus.CANCELLED,
        )
        response = self.client_for(entity).get(
            f"/v1/procurement/requisitions/?entity={entity.code}&status=REJECTED",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["data"]], [rejected.id])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_search_filters_broadly_and_clearing_restores_all_rows(self, _permission):
        entity, _, _, _, _ = self.build_p2p()
        matching = PurchaseRequisition.objects.create(
            entity=entity, title="Office refresh", request_date=datetime.date(2026, 1, 2),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=matching, description="Ergonomic conference chair",
            quantity=1, estimated_unit_price=100_000,
        )
        other = PurchaseRequisition.objects.create(
            entity=entity, title="Network upgrade", request_date=datetime.date(2026, 1, 3),
        )
        client = self.client_for(entity)

        filtered = client.get(
            f"/v1/procurement/requisitions/?entity={entity.code}&search=conference",
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([row["id"] for row in filtered.json()["data"]], [matching.id])

        cleared = client.get(f"/v1/procurement/requisitions/?entity={entity.code}")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(
            {row["id"] for row in cleared.json()["data"]}, {matching.id, other.id},
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_budget_availability_is_annual_and_counts_line_cost_centre(self, _permission):
        from django.utils import timezone
        from vs_finance.constants import BudgetStatus
        from vs_finance.models import Budget, BudgetLine, CostCenter

        entity, period, vendor, _, _ = self.build_p2p()
        dept = CostCenter.objects.create(entity=entity, code="IT", name="IT & Infrastructure")
        other_dept = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        expense = self.acc(entity, "5300")
        budget = Budget.objects.create(
            entity=entity, fiscal_year=period.fiscal_year, name="IT CAPEX 2026",
            status=BudgetStatus.APPROVED, approved_at=timezone.now(),
        )
        # Annual allocation is the sum across every period, not just the request month.
        for period_no, amount in ((1, 20_000_000), (2, 10_000_000)):
            BudgetLine.objects.create(
                budget=budget, account=expense, cost_center=dept,
                period_no=period_no, amount=amount,
            )

        # A directly-raised PO (no requisition) whose line is classified to IT - it
        # must still count, proving the commitment join uses the line's own centre.
        in_year = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2026, 3, 4),
            status=DocumentStatus.APPROVED,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=in_year, description="Servers", expense_account=expense,
            quantity=1, unit_price=12_000_000, net_amount=12_000_000, cost_center=dept, line_no=1,
        )
        # Noise that must be excluded: another department, and a prior-year PO.
        PurchaseOrderLine.objects.create(
            purchase_order=in_year, description="Chairs", expense_account=expense,
            quantity=1, unit_price=5_000_000, net_amount=5_000_000, cost_center=other_dept, line_no=2,
        )
        last_year = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2025, 12, 1),
            status=DocumentStatus.APPROVED,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=last_year, description="Prior year", expense_account=expense,
            quantity=1, unit_price=9_000_000, net_amount=9_000_000, cost_center=dept, line_no=1,
        )

        response = self.client_for(entity).get(
            f"/v1/procurement/requisitions/budget-availability/"
            f"?entity={entity.code}&cost_center={dept.code}&date=2026-01-15",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["has_budget"])
        self.assertEqual(data["period"], "IT CAPEX 2026")
        self.assertEqual(data["budget"], 30_000_000)
        self.assertEqual(data["committed"], 12_000_000)
        self.assertEqual(data["available"], 18_000_000)

    def test_summary_and_budget_endpoints_require_view_permission(self):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, _, _, _ = self.build_p2p()
        user = get_user_model().objects.create_user(
            email="req-no-grant@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="No", last_name="Grant",
        )
        client = TenantAPIClient(user=user)
        for path in (
            f"/v1/procurement/requisitions/summary/?entity={entity.code}",
            f"/v1/procurement/requisitions/budget-availability/?entity={entity.code}&cost_center=IT",
        ):
            self.assertEqual(client.get(path).status_code, 403)


# --------------------------------------------------------------------------- #
# Procurement analytics: spend, vendor performance, cycle time                #
# --------------------------------------------------------------------------- #

class ProcurementAnalyticsTests(_P2PFixtureMixin, TestCase):
    """Spend analysis, vendor performance and PR→payment cycle time."""

    def _full_chain(self, entity, vendor, *, qty=5, unit=200_000,
                    req=datetime.date(2026, 1, 2), order=datetime.date(2026, 1, 5),
                    expected=datetime.date(2026, 1, 9), received=datetime.date(2026, 1, 8),
                    invoiced=datetime.date(2026, 1, 10), paid=datetime.date(2026, 1, 25)):
        """Run requisition → PO → GRN → invoice → payment, returning the documents."""
        pr = PurchaseRequisition.objects.create(entity=entity, request_date=req)
        PurchaseRequisitionLine.objects.create(
            requisition=pr, description="chairs", quantity=qty,
            estimated_unit_price=unit, expense_account=self.acc(entity, "5100"), line_no=1,
        )
        submit_requisition(pr)
        approve_requisition(pr)
        po = create_po_from_requisition(
            pr, vendor=vendor, order_date=order, expected_date=expected,
        )
        approve_purchase_order(po)
        po.refresh_from_db()
        po_line = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(po_line, qty)])
        grn.received_date = received
        grn.save(update_fields=["received_date"])
        post_grn(grn)
        vi = self.make_bill(
            entity, vendor, [("5100", qty, unit, None, po_line)], po=po, date=invoiced,
        )
        post_vendor_invoice(vi)
        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=paid,
            gross_amount=qty * unit, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(pay)
        return pr, po, grn, vi, pay

    def test_spend_analysis_groups_by_vendor_and_category(self):
        entity, _, vendor, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(entity=entity, code="OFFICE", name="Office")
        vendor.category = category
        vendor.save(update_fields=["category"])
        self._full_chain(entity, vendor)

        report = spend_analysis(entity)
        self.assertEqual(report.total_gross, 1_000_000)
        self.assertEqual(report.invoice_count, 1)
        self.assertEqual(len(report.by_vendor), 1)
        self.assertEqual(report.by_vendor[0].key, "ACME")
        self.assertEqual(report.by_vendor[0].gross, 1_000_000)
        self.assertEqual(report.by_category[0].key, "OFFICE")
        self.assertEqual(report.by_category[0].gross, 1_000_000)

    def test_spend_window_excludes_out_of_range_invoices(self):
        entity, _, vendor, _, _ = self.build_p2p()
        self._full_chain(entity, vendor)  # invoiced 2026-01-10
        # A window after the invoice date sees no spend.
        report = spend_analysis(entity, start_date=datetime.date(2026, 2, 1))
        self.assertEqual(report.total_gross, 0)
        self.assertEqual(report.invoice_count, 0)

    def test_vendor_performance_blends_ordering_delivery_payment(self):
        entity, _, vendor, _, _ = self.build_p2p()
        self._full_chain(entity, vendor)  # received 01-08 vs expected 01-09 → on time

        report = vendor_performance(entity)
        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.po_count, 1)
        self.assertEqual(row.total_ordered, 1_000_000)
        self.assertEqual(row.receipt_count, 1)
        self.assertEqual(row.on_time_receipts, 1)
        self.assertEqual(row.late_receipts, 0)
        self.assertEqual(row.on_time_rate, 1.0)
        self.assertEqual(row.invoice_count, 1)
        self.assertEqual(row.total_billed, 1_000_000)
        self.assertEqual(row.payment_count, 1)
        self.assertEqual(row.total_paid, 1_000_000)
        self.assertEqual(row.avg_payment_days, 15.0)  # 01-10 → 01-25

    def test_vendor_performance_flags_late_delivery(self):
        entity, _, vendor, _, _ = self.build_p2p()
        # Receipt 01-12 against an expected date of 01-09 → late.
        self._full_chain(entity, vendor, received=datetime.date(2026, 1, 12))
        row = vendor_performance(entity).rows[0]
        self.assertEqual(row.on_time_receipts, 0)
        self.assertEqual(row.late_receipts, 1)
        self.assertEqual(row.on_time_rate, 0.0)

    def test_cycle_time_measures_each_hop(self):
        entity, _, vendor, _, _ = self.build_p2p()
        self._full_chain(entity, vendor)

        report = procurement_cycle_time(entity)
        stages = {s.name: s for s in report.stages}
        self.assertEqual(stages["req_to_po"].avg_days, 3.0)          # 01-02 → 01-05
        self.assertEqual(stages["po_to_receipt"].avg_days, 3.0)      # 01-05 → 01-08
        self.assertEqual(stages["receipt_to_invoice"].avg_days, 2.0)  # 01-08 → 01-10
        self.assertEqual(stages["invoice_to_payment"].avg_days, 15.0)  # 01-10 → 01-25
        self.assertEqual(report.end_to_end_avg_days, 23.0)           # 01-02 → 01-25
        self.assertEqual(report.end_to_end_count, 1)


class ProcurementAnalyticsReportAPITests(_P2PFixtureMixin, TestCase):
    """Security-first API coverage for the read-only analytics report endpoints
    (AP aging, spend analysis, vendor performance): permission gating, cross-entity
    isolation, the ``as_of`` regression fix, and the ``by_period`` trend series.
    """

    def _client(self, user):
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=user)

    def _user(self, entity, email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Rep", last_name="Ort",
        )

    def _second_entity(self):
        """A second fully-seeded entity (chart + open Jan period + vendor) for isolation."""
        entity = LedgerEntity.objects.create(
            name="Other Books", code="OBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        vendor = Vendor.objects.create(
            entity=entity, code="OTHERCO", name="Other Supplies",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        return entity, vendor

    def _posted_bill(self, entity, vendor, *, amount=1_000_000, date=datetime.date(2026, 1, 10)):
        vi = self.make_bill(entity, vendor, [("5300", 1, amount, None, None)], date=date)
        post_vendor_invoice(vi)
        return vi

    # --- permission gating (report.view) ---------------------------------- #

    def test_report_endpoints_require_report_permission(self):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(self._user(entity, "no-report-grant@test.com"))
        e = f"?entity={entity.code}"
        for url in (
            f"/v1/procurement/reports/ap-aging/{e}",
            f"/v1/procurement/reports/spend-analysis/{e}",
            f"/v1/procurement/reports/vendor-performance/{e}",
        ):
            self.assertEqual(client.get(url).status_code, 403, url)

    # --- as_of regression + aging shape ------------------------------------ #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_aging_accepts_as_of_query_param(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)  # due 2026-01-10, 1,000,000
        client = self._client(self._user(entity, "ap-aging@test.com"))
        resp = client.get(
            f"/v1/procurement/reports/ap-aging/?entity={entity.code}&as_of=2026-02-15")
        # Regression: a raw string as_of used to reach ``as_of - due_date`` and 500.
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["as_of"], "2026-02-15")
        self.assertEqual(data["total_net"]["kobo"], 1_000_000)
        # 36 days overdue on 2026-02-15 → "31-60" bucket.
        self.assertEqual(data["bucket_totals"]["31-60"]["kobo"], 1_000_000)
        self.assertEqual(data["rows"][0]["code"], "ACME")

    # --- cross-entity isolation -------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_aging_is_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)
        other, other_vendor = self._second_entity()
        self._posted_bill(other, other_vendor)  # a foreign entity's bill
        client = self._client(self._user(entity, "ap-scope@test.com"))
        resp = client.get(f"/v1/procurement/reports/ap-aging/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        codes = {r["code"] for r in resp.data["data"]["rows"]}
        self.assertEqual(codes, {"ACME"})  # never the foreign OTHERCO

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_spend_and_vendor_performance_are_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)
        other, other_vendor = self._second_entity()
        self._posted_bill(other, other_vendor)
        # Service-level isolation of both reports.
        self.assertEqual({r.key for r in spend_analysis(entity).by_vendor}, {"ACME"})
        self.assertEqual({r.code for r in vendor_performance(entity).rows}, {"ACME"})
        # API-level: vendor-performance for A never lists the foreign vendor.
        client = self._client(self._user(entity, "vp-scope@test.com"))
        resp = client.get(f"/v1/procurement/reports/vendor-performance/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({r["code"] for r in resp.data["data"]["rows"]}, {"ACME"})

    # --- spend by_period trend --------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_spend_analysis_by_period_shape_and_monthly_gross(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor, amount=1_000_000, date=datetime.date(2026, 1, 10))
        client = self._client(self._user(entity, "spend@test.com"))
        resp = client.get(f"/v1/procurement/reports/spend-analysis/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["total_gross"]["kobo"], 1_000_000)
        self.assertEqual(len(data["by_period"]), 1)
        period = data["by_period"][0]
        self.assertEqual(period["period"], "2026-01")
        self.assertEqual(period["label"], "Jan 2026")
        self.assertEqual(period["gross"]["kobo"], 1_000_000)
        self.assertEqual(period["invoice_count"], 1)

    def test_spend_by_period_orders_months_chronologically(self):
        entity, _, vendor, _, _ = self.build_p2p()
        # Open a second (Feb) period so a February bill can post.
        year = FiscalYear.objects.get(entity=entity, year=2026)
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=2, name="Feb 2026",
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),
        )
        self._posted_bill(entity, vendor, amount=1_000_000, date=datetime.date(2026, 2, 5))
        self._posted_bill(entity, vendor, amount=500_000, date=datetime.date(2026, 1, 20))
        report = spend_analysis(entity)
        # Ascending by month regardless of insertion order.
        self.assertEqual([p.period for p in report.by_period], ["2026-01", "2026-02"])
        self.assertEqual(report.by_period[0].gross, 500_000)
        self.assertEqual(report.by_period[1].gross, 1_000_000)

    # --- empty-entity shapes ----------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_empty_entity_report_shapes_do_not_error(self, _perm):
        entity, _, _, _, _ = self.build_p2p()  # no posted documents at all
        client = self._client(self._user(entity, "empty@test.com"))
        e = f"?entity={entity.code}"
        aging = client.get(f"/v1/procurement/reports/ap-aging/{e}")
        self.assertEqual(aging.status_code, 200)
        self.assertEqual(aging.data["data"]["rows"], [])
        self.assertEqual(aging.data["data"]["total_net"]["kobo"], 0)
        spend = client.get(f"/v1/procurement/reports/spend-analysis/{e}")
        self.assertEqual(spend.status_code, 200)
        self.assertEqual(spend.data["data"]["by_period"], [])
        self.assertEqual(spend.data["data"]["by_vendor"], [])
        self.assertEqual(spend.data["data"]["invoice_count"], 0)
        vp = client.get(f"/v1/procurement/reports/vendor-performance/{e}")
        self.assertEqual(vp.status_code, 200)
        self.assertEqual(vp.data["data"]["rows"], [])


class VendorAssessmentTests(_P2PFixtureMixin, TestCase):
    """VendorAssessment scorecards: weighted score/grade, create gating, entity
    isolation, score validation, and latest-per-vendor feeding vendor_performance.
    """

    def _client(self, user):
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=user)

    def _user(self, entity, email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Assess", last_name="Or",
        )

    def _second_entity(self):
        entity = LedgerEntity.objects.create(
            name="Other Books", code="OBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        vendor = Vendor.objects.create(
            entity=entity, code="OTHERCO", name="Other Supplies",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        return entity, vendor

    def _payload(self, vendor, **over):
        data = {
            "vendor": vendor.code,
            "on_time_delivery": 85, "quality_acceptance": 90,
            "invoice_accuracy": 85, "responsiveness": 80,
            "notes": "Solid quarter.",
        }
        data.update(over)
        return data

    # --- computed score + grade -------------------------------------------- #

    def test_weighted_score_and_grade_bands(self):
        entity, _, vendor, _, _ = self.build_p2p()

        def score(otd, q, ia, r):
            a = VendorAssessment(
                entity=entity, vendor=vendor, on_time_delivery=otd,
                quality_acceptance=q, invoice_accuracy=ia, responsiveness=r,
            )
            return a.overall_score, a.grade

        self.assertEqual(score(85, 90, 85, 80), (86, "B"))   # 85.75 → 86 → B
        self.assertEqual(score(94, 97, 89, 82), (92, "A"))   # 92.1  → 92 → A
        self.assertEqual(score(75, 75, 75, 75), (75, "C"))   # 75    → C
        self.assertEqual(score(90, 90, 90, 90), (90, "A"))   # boundary A
        self.assertEqual(score(76, 76, 76, 76), (76, "B"))   # boundary B

    # --- create gating ----------------------------------------------------- #

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_create_needs_assessment_key_but_list_rides_report_view(self, mock_has, _super):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(self._user(entity, "assess-gate@test.com"))
        e = f"?entity={entity.code}"
        # Without the create key, POST is denied but GET (report.view) still works.
        mock_has.side_effect = _deny_keys("procurement.vendor_assessment.create")
        denied = client.post(f"/v1/procurement/vendor-assessments/{e}", self._payload(vendor), format="json")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(client.get(f"/v1/procurement/vendor-assessments/{e}").status_code, 200)
        # Deny report.view instead → listing is 403.
        mock_has.side_effect = _deny_keys("procurement.report.view")
        self.assertEqual(client.get(f"/v1/procurement/vendor-assessments/{e}").status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_succeeds_and_computes_scorecard(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        user = self._user(entity, "assess-ok@test.com")
        resp = self._client(user).post(
            f"/v1/procurement/vendor-assessments/?entity={entity.code}",
            self._payload(vendor), format="json")
        self.assertEqual(resp.status_code, 201)
        data = resp.data["data"]
        self.assertEqual(data["overall_score"], 86)
        self.assertEqual(data["grade"], "B")
        self.assertEqual(data["vendor_code"], "ACME")
        self.assertTrue(data["assessor"])
        row = VendorAssessment.objects.get(entity=entity, vendor=vendor)
        self.assertEqual(row.assessor_id, user.id)  # assessor is the caller

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_out_of_range_score(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        resp = self._client(self._user(entity, "assess-bad@test.com")).post(
            f"/v1/procurement/vendor-assessments/?entity={entity.code}",
            self._payload(vendor, on_time_delivery=150), format="json")
        self.assertEqual(resp.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_cannot_assess_another_entitys_vendor(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        _other, other_vendor = self._second_entity()
        resp = self._client(self._user(entity, "assess-x@test.com")).post(
            f"/v1/procurement/vendor-assessments/?entity={entity.code}",
            self._payload(other_vendor), format="json")
        self.assertEqual(resp.status_code, 400)  # foreign vendor unresolvable in this entity

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_list_is_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other, other_vendor = self._second_entity()
        VendorAssessment.objects.create(
            entity=entity, vendor=vendor,
            on_time_delivery=80, quality_acceptance=80, invoice_accuracy=80, responsiveness=80)
        VendorAssessment.objects.create(
            entity=other, vendor=other_vendor,
            on_time_delivery=60, quality_acceptance=60, invoice_accuracy=60, responsiveness=60)
        resp = self._client(self._user(entity, "assess-list@test.com")).get(
            f"/v1/procurement/vendor-assessments/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({a["vendor_code"] for a in resp.data["data"]}, {"ACME"})

    # --- feeds the performance report -------------------------------------- #

    def test_latest_assessment_feeds_vendor_performance(self):
        entity, _, vendor, _, _ = self.build_p2p()
        VendorAssessment.objects.create(
            entity=entity, vendor=vendor, assessment_date=datetime.date(2026, 1, 1),
            on_time_delivery=50, quality_acceptance=50, invoice_accuracy=50, responsiveness=50)
        VendorAssessment.objects.create(
            entity=entity, vendor=vendor, assessment_date=datetime.date(2026, 3, 1),
            on_time_delivery=94, quality_acceptance=97, invoice_accuracy=89, responsiveness=82)
        vi = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, None, None)])
        post_vendor_invoice(vi)  # give the vendor activity so it appears in the report
        report = vendor_performance(entity)
        row = next(r for r in report.rows if r.vendor_id == vendor.id)
        self.assertIsNotNone(row.latest_assessment)
        self.assertEqual(row.latest_assessment.assessment_date, datetime.date(2026, 3, 1))  # newest wins
        self.assertEqual(row.latest_assessment.overall_score, 92)
        self.assertEqual(row.latest_assessment.grade, "A")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_vendor_performance_serializes_latest_assessment(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, None, None)])
        post_vendor_invoice(vi)
        VendorAssessment.objects.create(
            entity=entity, vendor=vendor,
            on_time_delivery=94, quality_acceptance=97, invoice_accuracy=89, responsiveness=82)
        resp = self._client(self._user(entity, "vp-assess@test.com")).get(
            f"/v1/procurement/reports/vendor-performance/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.data["data"]["rows"] if r["code"] == "ACME")
        self.assertEqual(row["latest_assessment"]["grade"], "A")
        self.assertEqual(row["latest_assessment"]["overall_score"], 92)
        self.assertEqual(row["latest_assessment"]["quality_acceptance"], 97)
        # on_time_rate stays the COMPUTED value (no rated receipts → None), never overwritten.
        self.assertIsNone(row["on_time_rate"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_vendor_performance_null_assessment_when_unrated(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(entity, vendor, [("5300", 1, 1_000_000, None, None)])
        post_vendor_invoice(vi)
        resp = self._client(self._user(entity, "vp-noassess@test.com")).get(
            f"/v1/procurement/reports/vendor-performance/?entity={entity.code}")
        row = next(r for r in resp.data["data"]["rows"] if r["code"] == "ACME")
        self.assertIsNone(row["latest_assessment"])


class AnalyticsDrawerEndpointTests(_P2PFixtureMixin, TestCase):
    """Report.view-gated drawer detail endpoints: AP per-vendor open bills, GR/IR
    per-GRN links, and the spend per-category scope. Security + real data.
    """

    def _client(self, user):
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=user)

    def _user(self, entity, email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Drawer", last_name="Viewer",
        )

    def _second_entity(self):
        entity = LedgerEntity.objects.create(
            name="Other Books", code="OBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        vendor = Vendor.objects.create(
            entity=entity, code="OTHERCO", name="Other Supplies",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        return entity, vendor

    def _posted_bill(self, entity, vendor, *, amount=1_000_000, date=datetime.date(2026, 1, 10)):
        vi = self.make_bill(entity, vendor, [("5300", 1, amount, None, None)], date=date)
        post_vendor_invoice(vi)
        return vi

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_drawer_endpoints_require_report_view(self, mock_has, _super):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(self._user(entity, "drawer-gate@test.com"))
        mock_has.side_effect = _deny_keys("procurement.report.view")
        e = f"?entity={entity.code}"
        for url in (
            f"/v1/procurement/reports/ap-aging/vendor/{e}&vendor={vendor.code}",
            f"/v1/procurement/reports/grir-aging/grn/{e}&grn=1",
        ):
            self.assertEqual(client.get(url).status_code, 403, url)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_vendor_open_bills_detail(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        self._posted_bill(entity, vendor)  # due 2026-01-10, 1,000,000
        resp = self._client(self._user(entity, "ap-vendor@test.com")).get(
            f"/v1/procurement/reports/ap-aging/vendor/?entity={entity.code}"
            f"&vendor={vendor.code}&as_of=2026-02-15")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["vendor"]["code"], "ACME")
        self.assertEqual(data["net"]["kobo"], 1_000_000)
        self.assertEqual(len(data["invoices"]), 1)
        inv = data["invoices"][0]
        self.assertEqual(inv["balance_due"]["kobo"], 1_000_000)
        self.assertEqual(inv["bucket"], "31-60")  # 36 days overdue on 2026-02-15
        self.assertEqual(inv["payment_status"], "UNPAID")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_vendor_detail_is_entity_scoped(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        _other, other_vendor = self._second_entity()
        resp = self._client(self._user(entity, "ap-vendor-x@test.com")).get(
            f"/v1/procurement/reports/ap-aging/vendor/?entity={entity.code}&vendor={other_vendor.code}")
        self.assertEqual(resp.status_code, 400)  # foreign vendor unresolvable here

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_grn_detail_links_po_and_reconciles(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 10)])
        post_grn(grn)  # GR/IR credit 1,000,000, still open
        po.refresh_from_db()
        resp = self._client(self._user(entity, "grir-grn@test.com")).get(
            f"/v1/procurement/reports/grir-aging/grn/?entity={entity.code}"
            f"&grn={grn.id}&as_of=2026-01-10")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["received_value"]["kobo"], 1_000_000)
        self.assertEqual(data["invoiced_value"]["kobo"], 0)
        self.assertEqual(data["open_value"]["kobo"], 1_000_000)
        self.assertEqual(data["po_number"], po.document_number or None)
        self.assertEqual(data["invoices"], [])
        # A foreign entity's GRN id is a 404, not a cross-entity read.
        other, _ = self._second_entity()
        cross = self._client(self._user(other, "grir-x@test.com")).get(
            f"/v1/procurement/reports/grir-aging/grn/?entity={other.code}&grn={grn.id}")
        self.assertEqual(cross.status_code, 404)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_grn_detail_shows_matched_invoice(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        post_grn(grn)
        grn_line = grn.lines.first()
        vi = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 10),
            approval_state=ProcApprovalState.APPROVED)
        VendorInvoiceLine.objects.create(
            vendor_invoice=vi, po_line=po_line, grn_line=grn_line,
            expense_account=self.acc(entity, "5100"), quantity=10, unit_price=100_000, line_no=1)
        post_vendor_invoice(vi)  # clears GR/IR
        resp = self._client(self._user(entity, "grir-matched@test.com")).get(
            f"/v1/procurement/reports/grir-aging/grn/?entity={entity.code}"
            f"&grn={grn.id}&as_of=2026-01-12")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["open_value"]["kobo"], 0)
        self.assertEqual(len(data["invoices"]), 1)
        self.assertEqual(data["invoices"][0]["net"]["kobo"], 1_000_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_spend_category_scope_filters_to_that_category(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        office = VendorCategory.objects.create(entity=entity, code="OFFICE", name="Office")
        itx = VendorCategory.objects.create(entity=entity, code="ITX", name="IT")
        vendor.category = office
        vendor.save(update_fields=["category"])
        v2 = Vendor.objects.create(
            entity=entity, code="TECHCO", name="Tech Co",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            category=itx, kyc_status="VERIFIED")
        self._posted_bill(entity, vendor, amount=1_000_000)
        self._posted_bill(entity, v2, amount=400_000)
        resp = self._client(self._user(entity, "spend-cat@test.com")).get(
            f"/v1/procurement/reports/spend-analysis/?entity={entity.code}&category=OFFICE")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["category"], "OFFICE")
        self.assertEqual(data["total_gross"]["kobo"], 1_000_000)  # only OFFICE vendor's bill
        self.assertEqual({r["key"] for r in data["by_vendor"]}, {"ACME"})


class GRIRPoLinesTests(_P2PFixtureMixin, TestCase):
    """PO-line-grain GR/IR report + its per-line drawer. Security + aggregation +
    status derivation over real POSTED goods receipts and vendor invoices.
    """

    def _client(self, user):
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=user)

    def _user(self, entity, email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="GR", last_name="Lines",
        )

    def _other_entity(self):
        entity = LedgerEntity.objects.create(
            name="Other GRIR", code="OGRIR", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        FiscalPeriod.objects.create(
            entity=entity,
            fiscal_year=FiscalYear.objects.create(
                entity=entity, year=2026,
                start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
            ),
            period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        vendor = Vendor.objects.create(
            entity=entity, code="OTHERCO", name="Other Supplies",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        return entity, vendor

    def _post_bill(self, entity, vendor, po, po_line, qty, *, grn_line=None, allow_variance=False):
        vi = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 10),
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=vi, po_line=po_line, grn_line=grn_line,
            expense_account=po_line.expense_account, quantity=qty,
            unit_price=po_line.unit_price, line_no=1,
        )
        post_vendor_invoice(vi, allow_variance=allow_variance)
        return vi

    def _cleared_line(self, entity, vendor):
        # Ordered 10, received 10, invoiced 10 → Cleared, balance 0.
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        pl = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(pl, 10)])
        post_grn(grn)
        pl.refresh_from_db()
        self._post_bill(entity, vendor, po, pl, 10, grn_line=grn.lines.first())
        return po, pl

    def _received_gt_invoiced_line(self, entity, vendor):
        # Ordered 10, received 10, no invoice → Received > Invoiced, balance +1,000,000.
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        pl = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(pl, 10)])
        post_grn(grn)
        return po, pl

    def _invoiced_gt_received_line(self, entity, vendor):
        # Ordered 10, received 5, invoiced 10 → Invoiced > Received, balance -500,000.
        # Billing ahead of receipt is an under-received variance, posted with override.
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        pl = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(pl, 5)])
        post_grn(grn)
        pl.refresh_from_db()
        self._post_bill(entity, vendor, po, pl, 10, grn_line=grn.lines.first(), allow_variance=True)
        return po, pl

    def _rows_by_line(self, data):
        return {r["po_line_id"]: r for r in data["rows"]}

    def test_linked_favorable_and_unfavorable_ppv_clear_every_grir_report(self):
        from vs_procurement.reports import (
            grir_aging,
            grir_grn_detail,
            grir_po_line_detail,
            grir_po_lines,
        )

        entity, _, vendor, _, _ = self.build_p2p()
        documents = []
        for actual_price in (120_000, 80_000):
            po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
            po_line = po.lines.get()
            grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
            # The linked receipt snapshot is deliberately different from the PO:
            # every report must use 1.1m, exactly like invoice posting.
            grn.lines.update(unit_price=110_000)
            grn_line = grn.lines.get()
            post_grn(grn)
            invoice = VendorInvoice.objects.create(
                entity=entity, vendor=vendor, purchase_order=po,
                invoice_date=datetime.date(2026, 1, 10),
                approval_state=ProcApprovalState.APPROVED,
            )
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, po_line=po_line, grn_line=grn_line,
                expense_account=po_line.expense_account, quantity=10,
                unit_price=actual_price, line_no=1,
            )
            post_vendor_invoice(invoice)
            documents.append((po_line, grn))

        aging = grir_aging(entity, as_of=datetime.date(2026, 1, 12))
        self.assertEqual(aging.rows, [])
        self.assertEqual(aging.total_open, 0)
        self.assertEqual(aging.control_balance, 0)
        self.assertEqual(aging.difference, 0)

        line_rows = {row.po_line_id: row for row in grir_po_lines(entity).rows}
        for po_line, grn in documents:
            with self.subTest(po_line=po_line.id):
                grn_detail = grir_grn_detail(entity, grn.id)
                self.assertEqual(grn_detail.received_value, 1_100_000)
                self.assertEqual(grn_detail.invoiced_value, 1_100_000)
                self.assertEqual(grn_detail.open_value, 0)
                self.assertEqual(grn_detail.invoices[0]["net"], 1_100_000)

                row = line_rows[po_line.id]
                self.assertEqual(row.received_value, 1_100_000)
                self.assertEqual(row.invoiced_value, 1_100_000)
                self.assertEqual(row.grir_balance, 0)
                self.assertEqual(row.status, "Cleared")

                line_detail = grir_po_line_detail(entity, po_line.id)
                self.assertEqual(line_detail.received_value, 1_100_000)
                self.assertEqual(line_detail.invoiced_value, 1_100_000)
                self.assertEqual(line_detail.grir_balance, 0)
                self.assertEqual(line_detail.status, "Cleared")
                self.assertEqual(line_detail.invoices[0]["net"], 1_100_000)

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_grir_lines_require_report_view(self, mock_has, _super):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(self._user(entity, "grir-lines-gate@test.com"))
        mock_has.side_effect = _deny_keys("procurement.report.view")
        e = f"?entity={entity.code}"
        for url in (
            f"/v1/procurement/reports/grir-lines/{e}",
            f"/v1/procurement/reports/grir-lines/detail/{e}&po_line=1",
        ):
            self.assertEqual(client.get(url).status_code, 403, url)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_lines_aggregation_and_status(self, _perm):
        from decimal import Decimal

        entity, _, vendor, _, _ = self.build_p2p()
        _, cleared = self._cleared_line(entity, vendor)
        _, recv_gt = self._received_gt_invoiced_line(entity, vendor)
        _, inv_gt = self._invoiced_gt_received_line(entity, vendor)

        resp = self._client(self._user(entity, "grir-lines-agg@test.com")).get(
            f"/v1/procurement/reports/grir-lines/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        rows = self._rows_by_line(resp.data["data"])
        # All three lines have activity and appear.
        self.assertEqual(set(rows), {cleared.id, recv_gt.id, inv_gt.id})

        c = rows[cleared.id]
        self.assertEqual(Decimal(c["ordered_qty"]), 10)
        self.assertEqual(Decimal(c["received_qty"]), 10)
        self.assertEqual(Decimal(c["invoiced_qty"]), 10)
        self.assertEqual(c["received_value"]["kobo"], 1_000_000)
        self.assertEqual(c["invoiced_value"]["kobo"], 1_000_000)
        self.assertEqual(c["grir_balance"]["kobo"], 0)
        self.assertEqual(c["status"], "Cleared")

        r = rows[recv_gt.id]
        self.assertEqual(Decimal(r["received_qty"]), 10)
        self.assertEqual(Decimal(r["invoiced_qty"]), 0)
        self.assertEqual(r["grir_balance"]["kobo"], 1_000_000)
        self.assertEqual(r["status"], "Received > Invoiced")

        i = rows[inv_gt.id]
        self.assertEqual(Decimal(i["received_qty"]), 5)
        self.assertEqual(Decimal(i["invoiced_qty"]), 10)
        self.assertEqual(i["received_value"]["kobo"], 500_000)
        self.assertEqual(i["invoiced_value"]["kobo"], 1_000_000)
        self.assertEqual(i["grir_balance"]["kobo"], -500_000)
        self.assertEqual(i["status"], "Invoiced > Received")
        # PO-line ref is "<PO document_number>-<line_no>".
        self.assertTrue(i["po_line_ref"].endswith("-1"))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_lines_excludes_inactive_lines_and_cancelled_pos(self, _perm):
        from vs_finance.constants import DocumentStatus

        entity, _, vendor, _, _ = self.build_p2p()
        # A PO line with no receipt and no invoice must not appear (no activity).
        self.make_po(entity, vendor, [("5100", 4, 50_000, None)])
        # A cancelled PO's received line is excluded even though it has receipt activity.
        po = self.make_po(entity, vendor, [("5100", 3, 100_000, None)])
        pl = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(pl, 3)])
        post_grn(grn)
        po.status = DocumentStatus.CANCELLED
        po.save(update_fields=["status"])

        resp = self._client(self._user(entity, "grir-lines-excl@test.com")).get(
            f"/v1/procurement/reports/grir-lines/?entity={entity.code}")
        self.assertEqual(resp.data["data"]["rows"], [])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_lines_empty_entity_shape(self, _perm):
        entity, _, _, _, _ = self.build_p2p()  # a vendor exists but no PO activity
        resp = self._client(self._user(entity, "grir-lines-empty@test.com")).get(
            f"/v1/procurement/reports/grir-lines/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"]["rows"], [])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_line_detail_links_documents(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        po, pl = self._cleared_line(entity, vendor)
        resp = self._client(self._user(entity, "grir-line-detail@test.com")).get(
            f"/v1/procurement/reports/grir-lines/detail/?entity={entity.code}&po_line={pl.id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["po_line_id"], pl.id)
        self.assertEqual(data["status"], "Cleared")
        self.assertEqual(data["received_value"]["kobo"], 1_000_000)
        self.assertEqual(data["invoiced_value"]["kobo"], 1_000_000)
        self.assertEqual(data["po_number"], po.document_number)
        self.assertEqual(len(data["grns"]), 1)
        self.assertEqual(data["grns"][0]["value"]["kobo"], 1_000_000)
        self.assertEqual(len(data["invoices"]), 1)
        self.assertEqual(data["invoices"][0]["net"]["kobo"], 1_000_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_lines_are_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        _, pl = self._cleared_line(entity, vendor)
        other, other_vendor = self._other_entity()
        # Other entity's report never contains this entity's PO line.
        listing = self._client(self._user(other, "grir-x-list@test.com")).get(
            f"/v1/procurement/reports/grir-lines/?entity={other.code}")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["data"]["rows"], [])
        # A foreign PO-line id is a 404 from the other entity, not a cross-entity read.
        cross = self._client(self._user(other, "grir-x-detail@test.com")).get(
            f"/v1/procurement/reports/grir-lines/detail/?entity={other.code}&po_line={pl.id}")
        self.assertEqual(cross.status_code, 404)


# --------------------------------------------------------------------------- #
# Sourcing: RFQ → quotations → award → PO                                     #
# --------------------------------------------------------------------------- #

class SourcingTests(_P2PFixtureMixin, TestCase):
    """RFQ lifecycle, quotation submission and award-into-PO conversion."""

    def _make_rfq(self, entity, *, lines=None, invite=None):
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Office chairs",
            issue_date=datetime.date(2026, 1, 3),
        )
        for i, (desc, qty) in enumerate(lines or [("Mesh chair", 10)], start=1):
            RfqLine.objects.create(
                rfq=rfq, description=desc, quantity=qty, line_no=i,
                expense_account=self.acc(entity, "5300"),
            )
        # Default: invite every purchase-eligible vendor in the entity so the RFQ can be
        # issued (issue now requires ≥1 invitation) and any of them may quote. Pass an
        # explicit ``invite=[]`` to exercise the no-invitation path.
        if invite is None:
            invite = list(
                Vendor.objects.filter(entity=entity, is_active=True, on_hold=False)
                .exclude(kyc_status=VendorKycStatus.REJECTED)
            )
        if invite:
            set_rfq_invitations(rfq, invite)
        return rfq

    def _make_quotation(self, entity, rfq, vendor, *, lines):
        """lines: [(description, qty, unit_price_kobo)]."""
        quo = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=datetime.date(2026, 1, 4),
        )
        rfq_lines = list(rfq.lines.all())
        for i, (desc, qty, price) in enumerate(lines, start=1):
            VendorQuotationLine.objects.create(
                quotation=quo, description=desc, quantity=qty, unit_price=price,
                line_no=i, expense_account=self.acc(entity, "5300"),
                rfq_line=rfq_lines[i - 1] if i - 1 < len(rfq_lines) else None,
            )
        return quo

    def test_issue_requires_lines_and_flips_status(self):
        entity, _, _, _, _ = self.build_p2p()
        empty = RequestForQuotation.objects.create(
            entity=entity, title="Empty", issue_date=datetime.date(2026, 1, 3),
        )
        with self.assertRaises(SourcingError):
            issue_rfq(empty)
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        rfq.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.ISSUED)
        self.assertTrue(rfq.document_number)

    def test_issue_enforces_competitive_minimum_and_audits_exception(self):
        entity, _, vendor, _, _ = self.build_p2p()
        ProcurementSettings.objects.create(
            entity=entity, minimum_rfq_invited_vendors=2,
        )
        rfq = self._make_rfq(entity, invite=[vendor])

        with self.assertRaises(SourcingError):
            issue_rfq(rfq)
        rfq.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.DRAFT)

        issue_rfq(rfq, competition_exception_reason="Only qualified local supplier")
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.RFQ_ISSUED,
        )
        self.assertTrue(audit.metadata["competition_exception"])
        self.assertEqual(audit.metadata["invited_vendor_count"], 1)
        self.assertEqual(audit.metadata["minimum_invited_vendors"], 2)
        self.assertEqual(
            audit.metadata["competition_exception_reason"],
            "Only qualified local supplier",
        )

    def test_quotation_only_submits_against_issued_rfq(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        # RFQ still DRAFT - cannot submit.
        with self.assertRaises(SourcingError):
            submit_quotation(quo)
        issue_rfq(rfq)
        submit_quotation(quo)
        quo.refresh_from_db()
        self.assertEqual(quo.quotation_status, QuotationStatus.SUBMITTED)
        self.assertEqual(quo.subtotal, 2_000_000)
        self.assertEqual(quo.total, 2_000_000)
        message = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.QUOTATION_SUBMITTED,
        ).message
        self.assertIn("₦", message)
        self.assertNotIn("kobo", message.lower())

    def test_award_builds_po_and_rejects_losers(self):
        entity, _, vendor, _, _ = self.build_p2p()
        loser = Vendor.objects.create(
            entity=entity, code="RIVAL", name="Rival Co",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
        )
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        winning = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        losing = self._make_quotation(entity, rfq, loser, lines=[("Mesh chair", 10, 250_000)])
        submit_quotation(winning)
        submit_quotation(losing)

        po = award_quotation(winning)

        self.assertEqual(po.status, DocumentStatus.DRAFT)
        self.assertEqual(po.vendor_id, vendor.pk)
        self.assertEqual(po.total, 2_000_000)
        self.assertEqual(po.lines.count(), 1)
        line = po.lines.first()
        self.assertEqual(line.unit_price, 200_000)
        self.assertEqual(line.expense_account, self.acc(entity, "5300"))
        self.assertIsNone(line.cost_center_id)

        winning.refresh_from_db()
        losing.refresh_from_db()
        rfq.refresh_from_db()
        self.assertEqual(winning.quotation_status, QuotationStatus.AWARDED)
        self.assertEqual(winning.awarded_po_id, po.pk)
        self.assertEqual(losing.quotation_status, QuotationStatus.REJECTED)
        self.assertEqual(rfq.rfq_status, RfqStatus.AWARDED)
        message = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.QUOTATION_AWARDED,
        ).message
        self.assertIn("₦", message)
        self.assertNotIn("kobo", message.lower())

    def test_award_enforces_submitted_bid_minimum_and_audits_exception(self):
        entity, _, vendor, _, _ = self.build_p2p()
        ProcurementSettings.objects.create(
            entity=entity, minimum_submitted_quotations_before_award=2,
        )
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        winning = self._make_quotation(
            entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)],
        )
        submit_quotation(winning)

        with self.assertRaises(SourcingError):
            award_quotation(winning)
        winning.refresh_from_db()
        self.assertEqual(winning.quotation_status, QuotationStatus.SUBMITTED)

        award_quotation(
            winning, competition_exception_reason="Emergency replacement purchase",
        )
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.QUOTATION_AWARDED,
        )
        self.assertTrue(audit.metadata["competition_exception"])
        self.assertEqual(audit.metadata["submitted_quotation_count"], 1)
        self.assertEqual(audit.metadata["minimum_submitted_quotations"], 2)
        self.assertEqual(
            audit.metadata["competition_exception_reason"],
            "Emergency replacement purchase",
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_award_carries_lineage_cost_centers_into_budget_commitments(self, _permission):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient
        from vs_finance.constants import BudgetStatus
        from vs_finance.models import Budget, BudgetLine, CostCenter

        entity, period, vendor, _, _ = self.build_p2p()
        line_center = CostCenter.objects.create(
            entity=entity, code="LINE", name="Line owner",
        )
        header_center = CostCenter.objects.create(
            entity=entity, code="HEAD", name="RFQ owner",
        )
        origin = PurchaseRequisition.objects.create(
            entity=entity, title="Origin", request_date=datetime.date(2026, 1, 2),
            cost_center=line_center,
        )
        origin_line = PurchaseRequisitionLine.objects.create(
            requisition=origin, description="Line-owned item", quantity=2,
            estimated_unit_price=200_000, expense_account=self.acc(entity, "5300"), line_no=1,
        )
        header = PurchaseRequisition.objects.create(
            entity=entity, title="RFQ header", request_date=datetime.date(2026, 1, 2),
            cost_center=header_center,
        )
        rfq = RequestForQuotation.objects.create(
            entity=entity, requisition=header, title="Mixed ownership",
            issue_date=datetime.date(2026, 1, 3),
        )
        RfqLine.objects.create(
            rfq=rfq, requisition_line=origin_line, description="Line-owned item",
            quantity=2, expense_account=self.acc(entity, "5300"), line_no=1,
        )
        RfqLine.objects.create(
            rfq=rfq, description="Header-owned item", quantity=3,
            expense_account=self.acc(entity, "5300"), line_no=2,
        )
        set_rfq_invitations(rfq, [vendor])
        issue_rfq(rfq)
        quotation = self._make_quotation(
            entity, rfq, vendor,
            lines=[("Line-owned item", 2, 200_000), ("Header-owned item", 3, 300_000)],
        )
        submit_quotation(quotation)

        po = award_quotation(quotation, order_date=datetime.date(2026, 1, 10))
        po_lines = list(po.lines.order_by("line_no"))
        self.assertEqual(po.status, DocumentStatus.DRAFT)
        self.assertEqual(po_lines[0].requisition_line_id, origin_line.pk)
        self.assertEqual(po_lines[0].cost_center_id, line_center.pk)
        self.assertEqual(po_lines[1].cost_center_id, header_center.pk)

        # Once the awarded draft enters a commitment status, the existing budget view
        # sees the line-origin cost centre without any special sourced-PO fallback.
        PurchaseOrder.objects.filter(pk=po.pk).update(status=DocumentStatus.PENDING_APPROVAL)
        budget = Budget.objects.create(
            entity=entity, fiscal_year=period.fiscal_year, name="Line owner 2026",
            status=BudgetStatus.APPROVED, approved_at=timezone.now(),
        )
        BudgetLine.objects.create(
            budget=budget, account=self.acc(entity, "5300"), cost_center=line_center,
            period_no=1, amount=2_000_000,
        )
        user = get_user_model().objects.create_user(
            email="sourced-budget@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Budget", last_name="Tester",
        )
        response = TenantAPIClient(user=user).get(
            f"/v1/procurement/requisitions/budget-availability/"
            f"?entity={entity.code}&cost_center={line_center.code}&date=2026-01-15",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["committed"], 400_000)
        self.assertEqual(response.data["data"]["available"], 1_600_000)

    def test_cannot_award_unsubmitted_quotation(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        with self.assertRaises(SourcingError):  # still DRAFT
            award_quotation(quo)

    def test_awarded_rfq_cannot_be_cancelled(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        submit_quotation(quo)
        award_quotation(quo)
        with self.assertRaises(SourcingError):
            cancel_rfq(rfq)

    def test_close_only_issued_and_rejects_live_quotes(self):
        entity, _, vendor, _, _ = self.build_p2p()
        draft = self._make_rfq(entity)
        with self.assertRaises(SourcingError):  # a draft was never open
            close_rfq(draft)
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        submit_quotation(quo)
        close_rfq(rfq, reason="No suitable bid")
        rfq.refresh_from_db()
        quo.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.CLOSED)
        self.assertEqual(quo.quotation_status, QuotationStatus.REJECTED)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.RFQ_CLOSED).exists())
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.QUOTATION_REJECTED,
            target_type="VendorQuotation", target_id=str(quo.pk)).exists())

    def test_cancel_issued_rfq_rejects_live_quotes(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        submit_quotation(quo)
        cancel_rfq(rfq, reason="Withdrawn")
        rfq.refresh_from_db()
        quo.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.CANCELLED)
        self.assertEqual(quo.quotation_status, QuotationStatus.REJECTED)

    def test_submit_blocks_ineligible_vendor(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        vendor.on_hold = True
        vendor.save(update_fields=["on_hold", "updated_at"])
        with self.assertRaises(SourcingError):
            submit_quotation(quo)

    def test_award_rejects_lapsed_quotation(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        submit_quotation(quo)
        # A validity date in the past makes the offer stale - award must refuse it.
        VendorQuotation.objects.filter(pk=quo.pk).update(
            valid_until=datetime.date.today() - datetime.timedelta(days=1),
        )
        quo.refresh_from_db()
        with self.assertRaises(SourcingError):
            award_quotation(quo)
        quo.refresh_from_db()
        rfq.refresh_from_db()
        self.assertEqual(quo.quotation_status, QuotationStatus.SUBMITTED)
        self.assertEqual(rfq.rfq_status, RfqStatus.ISSUED)

    # --- invited vendors --------------------------------------------------- #

    def test_set_invitations_validates_and_dedupes(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity, invite=[])
        # A duplicated vendor collapses to a single invitation.
        set_rfq_invitations(rfq, [vendor, vendor])
        self.assertEqual(rfq.invitations.count(), 1)
        # An ineligible (on-hold) vendor is rejected.
        onhold = Vendor.objects.create(
            entity=entity, code="HOLD", name="Held", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED", on_hold=True)
        with self.assertRaises(SourcingError):
            set_rfq_invitations(rfq, [vendor, onhold])
        # A cross-entity vendor is rejected.
        other = LedgerEntity.objects.create(name="Other", code="OTHER2", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        foreign = Vendor.objects.create(
            entity=other, code="FRV", name="Foreign",
            payable_account=Account.objects.get(entity=other, code="2100"),
            default_expense_account=Account.objects.get(entity=other, code="5300"),
            kyc_status="VERIFIED")
        with self.assertRaises(SourcingError):
            set_rfq_invitations(rfq, [foreign])

    def test_issue_requires_invitation(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity, invite=[])  # has lines, no invitation
        with self.assertRaises(SourcingError):
            issue_rfq(rfq)
        set_rfq_invitations(rfq, [vendor])
        issue_rfq(rfq)
        rfq.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.ISSUED)

    def test_set_invitations_rejects_removing_responded_vendor(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rival = Vendor.objects.create(
            entity=entity, code="RIVAL2", name="Rival", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        rfq = self._make_rfq(entity, invite=[vendor])
        # A quotation from `vendor` marks it responded (derived, never stored).
        self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        with self.assertRaises(SourcingError):
            set_rfq_invitations(rfq, [rival])  # would strand the responded vendor's bid
        # Keeping the responded vendor while adding another is allowed.
        set_rfq_invitations(rfq, [vendor, rival])
        self.assertEqual(rfq.invitations.count(), 2)

    def test_submit_blocked_when_vendor_uninvited(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity, invite=[vendor])
        issue_rfq(rfq)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        # Withdraw the invitation directly (the API can't on an issued RFQ) - submit must
        # then refuse the now-uninvited vendor's quote.
        RfqInvitation.objects.filter(rfq=rfq, vendor=vendor).delete()
        with self.assertRaises(SourcingError):
            submit_quotation(quo)


class SourcingConcurrencyTests(_P2PFixtureMixin, TransactionTestCase):
    """Real PostgreSQL races for RFQ-first sourcing transitions."""

    serialized_rollback = True

    def _draft_rfq(self, entity, vendor):
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Concurrent sourcing", issue_date=datetime.date(2026, 1, 3),
        )
        RfqLine.objects.create(
            rfq=rfq, description="Switch", quantity=1, line_no=1,
            expense_account=self.acc(entity, "5300"),
        )
        set_rfq_invitations(rfq, [vendor])
        return rfq

    def test_issue_waits_for_draft_edit_and_rechecks_committed_lines(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._draft_rfq(entity, vendor)
        stale = RequestForQuotation.objects.get(pk=rfq.pk)
        edit_locked = threading.Event()
        release_edit = threading.Event()
        issue_attempting = threading.Event()
        issue_done = threading.Event()
        outcomes = {}

        def edit_worker():
            close_old_connections()
            try:
                from django.db import transaction

                with transaction.atomic():
                    current = RequestForQuotation.objects.select_for_update().get(pk=rfq.pk)
                    current.lines.all().delete()
                    current.title = "Committed edit"
                    current.save(update_fields=["title", "updated_at"])
                    edit_locked.set()
                    if not release_edit.wait(5):
                        raise TimeoutError("issue race did not release the draft edit")
                outcomes["edit"] = "committed"
            except Exception as exc:  # pragma: no cover - asserted in the parent thread
                outcomes["edit_error"] = exc
            finally:
                close_old_connections()

        def issue_worker():
            close_old_connections()
            try:
                issue_attempting.set()
                result = issue_rfq(stale)
                outcomes["issue"] = result.rfq_status
            except Exception as exc:
                outcomes["issue_error"] = exc
            finally:
                close_old_connections()
                issue_done.set()

        edit_thread = threading.Thread(target=edit_worker, daemon=True)
        issue_thread = threading.Thread(target=issue_worker, daemon=True)
        edit_thread.start()
        try:
            self.assertTrue(edit_locked.wait(5))
            issue_thread.start()
            self.assertTrue(issue_attempting.wait(5))
            self.assertFalse(issue_done.wait(0.25))
        finally:
            release_edit.set()
        edit_thread.join(5)
        issue_thread.join(5)

        self.assertFalse(edit_thread.is_alive())
        self.assertFalse(issue_thread.is_alive())
        self.assertNotIn("edit_error", outcomes)
        self.assertEqual(outcomes.get("edit"), "committed")
        self.assertIsInstance(outcomes.get("issue_error"), SourcingError)
        rfq.refresh_from_db()
        self.assertEqual(rfq.title, "Committed edit")
        self.assertEqual(rfq.rfq_status, RfqStatus.DRAFT)
        self.assertFalse(rfq.lines.exists())
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.RFQ_ISSUED,
            target_id=str(rfq.pk),
        ).exists())

    def test_simultaneous_submit_has_one_authoritative_winner(self):
        from vs_procurement import sourcing as sourcing_services

        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._draft_rfq(entity, vendor)
        issue_rfq(rfq)
        quotation = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor, quote_date=datetime.date(2026, 1, 4),
        )
        VendorQuotationLine.objects.create(
            quotation=quotation, rfq_line=rfq.lines.get(), description="Switch",
            quantity=1, unit_price=200_000, expense_account=self.acc(entity, "5300"), line_no=1,
        )
        first_in_audit = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_done = threading.Event()
        outcomes = {}

        def blocking_record(**kwargs):
            first_in_audit.set()
            if not release_first.wait(5):
                raise TimeoutError("submit race did not release the first transaction")

        def submit_worker(name, *, attempting=None, done=None):
            close_old_connections()
            try:
                current = VendorQuotation.objects.get(pk=quotation.pk)
                if attempting is not None:
                    attempting.set()
                result = submit_quotation(current)
                outcomes[name] = (result is current, result.quotation_status)
            except Exception as exc:
                outcomes[f"{name}_error"] = exc
            finally:
                close_old_connections()
                if done is not None:
                    done.set()

        first_thread = threading.Thread(target=submit_worker, args=("first",), daemon=True)
        second_thread = threading.Thread(
            target=submit_worker, args=("second",),
            kwargs={"attempting": second_attempting, "done": second_done}, daemon=True,
        )
        with patch.object(sourcing_services, "record", side_effect=blocking_record) as audit_record:
            first_thread.start()
            try:
                self.assertTrue(first_in_audit.wait(5))
                second_thread.start()
                self.assertTrue(second_attempting.wait(5))
                self.assertFalse(second_done.wait(0.25))
            finally:
                release_first.set()
            first_thread.join(5)
            second_thread.join(5)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertNotIn("first_error", outcomes)
        self.assertEqual(outcomes.get("first"), (True, QuotationStatus.SUBMITTED))
        self.assertIsInstance(outcomes.get("second_error"), SourcingError)
        self.assertEqual(audit_record.call_count, 1)
        quotation.refresh_from_db()
        self.assertEqual(quotation.quotation_status, QuotationStatus.SUBMITTED)


def _deny_keys(*denied):
    """side_effect for ``vs_rbac.permissions.has_permission`` denying only *denied* keys."""
    def _check(user, permission_key, *args, **kwargs):
        return permission_key not in denied
    return _check


class SourcingConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Security-first coverage for the RFQ / quotation REST surface."""

    def _client(self, entity, email="sourcing-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Sourcing", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def _issued_rfq(self, entity, *, lines=None, invite=None):
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Switches", issue_date=datetime.date(2026, 1, 3),
        )
        for i, (desc, qty) in enumerate(lines or [("48-port switch", 5)], start=1):
            RfqLine.objects.create(
                rfq=rfq, description=desc, quantity=qty, line_no=i,
                expense_account=self.acc(entity, "5300"),
            )
        # Invite every purchase-eligible vendor so the RFQ can be issued and any of them
        # may quote (invited-only enforcement).
        if invite is None:
            invite = list(
                Vendor.objects.filter(entity=entity, is_active=True, on_hold=False)
                .exclude(kyc_status=VendorKycStatus.REJECTED)
            )
        if invite:
            set_rfq_invitations(rfq, invite)
        issue_rfq(rfq)
        return rfq

    def _submitted_quote(self, entity, rfq, vendor, *, price=200_000):
        quo = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor, quote_date=datetime.date(2026, 1, 4),
        )
        for i, rline in enumerate(rfq.lines.all().order_by("line_no", "id"), start=1):
            VendorQuotationLine.objects.create(
                quotation=quo, rfq_line=rline, description=rline.description,
                expense_account=self.acc(entity, "5300"), quantity=rline.quantity,
                unit_price=price, line_no=i,
            )
        submit_quotation(quo)
        return quo

    # --- permission gating ------------------------------------------------- #

    @patch("vs_procurement.views.orders.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.orders.user_has_rbac_permission", return_value=False)
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_competition_exception_requires_dedicated_permission(
        self, _ordinary_permission, _competition_permission, _super_admin,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        ProcurementSettings.objects.create(
            entity=entity, minimum_rfq_invited_vendors=2,
        )
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Limited market", issue_date=datetime.date(2026, 1, 3),
        )
        RfqLine.objects.create(
            rfq=rfq, description="Switch", quantity=1, line_no=1,
            expense_account=self.acc(entity, "5300"),
        )
        set_rfq_invitations(rfq, [vendor])

        response = self._client(entity).post(
            f"/v1/procurement/rfqs/{rfq.pk}/issue/?entity={entity.code}",
            {"competition_exception_reason": "Only qualified vendor"}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        rfq.refresh_from_db()
        self.assertEqual(rfq.rfq_status, RfqStatus.DRAFT)
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.RFQ_ISSUED,
        ).exists())

    @patch("vs_procurement.views.orders.is_vision_super_admin", return_value=False)
    @patch("vs_procurement.views.orders.user_has_rbac_permission", return_value=True)
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_authorized_competition_exception_is_passed_to_issue_service(
        self, _ordinary_permission, competition_permission, _super_admin,
    ):
        entity, _, vendor, _, _ = self.build_p2p()
        ProcurementSettings.objects.create(
            entity=entity, minimum_rfq_invited_vendors=2,
        )
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Limited market", issue_date=datetime.date(2026, 1, 3),
        )
        RfqLine.objects.create(
            rfq=rfq, description="Switch", quantity=1, line_no=1,
            expense_account=self.acc(entity, "5300"),
        )
        set_rfq_invitations(rfq, [vendor])

        response = self._client(entity).post(
            f"/v1/procurement/rfqs/{rfq.pk}/issue/?entity={entity.code}",
            {"competition_exception_reason": "Only qualified vendor"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        competition_permission.assert_called_once()
        self.assertEqual(
            competition_permission.call_args.args[1],
            "procurement.competition.override",
        )
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.RFQ_ISSUED,
        )
        self.assertEqual(
            audit.metadata["competition_exception_reason"], "Only qualified vendor",
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_every_endpoint_is_rbac_gated(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._issued_rfq(entity)
        quo = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor, quote_date=datetime.date(2026, 1, 4),
        )
        client = self._client(entity)
        e = f"?entity={entity.code}"
        denied = [
            ("get", f"/v1/procurement/rfqs/{e}"),
            ("post", f"/v1/procurement/rfqs/{e}"),
            ("get", f"/v1/procurement/rfqs/summary/{e}"),
            ("get", f"/v1/procurement/rfqs/{rfq.pk}/{e}"),
            ("patch", f"/v1/procurement/rfqs/{rfq.pk}/{e}"),
            ("post", f"/v1/procurement/rfqs/{rfq.pk}/issue/{e}"),
            ("post", f"/v1/procurement/rfqs/{rfq.pk}/close/{e}"),
            ("post", f"/v1/procurement/rfqs/{rfq.pk}/cancel/{e}"),
            ("get", f"/v1/procurement/quotations/{e}"),
            ("post", f"/v1/procurement/quotations/{e}"),
            ("get", f"/v1/procurement/quotations/{quo.pk}/{e}"),
            ("patch", f"/v1/procurement/quotations/{quo.pk}/{e}"),
            ("post", f"/v1/procurement/quotations/{quo.pk}/submit/{e}"),
            ("post", f"/v1/procurement/quotations/{quo.pk}/award/{e}"),
        ]
        for method, url in denied:
            response = getattr(client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 403, f"{method} {url}")

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_patch_requires_dedicated_update_key(self, mock_has, _super):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Draft", issue_date=datetime.date(2026, 1, 3),
        )
        RfqLine.objects.create(
            rfq=rfq, description="Item", quantity=1, line_no=1,
            expense_account=self.acc(entity, "5300"),
        )
        quo = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor, quote_date=datetime.date(2026, 1, 4),
        )
        client = self._client(entity)
        # Deny only the update keys: reads succeed, PATCH is refused for both documents.
        mock_has.side_effect = _deny_keys(
            "procurement.rfq.update", "procurement.quotation.update",
        )
        self.assertEqual(
            client.get(f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}").status_code, 200)
        self.assertEqual(
            client.patch(f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}",
                         {"title": "New"}, format="json").status_code, 403)
        self.assertEqual(
            client.get(f"/v1/procurement/quotations/{quo.pk}/?entity={entity.code}").status_code, 200)
        self.assertEqual(
            client.patch(f"/v1/procurement/quotations/{quo.pk}/?entity={entity.code}",
                         {"reference": "R2"}, format="json").status_code, 403)

    # --- summary ----------------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_summary_counts_are_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        # Draft, open (issued), plus one issued that closes within 7 days, and a response.
        RequestForQuotation.objects.create(
            entity=entity, title="Draft", issue_date=datetime.date(2026, 1, 3))
        open_rfq = self._issued_rfq(entity)
        closing = self._issued_rfq(entity, lines=[("Cable", 2)])
        closing.response_due_date = datetime.date.today() + datetime.timedelta(days=3)
        closing.save(update_fields=["response_due_date", "updated_at"])
        self._submitted_quote(entity, open_rfq, vendor)

        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        RequestForQuotation.objects.create(
            entity=other, title="Foreign", issue_date=datetime.date(2026, 1, 3))

        data = self._client(entity).get(
            f"/v1/procurement/rfqs/summary/?entity={entity.code}").data["data"]
        self.assertEqual(data["draft"], 1)          # only this entity's draft
        self.assertEqual(data["open"], 2)           # two issued RFQs
        self.assertEqual(data["responses_in"], 1)   # one submitted quote on an issued RFQ
        self.assertEqual(data["closing_soon"], 1)   # the one due in 3 days

    # --- list annotations + empty shape ------------------------------------ #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_list_annotations_filters_and_empty_shape(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._issued_rfq(entity, lines=[("Switch", 5), ("Module", 2)])
        self._submitted_quote(entity, rfq, vendor)
        client = self._client(entity)
        row = client.get(f"/v1/procurement/rfqs/?entity={entity.code}").data["data"][0]
        self.assertEqual(row["line_count"], 2)
        self.assertEqual(row["response_count"], 1)
        # ?status filter and ?q search.
        self.assertEqual(
            len(client.get(f"/v1/procurement/rfqs/?entity={entity.code}&status=ISSUED").data["data"]), 1)
        self.assertEqual(
            len(client.get(f"/v1/procurement/rfqs/?entity={entity.code}&status=DRAFT").data["data"]), 0)
        self.assertEqual(
            len(client.get(f"/v1/procurement/rfqs/?entity={entity.code}&q=switch").data["data"]), 1)
        # Quotations hold a PROTECT FK to the RFQ, so clear them first.
        VendorQuotation.objects.filter(entity=entity).delete()
        RequestForQuotation.objects.filter(entity=entity).delete()
        empty = client.get(f"/v1/procurement/rfqs/?entity={entity.code}")
        self.assertIn(empty.data["data"], ({}, []))
        empty_q = client.get(f"/v1/procurement/quotations/?entity={entity.code}")
        self.assertIn(empty_q.data["data"], ({}, []))

    # --- cross-entity isolation -------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_cross_entity_ids_are_404_or_rejected(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        other_vendor = Vendor.objects.create(
            entity=other, code="FARV", name="Foreign", payable_account=Account.objects.get(entity=other, code="2100"),
            default_expense_account=Account.objects.get(entity=other, code="5300"), kyc_status="VERIFIED",
        )
        other_tax = TaxCode.objects.create(
            entity=other, code="F-VAT", name="Foreign VAT", rate_bps=750,
            paid_account=Account.objects.get(entity=other, code="1300"),
        )
        rfq = self._issued_rfq(entity)
        quo = self._submitted_quote(entity, rfq, vendor)
        client = self._client(entity)
        # RFQ / quotation ids are invisible under the wrong entity.
        self.assertEqual(client.get(
            f"/v1/procurement/rfqs/{rfq.pk}/?entity={other.code}").status_code, 404)
        self.assertEqual(client.get(
            f"/v1/procurement/quotations/{quo.pk}/?entity={other.code}").status_code, 404)
        # Cross-entity vendor / tax code / rfq_line on quotation create are all rejected.
        base = {"rfq": rfq.pk, "quote_date": "2026-01-04",
                "lines": [{"description": "x", "quantity": 1, "unit_price": 100}]}
        self.assertEqual(client.post(
            f"/v1/procurement/quotations/?entity={entity.code}",
            {**base, "vendor": other_vendor.pk}, format="json").status_code, 400)
        self.assertEqual(client.post(
            f"/v1/procurement/quotations/?entity={entity.code}",
            {**base, "vendor": vendor.code,
             "lines": [{"description": "x", "quantity": 1, "unit_price": 100, "tax_code": other_tax.pk}]},
            format="json").status_code, 400)
        foreign_rfq = self._issued_rfq(entity, lines=[("Other", 1)])
        foreign_line = foreign_rfq.lines.first()
        self.assertEqual(client.post(
            f"/v1/procurement/quotations/?entity={entity.code}",
            {**base, "vendor": vendor.code,
             "lines": [{"description": "x", "quantity": 1, "unit_price": 100, "rfq_line": foreign_line.pk}]},
            format="json").status_code, 400)

    # --- validation bounds ------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_rfq_source_line_must_match_header_requisition_with_patch_rollback(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        first = PurchaseRequisition.objects.create(
            entity=entity, title="First source", request_date=datetime.date(2026, 1, 2),
        )
        first_line = PurchaseRequisitionLine.objects.create(
            requisition=first, description="First item", quantity=1,
            estimated_unit_price=100_000, expense_account=self.acc(entity, "5300"), line_no=1,
        )
        second = PurchaseRequisition.objects.create(
            entity=entity, title="Second source", request_date=datetime.date(2026, 1, 2),
        )
        second_line = PurchaseRequisitionLine.objects.create(
            requisition=second, description="Second item", quantity=1,
            estimated_unit_price=100_000, expense_account=self.acc(entity, "5300"), line_no=1,
        )
        client = self._client(entity)
        url = f"/v1/procurement/rfqs/?entity={entity.code}"

        def body(title, requisition, source_line):
            payload = {
                "title": title, "issue_date": "2026-01-03",
                "lines": [{
                    "description": title, "quantity": 1,
                    "expense_account": "5300", "requisition_line": source_line.pk,
                }],
            }
            if requisition is not None:
                payload["requisition"] = requisition.pk
            return payload

        accepted = client.post(url, body("Accepted", first, first_line), format="json")
        self.assertEqual(accepted.status_code, 201)
        rfq = RequestForQuotation.objects.get(pk=accepted.data["data"]["id"])
        original = rfq.lines.get()
        self.assertEqual(original.requisition_line_id, first_line.pk)

        rejected = client.post(url, body("Mismatched", first, second_line), format="json")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("requisition_line", rejected.data["error"]["detail"])
        self.assertFalse(RequestForQuotation.objects.filter(
            entity=entity, title="Mismatched",
        ).exists())

        # Headerless RFQs retain the existing entity-scoped source-line behavior.
        headerless = client.post(url, body("Headerless", None, second_line), format="json")
        self.assertEqual(headerless.status_code, 201)
        self.assertEqual(
            RequestForQuotation.objects.get(pk=headerless.data["data"]["id"])
            .lines.get().requisition_line_id,
            second_line.pk,
        )

        patch_url = f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}"
        bad_patch = client.patch(
            patch_url, {"lines": body("Bad replacement", first, second_line)["lines"]},
            format="json",
        )
        self.assertEqual(bad_patch.status_code, 400)
        self.assertIn("requisition_line", bad_patch.data["error"]["detail"])
        self.assertEqual(rfq.lines.count(), 1)
        preserved = rfq.lines.get()
        self.assertEqual(preserved.pk, original.pk)
        self.assertEqual(preserved.requisition_line_id, first_line.pk)

        valid_patch = client.patch(
            patch_url, {"lines": body("Valid replacement", first, first_line)["lines"]},
            format="json",
        )
        self.assertEqual(valid_patch.status_code, 200)
        self.assertEqual(rfq.lines.get().requisition_line_id, first_line.pk)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_rfq_create_validation_bounds(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        url = f"/v1/procurement/rfqs/?entity={entity.code}"

        def line(**over):
            base = {"description": "Item", "quantity": 1, "expense_account": "5300"}
            base.update(over)
            return base

        def create(**over):
            body = {"title": "R", "issue_date": "2026-01-03", "lines": [line()]}
            body.update(over)
            return client.post(url, body, format="json")

        self.assertEqual(create(lines=[line(quantity=0)]).status_code, 400)
        self.assertEqual(create(lines=[line(quantity=-3)]).status_code, 400)
        self.assertEqual(create(lines=[line(quantity="NaN")]).status_code, 400)
        self.assertEqual(create(title="x" * 201).status_code, 400)
        # Closing before issue date.
        self.assertEqual(create(response_due_date="2026-01-01").status_code, 400)
        # A LIABILITY (non-EXPENSE) account is rejected.
        self.assertEqual(create(lines=[line(expense_account="2100")]).status_code, 400)
        # An inactive EXPENSE account is rejected.
        expense = self.acc(entity, "5300")
        expense.is_active = False
        expense.save(update_fields=["is_active", "updated_at"])
        self.assertEqual(create().status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_quotation_create_validation_and_issued_requirement(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        draft_rfq = RequestForQuotation.objects.create(
            entity=entity, title="Draft", issue_date=datetime.date(2026, 1, 3))
        RfqLine.objects.create(
            rfq=draft_rfq, description="Item", quantity=1, line_no=1,
            expense_account=self.acc(entity, "5300"))
        issued = self._issued_rfq(entity)
        client = self._client(entity)
        url = f"/v1/procurement/quotations/?entity={entity.code}"

        def create(rfq, **over):
            body = {"rfq": rfq.pk, "vendor": vendor.code, "quote_date": "2026-01-04",
                    "lines": [{"description": "x", "quantity": 1, "unit_price": 100}]}
            body.update(over)
            return client.post(url, body, format="json")

        # Quotation against a non-issued RFQ is rejected.
        self.assertEqual(create(draft_rfq).status_code, 400)
        # Float / negative kobo unit price rejected.
        self.assertEqual(create(
            issued, lines=[{"description": "x", "quantity": 1, "unit_price": 100.5}]).status_code, 400)
        self.assertEqual(create(
            issued, lines=[{"description": "x", "quantity": 1, "unit_price": -5}]).status_code, 400)
        # valid_until before quote_date rejected.
        self.assertEqual(create(issued, valid_until="2026-01-01").status_code, 400)
        # lead_time out of range rejected.
        self.assertEqual(create(issued, lead_time_days=5000).status_code, 400)
        # Happy path.
        self.assertEqual(create(issued).status_code, 201)

    # --- eligibility + lifecycle via the API ------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_quotation_create_blocks_ineligible_vendor(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._issued_rfq(entity)  # vendor invited while still eligible
        # Vendor goes on hold after issue: its quotation must be refused on eligibility.
        vendor.on_hold = True
        vendor.save(update_fields=["on_hold", "updated_at"])
        response = self._client(entity).post(
            f"/v1/procurement/quotations/?entity={entity.code}",
            {"rfq": rfq.pk, "vendor": vendor.code, "quote_date": "2026-01-04",
             "lines": [{"description": "x", "quantity": 1, "unit_price": 100}]}, format="json")
        self.assertEqual(response.status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_patch_is_draft_only(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._issued_rfq(entity)  # already issued → not editable
        quo = self._submitted_quote(entity, rfq, vendor)  # submitted → not editable
        client = self._client(entity)
        self.assertEqual(client.patch(
            f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}",
            {"title": "New"}, format="json").status_code, 400)
        self.assertEqual(client.patch(
            f"/v1/procurement/quotations/{quo.pk}/?entity={entity.code}",
            {"reference": "R"}, format="json").status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_award_via_api_builds_draft_po_and_rejects_losers(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rival = Vendor.objects.create(
            entity=entity, code="RIVAL", name="Rival", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        rfq = self._issued_rfq(entity)
        winner = self._submitted_quote(entity, rfq, vendor, price=200_000)
        loser = self._submitted_quote(entity, rfq, rival, price=250_000)
        response = self._client(entity).post(
            f"/v1/procurement/quotations/{winner.pk}/award/?entity={entity.code}", {}, format="json")
        self.assertEqual(response.status_code, 201)
        po = PurchaseOrder.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(po.status, DocumentStatus.DRAFT)
        self.assertEqual(po.vendor_id, vendor.pk)
        self.assertEqual(po.entity_id, entity.pk)
        loser.refresh_from_db()
        rfq.refresh_from_db()
        self.assertEqual(loser.quotation_status, QuotationStatus.REJECTED)
        self.assertEqual(rfq.rfq_status, RfqStatus.AWARDED)
        # A second award on the same RFQ is refused.
        self.assertEqual(self._client(entity, "second@test.com").post(
            f"/v1/procurement/quotations/{loser.pk}/award/?entity={entity.code}", {},
            format="json").status_code, 422)

    # --- invited vendors + budget over the API ----------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_invited_only_quotation_create(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        uninvited = Vendor.objects.create(
            entity=entity, code="UNINV", name="Uninvited", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        # Only `vendor` is on the RFQ's addressee list.
        rfq = self._issued_rfq(entity, invite=[vendor])
        client = self._client(entity)

        def create(v):
            return client.post(
                f"/v1/procurement/quotations/?entity={entity.code}",
                {"rfq": rfq.pk, "vendor": v.code, "quote_date": "2026-01-04",
                 "lines": [{"description": "x", "quantity": 1, "unit_price": 100}]}, format="json")

        # Uninvited but otherwise-eligible vendor is rejected; the invited vendor succeeds.
        self.assertEqual(create(uninvited).status_code, 400)
        self.assertEqual(create(vendor).status_code, 201)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_budget_estimate_validation(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        url = f"/v1/procurement/rfqs/?entity={entity.code}"

        def create(**over):
            body = {"title": "R", "issue_date": "2026-01-03",
                    "lines": [{"description": "Item", "quantity": 1, "expense_account": "5300"}]}
            body.update(over)
            return client.post(url, body, format="json")

        created = create(budget_estimate=9_500_000)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["data"]["budget_estimate"], 9_500_000)
        # Float and negative kobo are rejected (integer-kobo boundary).
        self.assertEqual(create(budget_estimate=100.5).status_code, 400)
        self.assertEqual(create(budget_estimate=-5).status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_patch_replaces_and_protects_invite_set(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other = Vendor.objects.create(
            entity=entity, code="OTHV", name="Other", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        client = self._client(entity)
        created = client.post(
            f"/v1/procurement/rfqs/?entity={entity.code}",
            {"title": "Draft", "issue_date": "2026-01-03", "invited_vendors": [vendor.code],
             "lines": [{"description": "Item", "quantity": 1, "expense_account": "5300"}]},
            format="json")
        self.assertEqual(created.status_code, 201)
        rfq_id = created.data["data"]["id"]
        # PATCH replaces the invite set on a draft.
        patched = client.patch(
            f"/v1/procurement/rfqs/{rfq_id}/?entity={entity.code}",
            {"invited_vendors": [other.code]}, format="json")
        self.assertEqual(patched.status_code, 200)
        self.assertEqual([i["vendor_code"] for i in patched.data["data"]["invitations"]], ["OTHV"])
        # A responded vendor cannot be dropped: attach a quotation for `other`, then try.
        rfq = RequestForQuotation.objects.get(pk=rfq_id)
        VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=other, quote_date=datetime.date(2026, 1, 4))
        self.assertEqual(client.patch(
            f"/v1/procurement/rfqs/{rfq_id}/?entity={entity.code}",
            {"invited_vendors": [vendor.code]}, format="json").status_code, 422)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_rfq_detail_invitations_responded_derivation(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rival = Vendor.objects.create(
            entity=entity, code="RIV3", name="Rival", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        rfq = self._issued_rfq(entity, invite=[vendor, rival])
        quo = self._submitted_quote(entity, rfq, vendor)
        # A foreign entity's RFQ must not inflate this entity's invited_count.
        other = LedgerEntity.objects.create(name="Other", code="OTHER3", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        RequestForQuotation.objects.create(
            entity=other, title="Foreign", issue_date=datetime.date(2026, 1, 3))
        client = self._client(entity)
        detail = client.get(
            f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}").data["data"]
        self.assertEqual(detail["invited_count"], 2)
        by_vendor = {i["vendor_code"]: i for i in detail["invitations"]}
        self.assertTrue(by_vendor[vendor.code]["responded"])
        self.assertEqual(by_vendor[vendor.code]["quotation_id"], quo.pk)
        self.assertFalse(by_vendor[rival.code]["responded"])
        self.assertIsNone(by_vendor[rival.code]["quotation_id"])
        # The list annotation is entity-scoped too.
        row = next(r for r in client.get(
            f"/v1/procurement/rfqs/?entity={entity.code}").data["data"] if r["id"] == rfq.pk)
        self.assertEqual(row["invited_count"], 2)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_rfq_detail_uses_newest_quotation_per_vendor_and_latest_first_order(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._issued_rfq(entity, invite=[vendor])
        older = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=datetime.date(2026, 1, 4),
            quotation_status=QuotationStatus.SUBMITTED, total=100_000,
        )
        newer = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=datetime.date(2026, 1, 5),
            quotation_status=QuotationStatus.DRAFT, total=250_000,
        )
        now = timezone.now()
        VendorQuotation.objects.filter(pk=older.pk).update(
            created_at=now - datetime.timedelta(days=1),
        )
        VendorQuotation.objects.filter(pk=newer.pk).update(created_at=now)

        detail = self._client(entity).get(
            f"/v1/procurement/rfqs/{rfq.pk}/?entity={entity.code}",
        ).data["data"]
        invitation = detail["invitations"][0]
        self.assertTrue(invitation["responded"])
        self.assertEqual(invitation["quotation_id"], newer.pk)
        self.assertEqual(invitation["quotation_status"], QuotationStatus.DRAFT)
        self.assertEqual(invitation["quotation_total"], 250_000)
        self.assertEqual(
            [quotation["id"] for quotation in detail["quotations"]],
            [newer.pk, older.pk],
        )


# --------------------------------------------------------------------------- #
# Item catalog                                                                #
# --------------------------------------------------------------------------- #

class CatalogItemTests(_P2PFixtureMixin, TestCase):
    """Catalog master data and its line-default seeding."""

    def test_line_defaults_returns_buying_defaults(self):
        entity, _, vendor, input_vat, _ = self.build_p2p()
        item = CatalogItem.objects.create(
            entity=entity, code="CHAIR", name="Mesh chair",
            description="Ergonomic mesh chair",
            preferred_vendor=vendor,
            default_expense_account=self.acc(entity, "5300"),
            default_tax_code=input_vat,
            lead_time_days=7, standard_unit_price=200_000,
        )
        defaults = item.line_defaults()
        self.assertEqual(defaults["description"], "Ergonomic mesh chair")
        self.assertEqual(defaults["expense_account"], self.acc(entity, "5300"))
        self.assertEqual(defaults["tax_code"], input_vat)
        self.assertEqual(defaults["unit_price"], 200_000)

    def test_line_defaults_falls_back_to_name(self):
        entity, _, _, _, _ = self.build_p2p()
        item = CatalogItem.objects.create(
            entity=entity, code="PEN", name="Blue pen", standard_unit_price=5_000,
        )
        self.assertEqual(item.line_defaults()["description"], "Blue pen")
        self.assertIsNone(item.line_defaults()["expense_account"])

    def test_code_unique_per_entity(self):
        from django.db import IntegrityError, transaction
        entity, _, _, _, _ = self.build_p2p()
        CatalogItem.objects.create(entity=entity, code="DUP", name="First")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogItem.objects.create(entity=entity, code="DUP", name="Second")


class CatalogItemConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Backfilled security-first coverage for the catalog REST surface.

    Grouped separately from the sourcing suite so it can be committed on its own.
    """

    def _client(self, entity, email="catalog-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Catalog", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_insights_requires_report_permission(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = CatalogItem.objects.create(entity=entity, code="ITEM", name="Item")
        response = self._client(entity).get(
            f"/v1/procurement/catalog-items/{item.pk}/insights/?entity={entity.code}")
        self.assertEqual(response.status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_insights_is_entity_scoped(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = CatalogItem.objects.create(entity=entity, code="ITEM", name="Item")
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        # The same id must not resolve under a different entity.
        self.assertEqual(self._client(entity).get(
            f"/v1/procurement/catalog-items/{item.pk}/insights/?entity={other.code}").status_code, 404)
        self.assertEqual(self._client(entity, "cat2@test.com").get(
            f"/v1/procurement/catalog-items/{item.pk}/insights/?entity={entity.code}").status_code, 200)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_inactive_category_and_validates_bounds(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        inactive = VendorCategory.objects.create(
            entity=entity, code="OLD", name="Legacy", is_active=False)
        client = self._client(entity)
        url = f"/v1/procurement/catalog-items/?entity={entity.code}"
        # Inactive category cannot be assigned to a new item.
        self.assertEqual(client.post(url, {
            "code": "A", "name": "A", "category": inactive.pk}, format="json").status_code, 400)
        # Non-integer price is rejected at the kobo boundary.
        self.assertEqual(client.post(url, {
            "code": "B", "name": "B", "standard_unit_price": 100.5}, format="json").status_code, 400)
        # A non-EXPENSE default account is rejected.
        self.assertEqual(client.post(url, {
            "code": "C", "name": "C", "default_expense_account": "2100"}, format="json").status_code, 400)
        self.assertEqual(client.post(url, {"code": "D", "name": "D"}, format="json").status_code, 201)
        generated = client.post(url, {"name": "Generated item"}, format="json")
        self.assertEqual(generated.status_code, 201)
        self.assertTrue(generated.data["data"]["code"].startswith(f"IT-{entity.tenant_id}"))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_code_is_immutable_after_creation(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = CatalogItem.objects.create(entity=entity, code="KEEP", name="Item")
        response = self._client(entity).patch(
            f"/v1/procurement/catalog-items/{item.pk}/?entity={entity.code}",
            {"code": "CHANGED"}, format="json")
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.code, "KEEP")


# --------------------------------------------------------------------------- #
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

class VendorContractTests(_P2PFixtureMixin, TestCase):
    """Contract lifecycle, milestones and renewal/expiry alerts."""

    def _contract(self, entity, vendor, *, ref="C-001", start, end, notice=30, status=None):
        return VendorContract.objects.create(
            entity=entity, vendor=vendor, reference=ref, title="Cleaning services",
            start_date=start, end_date=end, contract_value=12_000_000,
            renewal_notice_days=notice, status=status or ContractStatus.DRAFT,
        )

    def test_activate_requires_dates_and_flips_status(self):
        entity, _, vendor, _, _ = self.build_p2p()
        no_dates = VendorContract.objects.create(
            entity=entity, vendor=vendor, reference="C-ND", title="No dates",
        )
        with self.assertRaises(ContractError):
            activate_contract(no_dates)
        c = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        activate_contract(c)
        c.refresh_from_db()
        self.assertEqual(c.status, ContractStatus.ACTIVE)

    def test_activate_blocks_ineligible_vendor(self):
        entity, _, vendor, _, _ = self.build_p2p()
        contract = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        vendor.on_hold = True
        vendor.save(update_fields=["on_hold", "updated_at"])

        with self.assertRaises(ContractError):
            activate_contract(contract)

        contract.refresh_from_db()
        self.assertEqual(contract.status, ContractStatus.DRAFT)

    def test_renew_builds_successor_and_marks_original_renewed(self):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        activate_contract(c)
        ContractMilestone.objects.create(
            contract=c, name="Q1 review", due_date=datetime.date(2026, 3, 31),
            amount=3_000_000, line_no=1,
        )
        successor = renew_contract(
            c, reference="C-001-R", start_date=datetime.date(2027, 1, 1),
            end_date=datetime.date(2027, 12, 31), copy_milestones=True,
        )
        c.refresh_from_db()
        self.assertEqual(c.status, ContractStatus.RENEWED)
        self.assertEqual(successor.status, ContractStatus.ACTIVE)
        self.assertEqual(successor.renews_id, c.pk)
        self.assertEqual(successor.contract_value, 12_000_000)  # carried over
        self.assertEqual(successor.milestones.count(), 1)       # copied forward

    def test_terminate_is_idempotent_and_refuses_draft(self):
        entity, _, vendor, _, _ = self.build_p2p()
        draft = self._contract(
            entity, vendor, ref="C-D", start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31),
        )
        with self.assertRaises(ContractError):
            terminate_contract(draft)
        activate_contract(draft)
        terminate_contract(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ContractStatus.TERMINATED)
        # Idempotent on terminal state.
        terminate_contract(draft)
        self.assertEqual(draft.status, ContractStatus.TERMINATED)

    def test_terminate_stale_instance_is_idempotent_with_single_audit(self):
        entity, _, vendor, _, _ = self.build_p2p()
        contract = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE,
        )
        stale = VendorContract.objects.get(pk=contract.pk)

        self.assertIs(terminate_contract(contract), contract)
        self.assertIs(terminate_contract(stale), stale)
        self.assertEqual(contract.status, ContractStatus.TERMINATED)
        self.assertEqual(stale.status, ContractStatus.TERMINATED)
        self.assertEqual(
            FinanceAuditLog.objects.filter(
                entity=entity, action=FinanceAuditAction.VENDOR_CONTRACT_TERMINATED,
                target_id=str(contract.pk),
            ).count(),
            1,
        )

    def test_complete_milestone_sets_date_and_status(self):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        ms = ContractMilestone.objects.create(
            contract=c, name="Kickoff", due_date=datetime.date(2026, 2, 1),
            amount=1_000_000, line_no=1,
        )
        complete_milestone(ms, on=datetime.date(2026, 1, 30))
        ms.refresh_from_db()
        self.assertEqual(ms.status, MilestoneStatus.COMPLETED)
        self.assertEqual(ms.completed_date, datetime.date(2026, 1, 30))

    def test_complete_stale_milestone_is_idempotent_with_single_audit(self):
        entity, _, vendor, _, _ = self.build_p2p()
        contract = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        milestone = ContractMilestone.objects.create(
            contract=contract, name="Kickoff", due_date=datetime.date(2026, 2, 1),
            amount=1_000_000, line_no=1,
        )
        stale = ContractMilestone.objects.get(pk=milestone.pk)

        self.assertIs(
            complete_milestone(milestone, on=datetime.date(2026, 1, 30)), milestone,
        )
        self.assertIs(
            complete_milestone(stale, on=datetime.date(2026, 1, 31)), stale,
        )
        self.assertEqual(milestone.status, MilestoneStatus.COMPLETED)
        self.assertEqual(stale.status, MilestoneStatus.COMPLETED)
        self.assertEqual(stale.completed_date, datetime.date(2026, 1, 30))
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.CONTRACT_MILESTONE_COMPLETED,
            target_id=str(contract.pk),
        )
        self.assertEqual(audit.metadata["milestone_id"], milestone.pk)

    def test_flag_missed_milestones(self):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        ContractMilestone.objects.create(
            contract=c, name="Late", due_date=datetime.date(2026, 1, 5), amount=0, line_no=1,
        )
        ContractMilestone.objects.create(
            contract=c, name="Future", due_date=datetime.date(2026, 12, 1), amount=0, line_no=2,
        )
        count = flag_missed_milestones(entity, as_of=datetime.date(2026, 6, 1))
        self.assertEqual(count, 1)
        self.assertEqual(
            c.milestones.filter(status=MilestoneStatus.MISSED).count(), 1)

    def test_expiring_and_mark_expired(self):
        entity, _, vendor, _, _ = self.build_p2p()
        # Ends soon, 30-day notice - inside window as of 2026-12-15.
        soon = self._contract(
            entity, vendor, ref="C-SOON", start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31), notice=30,
        )
        activate_contract(soon)
        # Ends far away - not yet in window.
        later = self._contract(
            entity, vendor, ref="C-LATER", start=datetime.date(2026, 1, 1),
            end=datetime.date(2027, 6, 30), notice=30,
        )
        activate_contract(later)

        due = expiring_contracts(entity, as_of=datetime.date(2026, 12, 15))
        self.assertEqual([c.reference for c in due], ["C-SOON"])

        # within_days horizon overrides per-contract notice.
        due90 = expiring_contracts(entity, as_of=datetime.date(2026, 12, 15), within_days=400)
        self.assertIn("C-LATER", [c.reference for c in due90])

        # mark_expired flips only the lapsed one.
        moved = mark_expired(entity, as_of=datetime.date(2027, 1, 1))
        self.assertEqual(moved, 1)
        soon.refresh_from_db()
        self.assertEqual(soon.status, ContractStatus.EXPIRED)


class VendorContractConcurrencyTests(_P2PFixtureMixin, TransactionTestCase):
    """PostgreSQL row locks serialize competing contract lifecycle transitions."""

    # LedgerEntity's platform fallback depends on migration-seeded ``codex`` tenant
    # data; TransactionTestCase flushes between methods, so restore serialized seed data.
    serialized_rollback = True

    def _contract(self, entity, vendor, *, ref="C-RACE"):
        return VendorContract.objects.create(
            entity=entity, vendor=vendor, reference=ref, title="Race",
            status=ContractStatus.ACTIVE, start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31), contract_value=12_000_000,
        )

    def test_terminate_serializes_against_concurrent_renewal(self):
        from vs_procurement import contracts as contract_services

        entity, _, vendor, _, _ = self.build_p2p()
        contract = self._contract(entity, vendor)
        audit_entered = threading.Event()
        release_termination = threading.Event()
        renewal_attempting = threading.Event()
        renewal_done = threading.Event()
        outcomes = {}

        def blocking_record(**kwargs):
            audit_entered.set()
            if not release_termination.wait(5):
                raise TimeoutError("termination test did not release the audit boundary")

        def terminate_worker():
            close_old_connections()
            try:
                current = VendorContract.objects.get(pk=contract.pk)
                result = terminate_contract(current)
                outcomes["termination"] = result.status
            except Exception as exc:  # pragma: no cover - asserted in the parent thread
                outcomes["termination_error"] = exc
            finally:
                close_old_connections()

        def renew_worker():
            close_old_connections()
            try:
                current = VendorContract.objects.get(pk=contract.pk)
                renewal_attempting.set()
                renew_contract(
                    current, reference="C-RACE-R", start_date=datetime.date(2027, 1, 1),
                    end_date=datetime.date(2027, 12, 31),
                )
                outcomes["renewal"] = "succeeded"
            except Exception as exc:  # The serialized loser must see TERMINATED.
                outcomes["renewal_error"] = exc
            finally:
                close_old_connections()
                renewal_done.set()

        terminate_thread = threading.Thread(target=terminate_worker, daemon=True)
        renew_thread = threading.Thread(target=renew_worker, daemon=True)
        with patch.object(contract_services, "record", side_effect=blocking_record) as audit_record:
            terminate_thread.start()
            try:
                self.assertTrue(audit_entered.wait(5))
                renew_thread.start()
                self.assertTrue(renewal_attempting.wait(5))
                self.assertFalse(renewal_done.wait(0.25))
            finally:
                release_termination.set()
            terminate_thread.join(5)
            renew_thread.join(5)

        self.assertFalse(terminate_thread.is_alive())
        self.assertFalse(renew_thread.is_alive())
        self.assertNotIn("termination_error", outcomes)
        self.assertEqual(outcomes.get("termination"), ContractStatus.TERMINATED)
        self.assertIsInstance(outcomes.get("renewal_error"), ContractError)
        self.assertEqual(audit_record.call_count, 1)
        contract.refresh_from_db()
        self.assertEqual(contract.status, ContractStatus.TERMINATED)
        self.assertFalse(VendorContract.objects.filter(reference="C-RACE-R").exists())

    def test_concurrent_milestone_completions_transition_once(self):
        from vs_procurement import contracts as contract_services

        entity, _, vendor, _, _ = self.build_p2p()
        contract = self._contract(entity, vendor)
        milestone = ContractMilestone.objects.create(
            contract=contract, name="Kickoff", due_date=datetime.date(2026, 2, 1),
            amount=1_000_000, line_no=1,
        )
        audit_entered = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_done = threading.Event()
        outcomes = {}

        def blocking_record(**kwargs):
            audit_entered.set()
            if not release_first.wait(5):
                raise TimeoutError("milestone test did not release the audit boundary")

        def complete_worker(name, on, *, attempting=None, done=None):
            close_old_connections()
            try:
                current = ContractMilestone.objects.get(pk=milestone.pk)
                if attempting is not None:
                    attempting.set()
                result = complete_milestone(current, on=on)
                outcomes[name] = (result.status, result.completed_date)
            except Exception as exc:  # pragma: no cover - asserted in the parent thread
                outcomes[f"{name}_error"] = exc
            finally:
                close_old_connections()
                if done is not None:
                    done.set()

        first_thread = threading.Thread(
            target=complete_worker,
            args=("first", datetime.date(2026, 1, 30)),
            daemon=True,
        )
        second_thread = threading.Thread(
            target=complete_worker,
            args=("second", datetime.date(2026, 1, 31)),
            kwargs={"attempting": second_attempting, "done": second_done},
            daemon=True,
        )
        with patch.object(contract_services, "record", side_effect=blocking_record) as audit_record:
            first_thread.start()
            try:
                self.assertTrue(audit_entered.wait(5))
                second_thread.start()
                self.assertTrue(second_attempting.wait(5))
                self.assertFalse(second_done.wait(0.25))
            finally:
                release_first.set()
            first_thread.join(5)
            second_thread.join(5)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertNotIn("first_error", outcomes)
        self.assertNotIn("second_error", outcomes)
        expected = (MilestoneStatus.COMPLETED, datetime.date(2026, 1, 30))
        self.assertEqual(outcomes.get("first"), expected)
        self.assertEqual(outcomes.get("second"), expected)
        self.assertEqual(audit_record.call_count, 1)
        milestone.refresh_from_db()
        self.assertEqual((milestone.status, milestone.completed_date), expected)


class ContractConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Security-first coverage for the vendor-contract REST surface."""

    def _client(self, entity, email="contract-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Contract", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def _contract(self, entity, vendor, *, ref="C-API-1", start, end, value=12_000_000,
                  status=None):
        c = VendorContract.objects.create(
            entity=entity, vendor=vendor, reference=ref, title="Support",
            start_date=start, end_date=end, contract_value=value,
        )
        if status:
            VendorContract.objects.filter(pk=c.pk).update(status=status)
            c.refresh_from_db()
        return c

    # --- permission gating ------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_every_endpoint_is_rbac_gated(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(entity, vendor, start=datetime.date(2026, 1, 1),
                           end=datetime.date(2026, 12, 31))
        client = self._client(entity)
        e = f"?entity={entity.code}"
        denied = [
            ("get", f"/v1/procurement/contracts/{e}"),
            ("post", f"/v1/procurement/contracts/{e}"),
            ("get", f"/v1/procurement/contracts/summary/{e}"),
            ("get", f"/v1/procurement/contracts/renewals/{e}"),
            ("get", f"/v1/procurement/contracts/{c.pk}/{e}"),
            ("patch", f"/v1/procurement/contracts/{c.pk}/{e}"),
            ("get", f"/v1/procurement/contracts/{c.pk}/linked-pos/{e}"),
            ("post", f"/v1/procurement/contracts/{c.pk}/activate/{e}"),
            ("post", f"/v1/procurement/contracts/{c.pk}/renew/{e}"),
            ("post", f"/v1/procurement/contracts/{c.pk}/terminate/{e}"),
        ]
        for method, url in denied:
            response = getattr(client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 403, f"{method} {url}")

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_linked_pos_gated_on_purchase_order_key(self, mock_has, _super):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(entity, vendor, start=datetime.date(2026, 1, 1),
                           end=datetime.date(2026, 12, 31))
        client = self._client(entity)
        # A user who can view the contract but not purchase orders is refused Linked POs.
        mock_has.side_effect = _deny_keys("procurement.purchase_order.view")
        self.assertEqual(
            client.get(f"/v1/procurement/contracts/{c.pk}/?entity={entity.code}").status_code, 200)
        self.assertEqual(
            client.get(f"/v1/procurement/contracts/{c.pk}/linked-pos/?entity={entity.code}"
                       ).status_code, 403)

    # --- entity isolation -------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_cross_entity_contract_is_not_reachable(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        ovendor = Vendor.objects.create(
            entity=other, code="OV", name="Other Vendor",
            payable_account=Account.objects.get(entity=other, code="2100"), kyc_status="VERIFIED")
        foreign = self._contract(other, ovendor, ref="C-OTHER",
                                 start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31))
        client = self._client(entity)
        e = f"?entity={entity.code}"
        # Reading/acting on another entity's contract id inside my entity → 404. (GETs are
        # called without a body - a data dict on .get() would fold into the query string and
        # drop ?entity.)
        self.assertEqual(client.get(f"/v1/procurement/contracts/{foreign.pk}/{e}").status_code, 404)
        self.assertEqual(
            client.get(f"/v1/procurement/contracts/{foreign.pk}/linked-pos/{e}").status_code, 404)
        for method, url in [
            ("patch", f"/v1/procurement/contracts/{foreign.pk}/{e}"),
            ("post", f"/v1/procurement/contracts/{foreign.pk}/activate/{e}"),
            ("post", f"/v1/procurement/contracts/{foreign.pk}/terminate/{e}"),
        ]:
            self.assertEqual(getattr(client, method)(url, {}, format="json").status_code, 404,
                             f"{method} {url}")

    # --- create / reference auto-generation / validation ------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_autogenerates_unique_reference(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        body = {"vendor": vendor.code, "title": "No ref supplied",
                "start_date": "2026-02-01", "end_date": "2026-11-30", "contract_value": 5_000_000}
        r1 = client.post(f"/v1/procurement/contracts/?entity={entity.code}", body, format="json")
        r2 = client.post(f"/v1/procurement/contracts/?entity={entity.code}", body, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        ref1, ref2 = r1.data["data"]["reference"], r2.data["data"]["reference"]
        prefix = f"CT-{entity.tenant_id}{timezone.localdate():%y%m%d}"
        self.assertEqual((ref1, ref2), (f"{prefix}1", f"{prefix}2"))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_and_patch_reject_bad_input(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        base = f"/v1/procurement/contracts/?entity={entity.code}"
        # end before start
        self.assertEqual(client.post(base, {
            "vendor": vendor.code, "title": "X", "start_date": "2026-06-01",
            "end_date": "2026-01-01"}, format="json").status_code, 400)
        # negative / non-integer kobo
        self.assertEqual(client.post(base, {
            "vendor": vendor.code, "title": "X", "contract_value": -5}, format="json").status_code, 400)
        # missing title
        self.assertEqual(client.post(base, {"vendor": vendor.code}, format="json").status_code, 400)
        # terminal contracts cannot be edited
        c = self._contract(entity, vendor, start=datetime.date(2026, 1, 1),
                           end=datetime.date(2026, 6, 1), status=ContractStatus.TERMINATED)
        self.assertEqual(client.patch(
            f"/v1/procurement/contracts/{c.pk}/?entity={entity.code}",
            {"title": "nope"}, format="json").status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_auto_renew_json_false_is_stored_false_on_create_and_patch(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        created = client.post(
            f"/v1/procurement/contracts/?entity={entity.code}",
            {"vendor": vendor.code, "title": "Strict flag", "auto_renew": False},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        contract = VendorContract.objects.get(reference=created.data["data"]["reference"])
        self.assertIs(contract.auto_renew, False)

        VendorContract.objects.filter(pk=contract.pk).update(auto_renew=True)
        updated = client.patch(
            f"/v1/procurement/contracts/{contract.pk}/?entity={entity.code}",
            {"auto_renew": False}, format="json",
        )
        self.assertEqual(updated.status_code, 200)
        contract.refresh_from_db()
        self.assertIs(contract.auto_renew, False)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_auto_renew_non_boolean_values_are_rejected_on_create_and_patch(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        client = self._client(entity)
        contract = self._contract(
            entity, vendor, start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
        )
        for invalid in ("false", "true", 0, 1, None):
            with self.subTest(method="POST", invalid=invalid):
                response = client.post(
                    f"/v1/procurement/contracts/?entity={entity.code}",
                    {"vendor": vendor.code, "title": "Invalid flag", "auto_renew": invalid},
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("auto_renew", response.data["error"]["detail"])
            with self.subTest(method="PATCH", invalid=invalid):
                response = client.patch(
                    f"/v1/procurement/contracts/{contract.pk}/?entity={entity.code}",
                    {"auto_renew": invalid}, format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("auto_renew", response.data["error"]["detail"])

    # --- summary / linked-pos / list --------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_summary_counts_are_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        today = datetime.date.today()
        self._contract(entity, vendor, ref="A", start=today - datetime.timedelta(days=30),
                       end=today + datetime.timedelta(days=200), value=10_000_000,
                       status=ContractStatus.ACTIVE)
        self._contract(entity, vendor, ref="EXP", start=today - datetime.timedelta(days=300),
                       end=today + datetime.timedelta(days=15), value=20_000_000,
                       status=ContractStatus.ACTIVE)   # expiring soon (also active)
        self._contract(entity, vendor, ref="OLD", start=today - datetime.timedelta(days=400),
                       end=today - datetime.timedelta(days=5), value=30_000_000,
                       status=ContractStatus.ACTIVE)   # active-but-past → expired
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        ov = Vendor.objects.create(entity=other, code="OV2", name="V",
                                   payable_account=Account.objects.get(entity=other, code="2100"),
                                   kyc_status="VERIFIED")
        self._contract(other, ov, ref="FOREIGN", start=today, end=today + datetime.timedelta(days=10),
                       status=ContractStatus.ACTIVE)
        data = self._client(entity).get(
            f"/v1/procurement/contracts/summary/?entity={entity.code}").data["data"]
        self.assertEqual(data["active"], 2)         # A + expiring (OLD is past → not active)
        self.assertEqual(data["expiring_soon"], 1)  # only EXP
        self.assertEqual(data["expired"], 1)        # OLD (active but past end)
        self.assertEqual(data["total_active_value"], 60_000_000)  # 10+20+30, all ACTIVE status

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_linked_pos_scoped_to_vendor_and_term(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other_vendor = Vendor.objects.create(
            entity=entity, code="OTHV", name="Other", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        c = self._contract(entity, vendor, start=datetime.date(2026, 1, 1),
                           end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE)
        inside = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])  # order_date 2026-01-05
        # Same vendor but a PO dated outside the term.
        outside = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])
        PurchaseOrder.objects.filter(pk=outside.pk).update(order_date=datetime.date(2027, 3, 1))
        # A PO for a different vendor inside the term.
        self.make_po(entity, other_vendor, [("5300", 1, 100_000, None)])
        rows = self._client(entity).get(
            f"/v1/procurement/contracts/{c.pk}/linked-pos/?entity={entity.code}").data["data"]
        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {inside.pk})  # only same-vendor, in-term

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_linked_pos_explicit_link_and_fallback(self, _perm):
        # An explicit call-off shows as "linked" (even dated outside the term); an
        # unlinked same-vendor PO in the term shows as "association"; and a PO linked
        # to a *different* overlapping contract of the same vendor never leaks in.
        entity, _, vendor, _, _ = self.build_p2p()
        c1 = self._contract(entity, vendor, ref="C-1", start=datetime.date(2026, 1, 1),
                            end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE)
        c2 = self._contract(entity, vendor, ref="C-2", start=datetime.date(2026, 1, 1),
                            end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE)
        linked = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])
        PurchaseOrder.objects.filter(pk=linked.pk).update(
            contract=c1, order_date=datetime.date(2030, 6, 1))  # explicit, outside term
        assoc = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])  # unlinked, in term
        other = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])
        PurchaseOrder.objects.filter(pk=other.pk).update(contract=c2)  # linked to c2, not c1
        rows = self._client(entity).get(
            f"/v1/procurement/contracts/{c1.pk}/linked-pos/?entity={entity.code}").data["data"]
        by_id = {r["id"]: r["link_type"] for r in rows}
        self.assertEqual(by_id, {linked.pk: "linked", assoc.pk: "association"})

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_po_contract_link_rejects_cross_vendor_and_non_active(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        other_vendor = Vendor.objects.create(
            entity=entity, code="OTHV2", name="Other", payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"), kyc_status="VERIFIED")
        active = self._contract(entity, vendor, ref="C-A", start=datetime.date(2026, 1, 1),
                                end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE)
        draft = self._contract(entity, vendor, ref="C-D", start=datetime.date(2026, 1, 1),
                               end=datetime.date(2026, 12, 31), status=ContractStatus.DRAFT)
        po = self.make_po(entity, vendor, [("5300", 1, 100_000, None)])
        po.status = DocumentStatus.DRAFT
        po.save(update_fields=["status", "updated_at"])
        client = self._client(entity)
        url = f"/v1/procurement/purchase-orders/{po.pk}/?entity={entity.code}"
        # A draft (non-ACTIVE) contract cannot be called off.
        self.assertEqual(client.patch(url, {"contract": "C-D"}, format="json").status_code, 400)
        # A contract owned by a different vendor is rejected.
        cross = VendorContract.objects.create(
            entity=entity, vendor=other_vendor, reference="C-X", title="x",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
            status=ContractStatus.ACTIVE)
        self.assertEqual(client.patch(url, {"contract": "C-X"}, format="json").status_code, 400)
        # An active same-vendor contract links, and clearing removes the link.
        self.assertEqual(client.patch(url, {"contract": "C-A"}, format="json").status_code, 200)
        po.refresh_from_db(); self.assertEqual(po.contract_id, active.pk)
        self.assertEqual(client.patch(url, {"contract": ""}, format="json").status_code, 200)
        po.refresh_from_db(); self.assertIsNone(po.contract_id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_list_expiring_filter_and_empty_shape(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        today = datetime.date.today()
        self._contract(entity, vendor, ref="SOON", start=today - datetime.timedelta(days=100),
                       end=today + datetime.timedelta(days=10), status=ContractStatus.ACTIVE)
        self._contract(entity, vendor, ref="LATER", start=today, end=today + datetime.timedelta(days=300),
                       status=ContractStatus.ACTIVE)
        client = self._client(entity)
        rows = client.get(
            f"/v1/procurement/contracts/?expiring=1&entity={entity.code}").data["data"]
        self.assertEqual([r["reference"] for r in rows], ["SOON"])
        # empty-list still serialises to a JSON array, not {}
        empty = client.get(
            f"/v1/procurement/contracts/?status=TERMINATED&entity={entity.code}").data["data"]
        self.assertEqual(empty, [])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_renewals_accepts_valid_within_days_horizon(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        as_of = datetime.date(2026, 6, 1)
        self._contract(
            entity, vendor, ref="DUE", start=datetime.date(2026, 1, 1),
            end=as_of + datetime.timedelta(days=10), status=ContractStatus.ACTIVE,
        )
        self._contract(
            entity, vendor, ref="LATER", start=datetime.date(2026, 1, 1),
            end=as_of + datetime.timedelta(days=20), status=ContractStatus.ACTIVE,
        )
        response = self._client(entity).get(
            f"/v1/procurement/contracts/renewals/?entity={entity.code}"
            f"&as_of={as_of.isoformat()}&within_days=15",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["reference"] for row in response.data["data"]], ["DUE"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_renewals_rejects_malformed_within_days(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        for invalid in ("abc", "1.5", ""):
            with self.subTest(invalid=invalid):
                response = client.get(
                    f"/v1/procurement/contracts/renewals/?entity={entity.code}"
                    f"&within_days={invalid}",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("within_days", response.data["error"]["detail"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_renewals_rejects_negative_and_excessive_within_days(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        for invalid in (-1, 3651):
            with self.subTest(invalid=invalid):
                response = client.get(
                    f"/v1/procurement/contracts/renewals/"
                    f"?entity={entity.code}&within_days={invalid}",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("within_days", response.data["error"]["detail"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_renew_creates_successor_and_marks_source_renewed(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        c = self._contract(entity, vendor, start=datetime.date(2026, 1, 1),
                           end=datetime.date(2026, 12, 31), status=ContractStatus.ACTIVE)
        r = self._client(entity).post(
            f"/v1/procurement/contracts/{c.pk}/renew/?entity={entity.code}",
            {"start_date": "2027-01-01", "end_date": "2027-12-31"}, format="json")
        self.assertEqual(r.status_code, 201)
        c.refresh_from_db()
        self.assertEqual(c.status, ContractStatus.RENEWED)
        self.assertTrue(r.data["data"]["reference"])          # successor got an auto ref
        self.assertEqual(r.data["data"]["renews_id"], c.pk)


# --------------------------------------------------------------------------- #
# AP cash-requirements forecast + GR/IR aging                                 #
# --------------------------------------------------------------------------- #

class CashForecastTests(_P2PFixtureMixin, TestCase):
    def test_open_bill_buckets_by_days_until_due(self):
        from vs_procurement.reports import ap_cash_requirements

        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(
            entity, vendor, [("5300", 1, 1_000_000, None, None)],
            date=datetime.date(2026, 1, 1),
        )
        vi.due_date = datetime.date(2026, 1, 10)
        vi.save(update_fields=["due_date", "updated_at"])
        post_vendor_invoice(vi)  # POSTED, due 2026-01-10

        # Five days before due → "0-7" window.
        fc = ap_cash_requirements(entity, as_of=datetime.date(2026, 1, 5))
        self.assertEqual(fc.total_due, 1_000_000)
        self.assertEqual(fc.bucket_totals["0-7"], 1_000_000)
        self.assertEqual(fc.rows[0].code, "ACME")

        # Past the due date → "overdue".
        fc2 = ap_cash_requirements(entity, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(fc2.bucket_totals["overdue"], 1_000_000)


class GRIRAgingTests(_P2PFixtureMixin, TestCase):
    def test_open_receipt_is_aged(self):
        from vs_procurement.reports import grir_aging, grir_balance

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 10)])
        post_grn(grn)  # received 2026-01-08, GR/IR credit 1,000,000

        report = grir_aging(entity, as_of=datetime.date(2026, 1, 10))
        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.open_value, 1_000_000)
        self.assertEqual(row.invoiced_value, 0)
        self.assertEqual(row.bucket, "1-30")
        self.assertEqual(report.total_open, 1_000_000)
        # Aging and control use the same signed normal-balance convention.
        self.assertEqual(report.total_open, grir_balance(entity))
        self.assertEqual(report.difference, 0)

    def test_invoice_heavy_linked_receipt_reconciles_as_a_signed_debit_position(self):
        from vs_procurement.reports import grir_aging

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.get()
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        post_grn(grn)  # Cr GR/IR 1,000,000
        invoice = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 10),
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=invoice, po_line=po_line, grn_line=grn.lines.get(),
            expense_account=self.acc(entity, "5100"),
            quantity=15, unit_price=100_000, line_no=1,
        )
        # The explicit override posts a legitimate over-bill and clears 1,500,000
        # against the linked receipt basis, leaving GR/IR net debit by 500,000.
        post_vendor_invoice(invoice, allow_variance=True)

        report = grir_aging(entity, as_of=datetime.date(2026, 1, 12))

        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.rows[0].grn_id, grn.pk)
        self.assertEqual(report.rows[0].open_value, -500_000)
        self.assertEqual(report.rows[0].bucket, "1-30")
        self.assertEqual(report.bucket_totals["1-30"], -500_000)
        self.assertEqual(report.total_open, -500_000)
        self.assertEqual(report.control_balance, -500_000)
        self.assertEqual(report.difference, 0)

    def test_matched_invoice_clears_the_grir_row(self):
        from vs_procurement.reports import grir_aging

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        po_line = po.lines.first()
        grn = self.make_grn(entity, vendor, po, [(po_line, 10)])
        post_grn(grn)
        grn_line = grn.lines.first()

        vi = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 10),
            # Posting now requires completed approval; this inline bill mirrors make_bill.
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=vi, po_line=po_line, grn_line=grn_line,
            expense_account=self.acc(entity, "5100"), quantity=10,
            unit_price=100_000, line_no=1,
        )
        post_vendor_invoice(vi)  # clears GR/IR

        report = grir_aging(entity, as_of=datetime.date(2026, 1, 12))
        self.assertEqual(report.rows, [])          # nothing open
        self.assertEqual(report.total_open, 0)
        self.assertEqual(report.difference, 0)


# --------------------------------------------------------------------------- #
# Procurement dashboard aggregate                                             #
# --------------------------------------------------------------------------- #

class PurchaseOrderConsoleDataTests(_P2PFixtureMixin, TestCase):
    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_draft_terms_can_update_but_pending_approval_is_locked(self, _permission):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5300", 4, 250_000, None)])
        po.status = DocumentStatus.DRAFT
        po.save(update_fields=["status", "updated_at"])
        original_lines = list(po.lines.values_list("id", flat=True))
        user = get_user_model().objects.create_user(
            email="po-edit@test.com", password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="PO", last_name="Editor",
        )
        client = TenantAPIClient(user=user)
        response = client.patch(
            f"/v1/procurement/purchase-orders/{po.id}/?entity={entity.code}",
            {"delivery_address": "12 Marina Road", "payment_terms": "Net 45"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        po.refresh_from_db()
        self.assertEqual(po.delivery_address, "12 Marina Road")
        self.assertEqual(po.payment_terms, "Net 45")
        self.assertEqual(list(po.lines.values_list("id", flat=True)), original_lines)

        # PO workflow submission uses the approval overlay while its base document can remain DRAFT.
        po.approval_state = ProcApprovalState.PENDING
        po.save(update_fields=["approval_state", "updated_at"])
        locked = client.patch(
            f"/v1/procurement/purchase-orders/{po.id}/?entity={entity.code}",
            {"payment_terms": "Immediate"}, format="json",
        )
        self.assertEqual(locked.status_code, 400)

    def test_summary_and_partial_filter_use_derived_receipt_progress(self):
        from vs_procurement.views.orders import (
            _filter_purchase_orders,
            _purchase_order_queryset,
            purchase_order_summary,
        )

        entity, _, vendor, _, _ = self.build_p2p()
        partial = self.make_po(entity, vendor, [("5300", 10, 100_000, None)])
        partial.status = DocumentStatus.APPROVED
        partial.save(update_fields=["status", "updated_at"])
        partial_line = partial.lines.first()
        partial_line.received_qty = 4
        partial_line.save(update_fields=["received_qty", "updated_at"])

        received = self.make_po(entity, vendor, [("5300", 5, 100_000, None)])
        received.status = DocumentStatus.APPROVED
        received.save(update_fields=["status", "updated_at"])
        received_line = received.lines.first()
        received_line.received_qty = received_line.quantity
        received_line.save(update_fields=["received_qty", "updated_at"])

        awaiting = self.make_po(entity, vendor, [("5300", 2, 100_000, None)])
        awaiting.status = DocumentStatus.APPROVED
        awaiting.save(update_fields=["status", "updated_at"])

        # A draft and an in-approval order are NOT issued commitments: they must not
        # inflate any KPI, even though the draft has zero received quantity.
        draft = self.make_po(entity, vendor, [("5300", 7, 100_000, None)])
        draft.status = DocumentStatus.DRAFT
        draft.save(update_fields=["status", "updated_at"])
        pending = self.make_po(entity, vendor, [("5300", 9, 100_000, None)])
        pending.status = DocumentStatus.PENDING_APPROVAL
        pending.save(update_fields=["status", "updated_at"])

        summary = purchase_order_summary(entity, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(summary["open"], {"count": 2, "amount": 1_200_000})
        self.assertEqual(summary["partially_received"], {"count": 1})
        self.assertEqual(summary["awaiting_receipt"], {"count": 1})
        self.assertEqual(summary["po_value_mtd"]["amount"], 1_700_000)
        self.assertIsNone(summary["po_value_mtd"]["change_pct"])

        partial_rows = _filter_purchase_orders(
            _purchase_order_queryset(entity), {"status": "PARTIAL"},
        )
        self.assertEqual(list(partial_rows.values_list("id", flat=True)), [partial.id])

    def test_summary_endpoint_is_permission_gated_and_entity_scoped(self):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient
        from vs_procurement.views.orders import purchase_order_summary

        entity, _, vendor, _, _ = self.build_p2p()
        issued = self.make_po(entity, vendor, [("5300", 4, 250_000, None)])
        issued.status = DocumentStatus.APPROVED
        issued.save(update_fields=["status", "updated_at"])

        # Another entity's issued PO must never contribute to this entity's KPIs.
        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER-PO", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        seed_chart_of_accounts(other)
        other_vendor = Vendor.objects.create(
            entity=other, code="OTHER-V", name="Other Vendor",
            payable_account=self.acc(other, "2100"),
            default_expense_account=self.acc(other, "5300"),
        )
        other_po = self.make_po(other, other_vendor, [("5300", 100, 1_000_000, None)])
        other_po.status = DocumentStatus.APPROVED
        other_po.save(update_fields=["status", "updated_at"])

        summary = purchase_order_summary(entity, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(summary["open"], {"count": 1, "amount": 1_000_000})

        # No procurement grant → the endpoint is refused before any data is returned.
        user = get_user_model().objects.create_user(
            email="po-summary-no-grant@test.com", password="pw",
            user_type="CX_STAFF", status="ACTIVE", first_name="No", last_name="Grant",
        )
        response = TenantAPIClient(user=user).get(
            f"/v1/procurement/purchase-orders/summary/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 403)


class PurchaseOrderVendorEmailTests(_P2PFixtureMixin, TestCase):
    """Approved-only vendor delivery remains durable, scoped, and retryable."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.entity, _, self.vendor, _, _ = self.build_p2p()
        self.vendor.email = "fallback@vendor.test"
        self.vendor.address = "12 Vendor Road"
        self.vendor.save(update_fields=["email", "address", "updated_at"])
        self.user = get_user_model().objects.create_user(
            email="buyer-po-email@test.com", password="pw", tenant=self.entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Purchase", last_name="Buyer",
        )

    def _po(self, *, approved=True):
        po = self.make_po(self.entity, self.vendor, [("5300", 2, 250_000, None)])
        po.delivery_address = "1 Delivery Avenue"
        po.payment_terms = "Net 30"
        po.approval_state = ProcApprovalState.APPROVED if approved else ProcApprovalState.NOT_SUBMITTED
        po.status = DocumentStatus.APPROVED if approved else DocumentStatus.DRAFT
        po.save(update_fields=[
            "delivery_address", "payment_terms", "approval_state", "status", "updated_at",
        ])
        return po

    def test_schedule_uses_opted_in_contacts_and_cancels_on_rejection(self):
        from vs_procurement import po_email

        po = self._po(approved=False)
        VendorContact.objects.create(
            vendor=self.vendor, name="Quotes", email="quotes@vendor.test",
            is_primary=True, receives_rfqs=True, receives_purchase_orders=False,
        )
        VendorContact.objects.create(
            vendor=self.vendor, name="Orders", email="orders@vendor.test",
            receives_rfqs=False, receives_purchase_orders=True,
        )
        delivery = po_email.schedule_after_approval(
            po, actor_user=self.user, buyer_message="Please confirm receipt.",
        )
        self.assertEqual(delivery.status, PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL)
        self.assertEqual(delivery.recipients, ["orders@vendor.test"])
        self.assertEqual(delivery.cc, ["backend-test@codexng.com"])

        po_email.cancel_awaiting(po, reason="Approval was rejected.", actor_user=self.user)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, PurchaseOrderVendorDeliveryStatus.CANCELLED)
        self.assertIn("rejected", delivery.failure_reason)

    @patch("vs_procurement.po_email.send_notification", return_value=["notification-id"])
    def test_manual_send_builds_pdf_and_queues_only_approved_order(self, notify):
        from vs_procurement import po_email

        po = self._po(approved=True)
        delivery = po_email.send_approved(
            po, actor_user=self.user, buyer_message="Deliver to reception.",
        )
        self.addCleanup(delivery.pdf_file.delete, False)
        self.assertEqual(delivery.source, PurchaseOrderVendorDeliverySource.MANUAL)
        self.assertEqual(delivery.status, PurchaseOrderVendorDeliveryStatus.PENDING)
        self.assertTrue(delivery.pdf_file.name.endswith(".pdf"))
        self.assertGreater(delivery.pdf_file.size, 100)
        metadata = notify.call_args.kwargs["metadata"]
        self.assertEqual(metadata["cc"], ["backend-test@codexng.com"])
        self.assertEqual(metadata["attachments"][0]["storage_name"], delivery.pdf_file.name)

        draft = self._po(approved=False)
        with self.assertRaises(po_email.PurchaseOrderEmailError):
            po_email.send_approved(draft, actor_user=self.user)

    def test_email_preview_is_permission_gated_and_entity_scoped(self):
        from core.test_utils import TenantAPIClient

        po = self._po(approved=True)
        client = TenantAPIClient(user=self.user)
        denied = client.get(
            f"/v1/procurement/purchase-orders/{po.pk}/email-preview/?entity={self.entity.code}",
        )
        self.assertEqual(denied.status_code, 403)

        with patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True):
            missing = client.get(
                f"/v1/procurement/purchase-orders/{po.pk + 9999}/email-preview/?entity={self.entity.code}",
            )
        self.assertEqual(missing.status_code, 404)

    def test_full_approval_releases_scheduled_email_after_commit(self):
        from vs_procurement import approvals, po_email

        po = self._po(approved=False)
        delivery = po_email.schedule_after_approval(po, actor_user=self.user)
        with patch("vs_procurement.po_email.release_awaiting") as release:
            with self.captureOnCommitCallbacks(execute=True):
                approvals.apply_approved(po, actor_user=self.user)

        po.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(po.status, DocumentStatus.APPROVED)
        self.assertEqual(po.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(delivery.status, PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL)
        release.assert_called_once_with(po.pk, actor_user=self.user)

    def test_release_error_becomes_failed_delivery_without_changing_approval(self):
        from vs_procurement import po_email

        po = self._po(approved=False)
        delivery = po_email.schedule_after_approval(po, actor_user=self.user)
        po.status = DocumentStatus.APPROVED
        po.approval_state = ProcApprovalState.APPROVED
        po.save(update_fields=["status", "approval_state", "updated_at"])

        po_email._mark_release_failed(po.pk, RuntimeError("PDF renderer unavailable"), actor_user=self.user)

        po.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(po.status, DocumentStatus.APPROVED)
        self.assertEqual(delivery.status, PurchaseOrderVendorDeliveryStatus.FAILED)
        self.assertIn("PDF renderer unavailable", delivery.failure_reason)

class ProcurementDashboardTests(_P2PFixtureMixin, TestCase):
    def test_dashboard_activity_is_success_only_and_limited_to_five(self):
        from vs_procurement.dashboard import procurement_dashboard

        entity, _, _, _, _ = self.build_p2p()
        for index in range(6):
            FinanceAuditLog.objects.create(
                entity=entity,
                action=FinanceAuditAction.PURCHASE_ORDER_APPROVED,
                status=FinanceAuditStatus.SUCCESS,
                document_number=f"PO-SUCCESS-{index}",
            )
        FinanceAuditLog.objects.create(
            entity=entity,
            action=FinanceAuditAction.PURCHASE_ORDER_APPROVED,
            status=FinanceAuditStatus.FAILED,
            document_number="PO-FAILED",
        )

        activity = procurement_dashboard(entity)["recent_activity"]
        self.assertEqual(len(activity), 5)
        self.assertNotIn("PO-FAILED", {item["reference"] for item in activity})
        self.assertNotIn("PO-SUCCESS-0", {item["reference"] for item in activity})

    def test_dashboard_aggregates_real_data_and_excludes_other_entities(self):
        from vs_procurement.dashboard import procurement_dashboard

        entity, _, vendor, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(entity=entity, code="CLOUD", name="Cloud")
        vendor.category = category
        vendor.on_hold = True
        vendor.save(update_fields=["category", "on_hold", "updated_at"])

        po = self.make_po(entity, vendor, [("5300", 10, 100_000, None)])
        po.status = DocumentStatus.APPROVED
        po.approval_state = "APPROVED"
        po.save(update_fields=["status", "approval_state", "updated_at"])
        line = po.lines.first()
        line.received_qty = 4
        line.save(update_fields=["received_qty", "updated_at"])
        received_po = self.make_po(entity, vendor, [("5300", 3, 100_000, None)])
        received_po.status = DocumentStatus.APPROVED
        received_po.approval_state = "APPROVED"
        received_po.save(update_fields=["status", "approval_state", "updated_at"])
        received_line = received_po.lines.first()
        received_line.received_qty = received_line.quantity
        received_line.save(update_fields=["received_qty", "updated_at"])

        bill = self.make_bill(
            entity, vendor, [("5300", 1, 2_000_000, None, None)],
            date=datetime.date(2026, 1, 5),
        )
        bill.status = DocumentStatus.POSTED
        bill.due_date = datetime.date(2026, 1, 10)
        bill.amount_paid = 500_000
        bill.subtotal = 2_000_000
        bill.tax_total = 0
        bill.total = 2_000_000
        bill.save(update_fields=[
            "status", "due_date", "amount_paid", "subtotal", "tax_total", "total", "updated_at",
        ])

        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        seed_chart_of_accounts(other)
        other_vendor = Vendor.objects.create(
            entity=other, code="OTHER-V", name="Other Vendor",
            payable_account=self.acc(other, "2100"),
            default_expense_account=self.acc(other, "5300"),
        )
        other_bill = self.make_bill(
            other, other_vendor, [("5300", 1, 99_000_000, None, None)],
            date=datetime.date(2026, 1, 5),
        )
        other_bill.status = DocumentStatus.POSTED
        other_bill.subtotal = 99_000_000
        other_bill.tax_total = 0
        other_bill.total = 99_000_000
        other_bill.save(update_fields=["status", "subtotal", "tax_total", "total", "updated_at"])

        data = procurement_dashboard(entity, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(data["kpis"]["total_spend_mtd"]["value"]["kobo"], 2_000_000)
        self.assertEqual(data["kpis"]["open_purchase_orders"], {"count": 1, "partial_count": 1})
        self.assertEqual(
            {item["key"]: item["count"] for item in data["purchase_order_status"]["items"]},
            {"APPROVED": 0, "PARTIAL": 1, "PENDING": 0, "DRAFT": 0, "RECEIVED": 1},
        )
        self.assertEqual(data["kpis"]["overdue_invoices"]["count"], 1)
        self.assertEqual(data["kpis"]["overdue_invoices"]["amount"]["kobo"], 1_500_000)
        self.assertEqual(data["kpis"]["active_vendors"]["on_hold_count"], 1)
        self.assertEqual(data["spend_by_category"]["items"][0]["label"], "Cloud")
        self.assertEqual(data["monthly_spend_trend"]["values"][-1], 2_000_000)
        self.assertEqual(len(data["monthly_spend_trend"]["labels"]), 8)

    def test_dashboard_approval_cards_are_actor_and_entity_scoped(self):
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone
        from vs_procurement.dashboard import procurement_dashboard
        from vs_procurement.constants import WF_DOCTYPE_REQUISITION
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStage, WorkflowStageApprover,
            WorkflowStageInstance, WorkflowTemplate,
        )

        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        User = get_user_model()
        requester = User.objects.create_user(
            email="dash-requester@test.com", user_type="CX_STAFF", status="ACTIVE",
            first_name="Dash", last_name="Requester",
        )
        approver = User.objects.create_user(
            email="dash-approver@test.com", user_type="CX_STAFF", status="ACTIVE",
            first_name="Dash", last_name="Approver",
        )
        stranger = User.objects.create_user(
            email="dash-stranger@test.com", user_type="CX_STAFF", status="ACTIVE",
            first_name="Dash", last_name="Stranger",
        )
        template = WorkflowTemplate.objects.create(
            tenant=entity.tenant, document_type=WF_DOCTYPE_REQUISITION,
            code="dashboard-test", name="Dashboard test",
        )
        stage = WorkflowStage.objects.create(
            template=template, code="manager", label="Manager approval",
        )
        content_type = ContentType.objects.get_for_model(PurchaseRequisition)

        def pending_for(target_entity, suffix):
            req = PurchaseRequisition.objects.create(
                entity=target_entity, request_date=datetime.date(2026, 1, 5),
                requested_by=requester, justification=f"Request {suffix}",
                estimated_total=1_000_000,
            )
            instance = WorkflowInstance.all_objects.create(
                tenant=target_entity.tenant, template=template,
                document_content_type=content_type, document_object_id=str(req.pk),
                document_type=WF_DOCTYPE_REQUISITION, status="IN_PROGRESS",
                requested_by=requester, current_stage=stage,
            )
            active = WorkflowStageInstance.objects.create(
                instance=instance, stage=stage, status="ACTIVE", activated_at=timezone.now(),
            )
            WorkflowStageApprover.objects.create(stage_instance=active, user=approver, attempt=1)
            return req

        own = pending_for(entity, "own")
        pending_for(other, "other")

        data = procurement_dashboard(entity, user=approver, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(data["kpis"]["pending_approvals"]["count"], 1)
        self.assertEqual(data["approvals_awaiting_user"][0]["document_id"], own.pk)
        self.assertEqual(
            procurement_dashboard(entity, user=stranger, as_of=datetime.date(2026, 1, 20))
            ["approvals_awaiting_user"],
            [],
        )

    def test_dashboard_includes_vendor_payment_workflows(self):
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone
        from vs_procurement.constants import WF_DOCTYPE_VENDOR_PAYMENT
        from vs_procurement.dashboard import procurement_dashboard
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStage, WorkflowStageApprover,
            WorkflowStageInstance, WorkflowTemplate,
        )

        entity, _, vendor, _, _ = self.build_p2p()
        User = get_user_model()
        requester = User.objects.create_user(
            email="dash-pay-requester@test.com", user_type="CX_STAFF", status="ACTIVE",
            first_name="Pay", last_name="Requester",
        )
        approver = User.objects.create_user(
            email="dash-pay-approver@test.com", user_type="CX_STAFF", status="ACTIVE",
            first_name="Pay", last_name="Approver",
        )
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 10),
            gross_amount=1_000_000, net_amount=1_000_000,
            approval_state=ProcApprovalState.PENDING,
        )
        template = WorkflowTemplate.objects.create(
            tenant=entity.tenant, document_type=WF_DOCTYPE_VENDOR_PAYMENT,
            code="dashboard-payment", name="Dashboard payment",
        )
        stage = WorkflowStage.objects.create(
            template=template, code="manager", label="Manager approval",
        )
        instance = WorkflowInstance.all_objects.create(
            tenant=entity.tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(VendorPayment),
            document_object_id=str(payment.pk), document_type=WF_DOCTYPE_VENDOR_PAYMENT,
            status="IN_PROGRESS", requested_by=requester, current_stage=stage,
        )
        active = WorkflowStageInstance.objects.create(
            instance=instance, stage=stage, status="ACTIVE", activated_at=timezone.now(),
        )
        WorkflowStageApprover.objects.create(stage_instance=active, user=approver, attempt=1)

        data = procurement_dashboard(entity, user=approver, as_of=datetime.date(2026, 1, 20))
        self.assertEqual(data["kpis"]["pending_approvals"]["count"], 1)
        self.assertEqual(
            data["approvals_awaiting_user"][0]["document_type"],
            WF_DOCTYPE_VENDOR_PAYMENT,
        )

    def test_dashboard_endpoint_requires_report_permission(self):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        entity, _, _, _, _ = self.build_p2p()
        user = get_user_model().objects.create_user(
            email="dashboard-no-grant@test.com", password="pw",
            user_type="CX_STAFF", status="ACTIVE", first_name="No", last_name="Grant",
        )
        response = TenantAPIClient(user=user).get(
            f"/v1/procurement/reports/dashboard/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 403)

class WorkflowApprovalTests(_P2PFixtureMixin, TestCase):
    """Spend approvals are routed through vs_workflow (threshold-gated stages).

    Covers: default-template provisioning + idempotency, the auto-skip path (no
    eligible approvers → the requisition is approved synchronously on submit),
    threshold gating of the senior stage, a real APPROVED vote driving the document
    to APPROVED, and a REJECTED vote cancelling the requisition.
    """

    # -- helpers ------------------------------------------------------------- #

    @staticmethod
    def _user(email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, user_type="CX_STAFF", first_name="T", last_name="U",
        )

    def _make_requisition(self, entity, *, unit_price, qty=1):
        from vs_procurement.models import PurchaseRequisition, PurchaseRequisitionLine

        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 3),
            requested_by=self._user(f"req-{unit_price}-{qty}@t.com"),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=req, line_no=1, description="thing",
            quantity=qty, estimated_unit_price=unit_price,
            expense_account=self.acc(entity, "5300"),
        )
        req.recompute_total(save=True)
        return req

    # -- template provisioning ---------------------------------------------- #

    def test_default_templates_are_provisioned_idempotently(self):
        from vs_workflow.models import WorkflowStage, WorkflowTemplate
        from vs_procurement.approvals import ensure_default_approval_templates
        from vs_procurement.constants import (
            WF_DEFAULT_TEMPLATE_CODE, WF_DOCTYPE_REQUISITION,
        )

        first = ensure_default_approval_templates()
        self.assertEqual(len(first), 4)
        # One platform-wide template per approvable document type.
        self.assertEqual(
            WorkflowTemplate.objects.filter(
                tenant__isnull=True, branch__isnull=True,
                code=WF_DEFAULT_TEMPLATE_CODE,
            ).count(),
            4,
        )
        req_tmpl = WorkflowTemplate.objects.get(
            document_type=WF_DOCTYPE_REQUISITION, code=WF_DEFAULT_TEMPLATE_CODE,
            tenant__isnull=True, branch__isnull=True,
        )
        stages = list(WorkflowStage.objects.filter(template=req_tmpl).order_by("order"))
        self.assertEqual([s.code for s in stages], ["manager", "senior"])
        # The senior stage is threshold-gated on the document's amount field.
        self.assertEqual(stages[1].inclusion_condition.get("op"), "gte")
        self.assertEqual(stages[1].inclusion_condition.get("field"), "estimated_total")

        # Re-running upserts in place - still exactly four templates / two stages.
        ensure_default_approval_templates()
        self.assertEqual(
            WorkflowTemplate.objects.filter(code=WF_DEFAULT_TEMPLATE_CODE).count(), 4,
        )
        self.assertEqual(WorkflowStage.objects.filter(template=req_tmpl).count(), 2)

    # -- auto-skip (no approvers) ------------------------------------------- #

    def test_submit_without_approvers_auto_approves(self):
        from vs_workflow.constants import WorkflowInstanceStatus
        from vs_procurement.approvals import (
            ensure_default_approval_templates, submit_for_approval,
        )
        from vs_procurement.constants import ProcApprovalState

        entity, _, _, _, _ = self.build_p2p()
        ensure_default_approval_templates()
        req = self._make_requisition(entity, unit_price=10_000)  # below threshold
        actor = self._user("actor@t.com")

        instance = submit_for_approval(req, actor_user=actor)
        req.refresh_from_db()
        # Both stages skip (no eligible approvers) → terminal APPROVED on submit.
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(req.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(req.status, DocumentStatus.APPROVED)

    def test_double_submit_is_rejected(self):
        from vs_procurement.approvals import (
            ensure_default_approval_templates, submit_for_approval,
        )
        from vs_procurement.exceptions import ApprovalWorkflowError

        entity, _, _, _, _ = self.build_p2p()
        ensure_default_approval_templates()
        req = self._make_requisition(entity, unit_price=10_000)
        actor = self._user("actor2@t.com")
        submit_for_approval(req, actor_user=actor)  # → APPROVED
        with self.assertRaises(ApprovalWorkflowError):
            submit_for_approval(req, actor_user=actor)

    # -- real votes (mocked approver resolution) ---------------------------- #

    def test_manager_vote_approves_low_value_requisition(self):
        from unittest.mock import patch

        from vs_workflow.constants import (
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum,
        )
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.services.approvers import EligibleApprover
        from vs_procurement.approvals import (
            ensure_default_approval_templates, submit_for_approval,
        )
        from vs_procurement.constants import ProcApprovalState

        entity, _, _, _, _ = self.build_p2p()
        ensure_default_approval_templates()
        req = self._make_requisition(entity, unit_price=10_000)  # below threshold
        actor = self._user("requester@t.com")
        manager = self._user("manager@t.com")

        with patch(
            "vs_workflow.services.approvers.resolve_approvers",
            return_value=[EligibleApprover(user=manager)],
        ):
            instance = submit_for_approval(req, actor_user=actor)
            # Only the manager stage runs (senior excluded below threshold).
            self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
            wf_actions.record_action(instance.id, manager, ActionEnum.APPROVED)

        instance.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(req.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(req.status, DocumentStatus.APPROVED)

    def test_high_value_requisition_escalates_to_senior_stage(self):
        from unittest.mock import patch

        from vs_workflow.constants import (
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum,
        )
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.services.approvers import EligibleApprover
        from vs_procurement.approvals import (
            ensure_default_approval_templates, submit_for_approval,
        )
        from vs_procurement.constants import (
            ProcApprovalState, WF_DEFAULT_SENIOR_THRESHOLD,
        )

        entity, _, _, _, _ = self.build_p2p()
        ensure_default_approval_templates()
        # Above the senior threshold → the senior stage is included.
        req = self._make_requisition(entity, unit_price=WF_DEFAULT_SENIOR_THRESHOLD + 100)
        actor = self._user("requester2@t.com")
        manager = self._user("manager2@t.com")

        with patch(
            "vs_workflow.services.approvers.resolve_approvers",
            return_value=[EligibleApprover(user=manager)],
        ):
            instance = submit_for_approval(req, actor_user=actor)
            # Manager approves → not terminal: must escalate to the senior stage.
            wf_actions.record_action(instance.id, manager, ActionEnum.APPROVED)
            instance.refresh_from_db()
            self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
            req.refresh_from_db()
            self.assertEqual(req.approval_state, ProcApprovalState.PENDING)
            # Senior approves → terminal APPROVED.
            wf_actions.record_action(instance.id, manager, ActionEnum.APPROVED)

        instance.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(req.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(req.status, DocumentStatus.APPROVED)

    def test_rejection_cancels_the_requisition(self):
        from unittest.mock import patch

        from vs_workflow.constants import (
            WorkflowInstanceStatus, WorkflowStageAction as ActionEnum,
        )
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.services.approvers import EligibleApprover
        from vs_procurement.approvals import (
            ensure_default_approval_templates, submit_for_approval,
        )
        from vs_procurement.constants import ProcApprovalState

        entity, _, _, _, _ = self.build_p2p()
        ensure_default_approval_templates()
        req = self._make_requisition(entity, unit_price=10_000)
        actor = self._user("requester3@t.com")
        manager = self._user("manager3@t.com")

        with patch(
            "vs_workflow.services.approvers.resolve_approvers",
            return_value=[EligibleApprover(user=manager)],
        ):
            instance = submit_for_approval(req, actor_user=actor)
            wf_actions.record_action(
                instance.id, manager, ActionEnum.REJECTED, comment="no budget",
            )

        instance.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.REJECTED)
        self.assertEqual(req.approval_state, ProcApprovalState.REJECTED)
        self.assertEqual(req.status, DocumentStatus.CANCELLED)


class ProcurementApprovalQueueTests(_P2PFixtureMixin, TestCase):
    """The Procurement inbox narrows generic workflows to actor + ledger entity."""

    def _user(self, entity, email, first_name):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE",
            first_name=first_name, last_name="Tester",
        )

    def _pending(self, entity, *, requester, approver, document_type, document,
                 on_behalf_of=None, code="queue-test"):
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStage, WorkflowStageApprover,
            WorkflowStageInstance, WorkflowTemplate,
        )

        template = WorkflowTemplate.objects.create(
            tenant=entity.tenant, document_type=document_type,
            code=code, name="Queue test",
        )
        stage = WorkflowStage.objects.create(
            template=template, code="manager", label="Manager approval",
            advance_rule="ANY", on_rejection="TERMINAL",
        )
        instance = WorkflowInstance.all_objects.create(
            tenant=entity.tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(type(document)),
            document_object_id=str(document.pk), document_type=document_type,
            status="IN_PROGRESS", requested_by=requester, current_stage=stage,
            submitted_at=timezone.now(),
        )
        stage_instance = WorkflowStageInstance.objects.create(
            instance=instance, stage=stage, status="ACTIVE",
            activated_at=timezone.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=stage_instance, user=approver,
            on_behalf_of=on_behalf_of, attempt=stage_instance.attempt,
        )
        return instance, stage_instance

    def test_queue_is_actor_entity_and_document_family_scoped(self):
        from core.test_utils import TenantAPIClient
        from vs_procurement.constants import (
            WF_DOCTYPE_REQUISITION, WF_DOCTYPE_VENDOR_PAYMENT,
        )

        entity, _, vendor, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(
            name="Other Books", code="QOTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        requester = self._user(entity, "queue-requester@test.com", "Requester")
        approver = self._user(entity, "queue-approver@test.com", "Approver")
        stranger = self._user(entity, "queue-stranger@test.com", "Stranger")
        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 5),
            requested_by=requester, title="Server refresh", estimated_total=2_500_000,
            status=DocumentStatus.PENDING_APPROVAL, approval_state=ProcApprovalState.PENDING,
        )
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 10),
            gross_amount=1_000_000, net_amount=1_000_000,
            approval_state=ProcApprovalState.PENDING,
        )
        other_req = PurchaseRequisition.objects.create(
            entity=other, request_date=datetime.date(2026, 1, 5),
            requested_by=requester, title="Foreign request", estimated_total=3_000_000,
            status=DocumentStatus.PENDING_APPROVAL, approval_state=ProcApprovalState.PENDING,
        )
        own_instance, _ = self._pending(
            entity, requester=requester, approver=approver,
            document_type=WF_DOCTYPE_REQUISITION, document=req, code="own",
        )
        payment_instance, _ = self._pending(
            entity, requester=requester, approver=approver,
            document_type=WF_DOCTYPE_VENDOR_PAYMENT, document=payment, code="payment",
            on_behalf_of=requester,
        )
        self._pending(
            other, requester=requester, approver=approver,
            document_type=WF_DOCTYPE_REQUISITION, document=other_req, code="other",
        )
        self._pending(
            entity, requester=requester, approver=approver,
            document_type="finance.journal", document=req, code="non-proc",
        )

        response = TenantAPIClient(user=approver).get(
            f"/v1/procurement/approvals/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertEqual({row["id"] for row in rows}, {own_instance.id, payment_instance.id})
        self.assertEqual({row["document_type"] for row in rows}, {
            WF_DOCTYPE_REQUISITION, WF_DOCTYPE_VENDOR_PAYMENT,
        })
        delegated = next(row for row in rows if row["id"] == payment_instance.id)
        self.assertEqual(delegated["on_behalf_of"], "Requester Tester")
        self.assertEqual(
            TenantAPIClient(user=stranger).get(
                f"/v1/procurement/approvals/?entity={entity.code}",
            ).json()["data"],
            [],
        )

    def test_detail_hides_foreign_targets_and_raw_audit_context(self):
        from core.test_utils import TenantAPIClient
        from vs_procurement.constants import WF_DOCTYPE_REQUISITION
        from vs_workflow.models import WorkflowAuditLog

        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(
            name="Other Detail Books", code="QDOTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant,
        )
        requester = self._user(entity, "detail-requester@test.com", "Requester")
        approver = self._user(entity, "detail-approver@test.com", "Approver")
        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 5),
            requested_by=requester, title="Safe request", estimated_total=2_500_000,
            status=DocumentStatus.PENDING_APPROVAL, approval_state=ProcApprovalState.PENDING,
        )
        instance, _ = self._pending(
            entity, requester=requester, approver=approver,
            document_type=WF_DOCTYPE_REQUISITION, document=req,
        )
        WorkflowAuditLog.objects.create(
            instance=instance, event_type="INSTANCE_SUBMITTED", actor=requester,
            message="Submitted for approval.", context={"secret": "never-return-this"},
        )
        client = TenantAPIClient(user=approver)
        detail = client.get(
            f"/v1/procurement/approvals/{instance.id}/?entity={entity.code}",
        )
        self.assertEqual(detail.status_code, 200)
        activity = detail.json()["data"]["activity"]
        self.assertEqual(activity[0]["message"], "Submitted for approval.")
        self.assertNotIn("context", activity[0])
        self.assertNotIn("never-return-this", str(detail.json()))
        self.assertEqual(
            client.get(
                f"/v1/procurement/approvals/{instance.id}/?entity={other.code}",
            ).status_code,
            404,
        )

    def test_eligible_actor_can_approve_without_manage_permission(self):
        from core.test_utils import TenantAPIClient
        from vs_procurement.constants import WF_DOCTYPE_REQUISITION

        entity, _, _, _, _ = self.build_p2p()
        requester = self._user(entity, "vote-requester@test.com", "Requester")
        approver = self._user(entity, "vote-approver@test.com", "Approver")
        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 5),
            requested_by=requester, title="Approve me", estimated_total=2_500_000,
            status=DocumentStatus.PENDING_APPROVAL, approval_state=ProcApprovalState.PENDING,
        )
        instance, _ = self._pending(
            entity, requester=requester, approver=approver,
            document_type=WF_DOCTYPE_REQUISITION, document=req,
        )
        response = TenantAPIClient(user=approver).post(
            f"/v1/procurement/approvals/{instance.id}/actions/?entity={entity.code}",
            {"action": "APPROVED", "comment": "Within budget."}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(req.status, DocumentStatus.APPROVED)

    def test_requester_cannot_approve_own_document(self):
        from core.test_utils import TenantAPIClient
        from vs_procurement.constants import WF_DOCTYPE_REQUISITION

        entity, _, _, _, _ = self.build_p2p()
        requester = self._user(entity, "self-requester@test.com", "Requester")
        req = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 5),
            requested_by=requester, title="Self approval", estimated_total=2_500_000,
            status=DocumentStatus.PENDING_APPROVAL, approval_state=ProcApprovalState.PENDING,
        )
        instance, _ = self._pending(
            entity, requester=requester, approver=requester,
            document_type=WF_DOCTYPE_REQUISITION, document=req,
        )
        response = TenantAPIClient(user=requester).post(
            f"/v1/procurement/approvals/{instance.id}/actions/?entity={entity.code}",
            {"action": "APPROVED"}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.approval_state, ProcApprovalState.PENDING)


class StockLedgerTests(_P2PFixtureMixin, TestCase):
    """Perpetual inventory at weighted-average cost: receipt, issue, adjustment."""

    def _stock_item(self, entity, *, code="WIDGET", reorder_level=0, reorder_qty=0):
        return StockItem.objects.create(
            entity=entity, code=code, name=f"Stock {code}",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5100"),
            reorder_level=reorder_level, reorder_qty=reorder_qty,
        )

    def _receive_via_grn(self, entity, vendor, item, *, qty, unit_price, cost_center=None,
                         received_date=datetime.date(2026, 1, 8)):
        """Build & post a single-line stock GRN, returning the posted GRN."""
        po = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2026, 1, 5),
            status=DocumentStatus.APPROVED,
        )
        po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, description=item.name,
            expense_account=self.acc(entity, "5100"), quantity=qty,
            unit_price=unit_price, cost_center=cost_center, line_no=1,
        )
        from vs_procurement.purchasing import price_po
        price_po(po)
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po, received_date=received_date,
        )
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, stock_item=item,
            expense_account=self.acc(entity, "5100"),
            cost_center=cost_center,
            accepted_qty=qty, unit_price=unit_price, line_no=1,
        )
        post_grn(grn)
        item.refresh_from_db()      # GRN posted against a fresh instance; sync the caller's
        return grn

    def test_grn_into_stock_capitalises_to_inventory(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        grn = self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)

        # GL: Dr inventory (1400), Cr GR/IR (2150) - not the line expense account.
        lines = {l.account.code: l for l in grn.journal.lines.all()}
        self.assertEqual(lines["1400"].debit, 1_000_000)
        self.assertEqual(lines["2150"].credit, 1_000_000)
        self.assertNotIn("5100", lines)

        # Sub-ledger raised; a RECEIPT movement snapshots the running balance.
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 10)
        self.assertEqual(item.stock_value, 1_000_000)
        self.assertEqual(item.unit_cost, 100_000)
        movement = item.movements.get()
        self.assertEqual(movement.movement_type, "RECEIPT")
        self.assertEqual(movement.balance_qty, 10)
        self.assertEqual(movement.balance_value, 1_000_000)
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.STOCK_RECEIVED,
            target_type="StockItem", target_id=str(item.pk),
        )
        self.assertIn("Received 10", audit.message)
        self.assertEqual(audit.metadata["value"], 1_000_000)
        self.assertEqual(audit.metadata["journal_id"], grn.journal_id)
        self.assertEqual(audit.metadata["grn_id"], grn.pk)
        self.assertEqual(audit.metadata["movement_id"], movement.pk)

    def test_duplicate_stock_grn_post_adds_no_movement_or_receipt_audit(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        grn = self._receive_via_grn(entity, vendor, item, qty=2, unit_price=100_000)

        with self.assertRaises(PostingError):
            post_grn(grn)

        self.assertEqual(item.movements.count(), 1)
        self.assertEqual(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.STOCK_RECEIVED,
            target_type="StockItem", target_id=str(item.pk),
        ).count(), 1)

    def test_two_stock_receipt_lines_each_keep_movement_and_audit_evidence(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        po = self.make_po(
            entity, vendor,
            [("5100", 1, 100_000, None), ("5100", 2, 75_000, None)],
        )
        grn = self.make_grn(
            entity, vendor, po,
            [(po.lines.order_by("line_no")[0], 1), (po.lines.order_by("line_no")[1], 2)],
        )
        grn.lines.update(stock_item=item)

        post_grn(grn)

        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 3)
        self.assertEqual(item.stock_value, 250_000)
        self.assertEqual(item.movements.filter(movement_type="RECEIPT").count(), 2)
        self.assertEqual(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.STOCK_RECEIVED,
            target_type="StockItem", target_id=str(item.pk),
        ).count(), 2)

    def test_stock_receipt_drops_cost_center_from_inventory_control_debit(self):
        from vs_finance.models import CostCenter

        entity, _, vendor, _, _ = self.build_p2p()
        center = CostCenter.objects.create(entity=entity, code="OPS", name="Operations")
        grn = self._receive_via_grn(
            entity, vendor, self._stock_item(entity),
            qty=1, unit_price=100_000, cost_center=center,
        )
        self.assertIsNone(grn.journal.lines.get(account__code="1400").cost_center_id)
        self.assertIsNone(grn.journal.lines.get(account__code="2150").cost_center_id)

    def test_weighted_average_cost_blends_two_lots(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)  # 10 @ 1000.00
        self._receive_via_grn(entity, vendor, item, qty=10, unit_price=200_000)  # 10 @ 2000.00

        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 20)
        self.assertEqual(item.stock_value, 3_000_000)
        self.assertEqual(item.unit_cost, 150_000)            # weighted average 1500.00

    def test_issue_posts_dr_expense_cr_inventory_at_average(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)

        movement = issue_stock(
            item, quantity=4, movement_date=datetime.date(2026, 1, 12),
        )
        lines = {l.account.code: l for l in movement.journal.lines.all()}
        self.assertEqual(lines["5100"].debit, 400_000)       # Dr expense at average
        self.assertEqual(lines["1400"].credit, 400_000)      # Cr inventory relief

        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 6)
        self.assertEqual(item.stock_value, 600_000)
        self.assertEqual(movement.movement_type, "ISSUE")
        self.assertEqual(movement.quantity, -4)
        self.assertEqual(movement.value_amount, -400_000)
        message = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.STOCK_ISSUED,
        ).message
        self.assertIn("₦", message)
        self.assertNotIn("kobo", message.lower())

    def test_issue_beyond_on_hand_is_blocked_and_audited(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        self._receive_via_grn(entity, vendor, item, qty=5, unit_price=100_000)

        with self.assertRaises(InsufficientStockError):
            issue_stock(item, quantity=8, movement_date=datetime.date(2026, 1, 12))

        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 5)                # unchanged
        self.assertTrue(
            FinanceAuditLog.objects.filter(
                entity=entity, action="STOCK_ISSUE_REJECTED",
                status=FinanceAuditStatus.FAILED,
            ).exists()
        )

    def test_issue_clears_value_exactly_when_emptying_stock(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        self._receive_via_grn(entity, vendor, item, qty=3, unit_price=100_000)

        issue_stock(item, quantity=1, movement_date=datetime.date(2026, 1, 12))
        issue_stock(item, quantity=2, movement_date=datetime.date(2026, 1, 13))

        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 0)
        self.assertEqual(item.stock_value, 0)               # no residual drift

    def test_stale_issue_and_adjustment_instances_use_locked_current_balances(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        stale_issue = StockItem.objects.get(pk=item.pk)     # snapshots the empty balance
        stale_adjustment = StockItem.objects.get(pk=item.pk)
        self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)

        issue = issue_stock(
            stale_issue, quantity=4, movement_date=datetime.date(2026, 1, 12),
        )
        adjustment = adjust_stock(
            stale_adjustment, quantity_delta=-2,
            movement_date=datetime.date(2026, 1, 13),
        )

        self.assertEqual(issue.balance_qty, 6)
        self.assertEqual(issue.balance_value, 600_000)
        self.assertEqual(adjustment.balance_qty, 4)
        self.assertEqual(adjustment.balance_value, 400_000)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 4)
        self.assertEqual(item.stock_value, 400_000)

    def test_stock_rounding_is_half_up_for_unit_cost_issues_and_writeups(self):
        entity, _, _, _, _ = self.build_p2p()

        derived = self._stock_item(entity, code="ROUND-UNIT")
        StockItem.objects.filter(pk=derived.pk).update(on_hand_qty=2, stock_value=1)
        derived.refresh_from_db()
        self.assertEqual(derived.unit_cost, 1)               # 1 / 2 = 0.5 → 1 kobo

        issued = self._stock_item(entity, code="ROUND-ISSUE")
        StockItem.objects.filter(pk=issued.pk).update(on_hand_qty=2, stock_value=3)
        first = issue_stock(
            issued, quantity=1, movement_date=datetime.date(2026, 1, 12),
        )
        second = issue_stock(
            issued, quantity=1, movement_date=datetime.date(2026, 1, 13),
        )
        self.assertEqual(first.value_amount, -2)             # 3 × 1 / 2 = 1.5 → 2
        self.assertEqual(second.value_amount, -1)            # exact depletion takes residue
        issued.refresh_from_db()
        self.assertEqual(issued.on_hand_qty, 0)
        self.assertEqual(issued.stock_value, 0)

        explicit = self._stock_item(entity, code="ROUND-EXPLICIT")
        explicit_move = adjust_stock(
            explicit, quantity_delta=Decimal("0.5"), unit_cost=1,
            movement_date=datetime.date(2026, 1, 14),
        )
        self.assertEqual(explicit_move.value_amount, 1)      # 1 × 0.5 → 1 kobo

        average = self._stock_item(entity, code="ROUND-AVERAGE")
        StockItem.objects.filter(pk=average.pk).update(on_hand_qty=2, stock_value=1)
        average_move = adjust_stock(
            average, quantity_delta=1, movement_date=datetime.date(2026, 1, 15),
        )
        self.assertEqual(average_move.value_amount, 1)       # current average 0.5 → 1 kobo

    def test_adjustment_writeup_and_shrinkage(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)

        # Shrinkage: count short by 2 at current average → Dr 5150, Cr 1400.
        shrink = adjust_stock(
            item, quantity_delta=-2, movement_date=datetime.date(2026, 1, 14),
        )
        lines = {l.account.code: l for l in shrink.journal.lines.all()}
        self.assertEqual(lines["5150"].debit, 200_000)
        self.assertEqual(lines["1400"].credit, 200_000)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 8)
        self.assertEqual(item.stock_value, 800_000)

        # Write-up: found 2 more, priced at current average → Dr 1400, Cr 5150.
        writeup = adjust_stock(
            item, quantity_delta=2, movement_date=datetime.date(2026, 1, 15),
        )
        lines = {l.account.code: l for l in writeup.journal.lines.all()}
        self.assertEqual(lines["1400"].debit, 200_000)
        self.assertEqual(lines["5150"].credit, 200_000)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 10)
        self.assertEqual(item.stock_value, 1_000_000)
        messages = FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.STOCK_ADJUSTED,
        ).values_list("message", flat=True)
        self.assertTrue(all("₦" in message and "kobo" not in message.lower() for message in messages))

    def test_writeup_into_empty_stock_requires_unit_cost(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)

        with self.assertRaises(StockError):
            adjust_stock(item, quantity_delta=5, movement_date=datetime.date(2026, 1, 14))

        # With a unit_cost the opening write-up posts.
        adjust_stock(
            item, quantity_delta=5, unit_cost=50_000,
            movement_date=datetime.date(2026, 1, 14),
        )
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 5)
        self.assertEqual(item.stock_value, 250_000)

    def test_reorder_report_and_valuation(self):
        entity, _, vendor, _, _ = self.build_p2p()
        low = self._stock_item(entity, code="LOW", reorder_level=20, reorder_qty=50)
        ok = self._stock_item(entity, code="OK", reorder_level=2, reorder_qty=10)
        self._receive_via_grn(entity, vendor, low, qty=10, unit_price=100_000)
        self._receive_via_grn(entity, vendor, ok, qty=10, unit_price=50_000)

        report = reorder_report(entity)
        codes = {r["code"] for r in report}
        self.assertEqual(codes, {"LOW"})                    # only LOW is at/below level

        valuation = stock_valuation(entity)
        self.assertEqual(valuation["total_value"], 1_500_000)
        by_code = {r["code"]: r for r in valuation["rows"]}
        self.assertEqual(by_code["LOW"]["stock_value"], 1_000_000)
        self.assertEqual(by_code["OK"]["stock_value"], 500_000)


class StockConsoleAPITests(_P2PFixtureMixin, TestCase):
    """Security-first coverage for the stock-item / movement REST surface."""

    def _client(self, entity, email="stock-console@test.com"):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=entity.tenant,
            user_type="CX_STAFF", status="ACTIVE", first_name="Stock", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def _item(self, entity, *, code="WIDGET", reorder_level=0, reorder_qty=0, is_active=True):
        return StockItem.objects.create(
            entity=entity, code=code, name=f"Stock {code}",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5100"),
            reorder_level=reorder_level, reorder_qty=reorder_qty, is_active=is_active,
        )

    def _receive(self, entity, vendor, item, *, qty, unit_price):
        """Post a single-line stock GRN (real receipt) so on-hand/value are ledger-built."""
        from vs_procurement.purchasing import price_po

        po = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2026, 1, 5),
            status=DocumentStatus.APPROVED,
        )
        po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, description=item.name,
            expense_account=self.acc(entity, "5100"), quantity=qty,
            unit_price=unit_price, line_no=1,
        )
        price_po(po)
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            received_date=datetime.date(2026, 1, 8),
        )
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, stock_item=item,
            expense_account=self.acc(entity, "5100"),
            accepted_qty=qty, unit_price=unit_price, line_no=1,
        )
        post_grn(grn)
        item.refresh_from_db()
        return grn

    # --- permission gating ------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_every_endpoint_is_rbac_gated(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = self._item(entity)
        client = self._client(entity)
        e = f"?entity={entity.code}"
        denied = [
            ("get", f"/v1/procurement/stock-items/{e}"),
            ("post", f"/v1/procurement/stock-items/{e}"),
            ("get", f"/v1/procurement/stock-items/summary/{e}"),
            ("get", f"/v1/procurement/stock-items/{item.pk}/{e}"),
            ("patch", f"/v1/procurement/stock-items/{item.pk}/{e}"),
            ("post", f"/v1/procurement/stock-items/{item.pk}/issue/{e}"),
            ("post", f"/v1/procurement/stock-items/{item.pk}/adjust/{e}"),
            ("get", f"/v1/procurement/stock-movements/{e}"),
            ("get", f"/v1/procurement/reports/stock-reorder/{e}"),
            ("get", f"/v1/procurement/reports/stock-valuation/{e}"),
        ]
        for method, url in denied:
            response = getattr(client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 403, f"{method} {url}")

    @patch("vs_rbac.permissions.is_vision_super_admin", return_value=False)
    @patch("vs_rbac.permissions.has_permission")
    def test_verbs_resolve_distinct_permission_keys(self, mock_has, _super):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=10, unit_price=100_000)
        client = self._client(entity)
        e = f"?entity={entity.code}"
        # view grants list/detail/summary/movements but NOT manage/issue/adjust.
        mock_has.side_effect = _deny_keys(
            "procurement.stock.manage", "procurement.stock.issue", "procurement.stock.adjust")
        self.assertEqual(client.get(f"/v1/procurement/stock-items/{e}").status_code, 200)
        self.assertEqual(client.get(f"/v1/procurement/stock-items/summary/{e}").status_code, 200)
        self.assertEqual(client.get(f"/v1/procurement/stock-items/{item.pk}/{e}").status_code, 200)
        self.assertEqual(
            client.patch(f"/v1/procurement/stock-items/{item.pk}/{e}", {"name": "x"},
                         format="json").status_code, 403)
        self.assertEqual(
            client.post(f"/v1/procurement/stock-items/{item.pk}/issue/{e}", {"quantity": 1},
                        format="json").status_code, 403)
        self.assertEqual(
            client.post(f"/v1/procurement/stock-items/{item.pk}/adjust/{e}",
                        {"quantity_delta": 1, "unit_cost": 1000}, format="json").status_code, 403)
        # reports resolve on report.view - denied even when stock.view is held.
        mock_has.side_effect = _deny_keys("procurement.report.view")
        self.assertEqual(
            client.get(f"/v1/procurement/reports/stock-reorder/{e}").status_code, 403)
        self.assertEqual(
            client.get(f"/v1/procurement/reports/stock-valuation/{e}").status_code, 403)

    # --- entity isolation -------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_cross_entity_item_is_not_reachable(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        foreign = self._item(other, code="FOREIGN")
        client = self._client(entity)
        e = f"?entity={entity.code}"
        self.assertEqual(client.get(f"/v1/procurement/stock-items/{foreign.pk}/{e}").status_code, 404)
        for method, url, body in [
            ("patch", f"/v1/procurement/stock-items/{foreign.pk}/{e}", {"name": "x"}),
            ("post", f"/v1/procurement/stock-items/{foreign.pk}/issue/{e}", {"quantity": 1}),
            ("post", f"/v1/procurement/stock-items/{foreign.pk}/adjust/{e}",
             {"quantity_delta": 1, "unit_cost": 1000}),
        ]:
            self.assertEqual(getattr(client, method)(url, body, format="json").status_code, 404,
                             f"{method} {url}")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_cross_entity_account_reference_is_rejected(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        client = self._client(entity)
        # 1400 exists in *other* only under its own rows; resolving it against my entity's
        # chart still finds my own 1400 (codes are per-entity), so name it by a foreign id.
        foreign_inv = Account.objects.get(entity=other, code="1400")
        response = client.post(
            f"/v1/procurement/stock-items/?entity={entity.code}",
            {"code": "X", "name": "X", "inventory_account": foreign_inv.pk}, format="json")
        self.assertEqual(response.status_code, 400)

    # --- create / patch validation ----------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_normalizes_code_and_returns_detail(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        response = client.post(
            f"/v1/procurement/stock-items/?entity={entity.code}",
            {"code": " sw-01 ", "name": " Widget ", "inventory_account": "1400",
             "default_expense_account": "5100", "reorder_level": 5, "reorder_qty": 10},
            format="json")
        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(data["code"], "SW-01")           # trimmed + upper-cased
        self.assertEqual(data["name"], "Widget")
        self.assertEqual(data["inventory_code"], "1400")
        self.assertEqual(data["expense_code"], "5100")
        self.assertEqual(data["movements"], [])            # detail shape, empty ledger
        self.assertEqual(data["activity"], [])

        generated = client.post(
            f"/v1/procurement/stock-items/?entity={entity.code}",
            {"name": "Generated stock", "inventory_account": "1400"},
            format="json",
        )
        self.assertEqual(generated.status_code, 201)
        self.assertTrue(generated.data["data"]["code"].startswith(f"ST-{entity.tenant_id}"))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_detail_loads_only_newest_fifty_movements_with_sql_limit(self, _perm):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        entity, _, _, _, _ = self.build_p2p()
        item = self._item(entity, code="MOVEMENT-LIMIT")
        movements = StockMovement.objects.bulk_create([
            StockMovement(
                entity=entity, stock_item=item, movement_type="RECEIPT",
                movement_date=datetime.date(2026, 1, 8),
                quantity=1, value_amount=index + 1,
                balance_qty=index + 1, balance_value=index + 1,
            )
            for index in range(55)
        ])
        expected_ids = [movement.pk for movement in reversed(movements[-50:])]
        client = self._client(entity)
        with CaptureQueriesContext(connection) as captured:
            response = client.get(
                f"/v1/procurement/stock-items/{item.pk}/?entity={entity.code}",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.data["data"]["movements"]],
            expected_ids,
        )
        movement_sql = [
            query["sql"] for query in captured.captured_queries
            if "vs_procurement_stockmovement" in query["sql"].lower()
        ]
        self.assertEqual(len(movement_sql), 1)
        self.assertIn("LIMIT 50", movement_sql[0].upper())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_receipt_audit_appears_in_stock_item_activity(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=2, unit_price=100_000)

        response = self._client(entity).get(
            f"/v1/procurement/stock-items/{item.pk}/?entity={entity.code}",
        )

        self.assertEqual(response.status_code, 200)
        activity = response.data["data"]["activity"]
        received = [row for row in activity if row["action"] == "STOCK_RECEIVED"]
        self.assertEqual(len(received), 1)
        self.assertIn("Received 2", received[0]["message"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_rejects_bad_account_types_and_negative_reorder(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/?entity={entity.code}"
        # inventory_account must be ASSET - 2100 is a LIABILITY.
        self.assertEqual(client.post(base, {
            "code": "A", "name": "A", "inventory_account": "2100"}, format="json").status_code, 400)
        # default_expense_account must be EXPENSE - 1400 is an ASSET.
        self.assertEqual(client.post(base, {
            "code": "B", "name": "B", "inventory_account": "1400",
            "default_expense_account": "1400"}, format="json").status_code, 400)
        # inventory_account is required.
        self.assertEqual(client.post(base, {
            "code": "C", "name": "C"}, format="json").status_code, 400)
        # blank name / negative reorder.
        self.assertEqual(client.post(base, {
            "code": "D", "name": "  ", "inventory_account": "1400"}, format="json").status_code, 400)
        self.assertEqual(client.post(base, {
            "code": "E", "name": "E", "inventory_account": "1400",
            "reorder_level": -1}, format="json").status_code, 400)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_create_maps_normalized_duplicate_code_to_stable_field_error(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        self._item(entity, code="WIDGET")
        response = self._client(entity).post(
            f"/v1/procurement/stock-items/?entity={entity.code}",
            {"code": " widget ", "name": "Duplicate", "inventory_account": "1400"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("code", str(response.data).lower())
        self.assertIn("already exists", str(response.data).lower())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_unit_of_measure_is_bounded_and_blank_falls_back_to_each(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/?entity={entity.code}"
        blank = client.post(base, {
            "code": "UOM-BLANK", "name": "Blank UOM",
            "inventory_account": "1400", "unit_of_measure": "",
        }, format="json")
        self.assertEqual(blank.status_code, 201)
        self.assertEqual(blank.data["data"]["unit_of_measure"], "each")
        too_long = client.post(base, {
            "code": "UOM-LONG", "name": "Long UOM",
            "inventory_account": "1400", "unit_of_measure": "x" * 25,
        }, format="json")
        self.assertEqual(too_long.status_code, 400)
        item = StockItem.objects.get(pk=blank.data["data"]["id"])
        url = f"/v1/procurement/stock-items/{item.pk}/?entity={entity.code}"
        self.assertEqual(
            client.patch(url, {"unit_of_measure": "cartons"}, format="json").status_code,
            200,
        )
        fallback = client.patch(url, {"unit_of_measure": None}, format="json")
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.data["data"]["unit_of_measure"], "each")
        self.assertEqual(
            client.patch(url, {"unit_of_measure": "x" * 25}, format="json").status_code,
            400,
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_is_active_requires_strict_json_boolean_on_create_and_patch(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/?entity={entity.code}"
        for index, value in enumerate(("false", 0, 1), start=1):
            response = client.post(base, {
                "code": f"BOOL-{index}", "name": "Boolean",
                "inventory_account": "1400", "is_active": value,
            }, format="json")
            self.assertEqual(response.status_code, 400, value)
        valid = client.post(base, {
            "code": "BOOL-VALID", "name": "Boolean",
            "inventory_account": "1400", "is_active": False,
        }, format="json")
        self.assertEqual(valid.status_code, 201)
        self.assertFalse(valid.data["data"]["is_active"])
        url = (
            f"/v1/procurement/stock-items/{valid.data['data']['id']}/"
            f"?entity={entity.code}"
        )
        for value in ("true", 1):
            self.assertEqual(
                client.patch(url, {"is_active": value}, format="json").status_code,
                400,
                value,
            )
        self.assertEqual(
            client.patch(url, {"is_active": True}, format="json").status_code,
            200,
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_patch_code_is_immutable_but_same_code_is_accepted(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = self._item(entity, code="WIDGET")
        client = self._client(entity)
        url = f"/v1/procurement/stock-items/{item.pk}/?entity={entity.code}"
        # A different code is refused with the immutability message.
        changed = client.patch(url, {"code": "OTHER"}, format="json")
        self.assertEqual(changed.status_code, 400)
        self.assertIn("cannot be changed", str(changed.data).lower())
        # The same code (any case) is a no-op, not an error.
        same = client.patch(url, {"code": "widget", "name": "Renamed"}, format="json")
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.data["data"]["name"], "Renamed")
        item.refresh_from_db()
        self.assertEqual(item.code, "WIDGET")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_inventory_account_changes_only_when_quantity_and_value_are_zero(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        alternate = Account.objects.create(
            entity=entity, code="1410", name="Alternate inventory",
            account_type="ASSET", is_postable=True,
        )
        client = self._client(entity)

        empty = self._item(entity, code="EMPTY")
        empty_url = f"/v1/procurement/stock-items/{empty.pk}/?entity={entity.code}"
        changed = client.patch(
            empty_url, {"inventory_account": alternate.pk}, format="json",
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.data["data"]["inventory_code"], "1410")

        quantity_only = self._item(entity, code="QTY-ONLY")
        StockItem.objects.filter(pk=quantity_only.pk).update(on_hand_qty=1, stock_value=0)
        quantity_url = (
            f"/v1/procurement/stock-items/{quantity_only.pk}/?entity={entity.code}"
        )
        blocked_qty = client.patch(
            quantity_url, {"inventory_account": alternate.pk}, format="json",
        )
        self.assertEqual(blocked_qty.status_code, 400)
        self.assertIn("quantity or value remains", str(blocked_qty.data))
        # Sending the existing account is a permitted no-op even with a live balance.
        self.assertEqual(
            client.patch(quantity_url, {"inventory_account": "1400"}, format="json").status_code,
            200,
        )

        value_only = self._item(entity, code="VALUE-ONLY")
        StockItem.objects.filter(pk=value_only.pk).update(on_hand_qty=0, stock_value=1)
        value_url = f"/v1/procurement/stock-items/{value_only.pk}/?entity={entity.code}"
        blocked_value = client.patch(
            value_url, {"inventory_account": alternate.pk}, format="json",
        )
        self.assertEqual(blocked_value.status_code, 400)
        self.assertIn("quantity or value remains", str(blocked_value.data))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_patch_never_touches_balances(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=10, unit_price=100_000)
        client = self._client(entity)
        url = f"/v1/procurement/stock-items/{item.pk}/?entity={entity.code}"
        client.patch(url, {"on_hand_qty": 999, "stock_value": 1}, format="json")
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 10)             # ledger-owned, unchanged
        self.assertEqual(item.stock_value, 1_000_000)

    # --- issue ------------------------------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_issue_posts_dr_expense_cr_inventory_at_average(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=10, unit_price=100_000)    # avg 1000.00
        self._receive(entity, vendor, item, qty=10, unit_price=200_000)    # avg 1500.00
        client = self._client(entity)
        response = client.post(
            f"/v1/procurement/stock-items/{item.pk}/issue/?entity={entity.code}",
            {"quantity": 4, "movement_date": "2026-01-20"}, format="json")
        self.assertEqual(response.status_code, 201)
        movement = StockMovement.objects.get(id=response.data["data"]["movement"]["id"])
        lines = {l.account.code: l for l in movement.journal.lines.all()}
        self.assertEqual(lines["5100"].debit, 600_000)     # Dr expense at moving average
        self.assertEqual(lines["1400"].credit, 600_000)    # Cr inventory relief
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 16)
        self.assertEqual(item.stock_value, 2_400_000)
        # The detail payload reflects the new balance and carries the movement ledger.
        stock_item = response.data["data"]["stock_item"]
        self.assertEqual(stock_item["stock_value"], 2_400_000)
        self.assertEqual(stock_item["movements"][0]["movement_type"], "ISSUE")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_issue_over_on_hand_is_rejected_and_leaves_balance(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=5, unit_price=100_000)
        client = self._client(entity)
        response = client.post(
            f"/v1/procurement/stock-items/{item.pk}/issue/?entity={entity.code}",
            {"quantity": 8}, format="json")
        # Over-issue is a domain conflict (InsufficientStockError → 409), not bad input.
        self.assertEqual(response.status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 5)              # never negative
        self.assertEqual(item.stock_value, 500_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_issue_rejects_bad_quantity_and_date(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=5, unit_price=100_000)
        client = self._client(entity)
        url = f"/v1/procurement/stock-items/{item.pk}/issue/?entity={entity.code}"
        for body in ({"quantity": 0}, {"quantity": -1}, {"quantity": "NaN"},
                     {"quantity": 1, "movement_date": "not-a-date"}):
            self.assertEqual(client.post(url, body, format="json").status_code, 400, body)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_issue_and_adjust_bound_reference_and_narration_text(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=5, unit_price=100_000)
        client = self._client(entity)
        issue_url = f"/v1/procurement/stock-items/{item.pk}/issue/?entity={entity.code}"
        adjust_url = f"/v1/procurement/stock-items/{item.pk}/adjust/?entity={entity.code}"
        for url, payload in (
            (issue_url, {"quantity": 1, "reference": "r" * 65}),
            (issue_url, {"quantity": 1, "narration": "n" * 256}),
            (adjust_url, {"quantity_delta": -1, "reference": "r" * 65}),
            (adjust_url, {"quantity_delta": -1, "narration": "n" * 256}),
        ):
            self.assertEqual(client.post(url, payload, format="json").status_code, 400)

    # --- adjust ------------------------------------------------------------ #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_adjust_writeup_and_shrinkage_post_correct_sides(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=10, unit_price=100_000)    # 10 @ 1000.00
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/{item.pk}/adjust/?entity={entity.code}"
        # Shrinkage: −2 at average → Dr 5150, Cr 1400.
        shrink = client.post(base, {"quantity_delta": -2, "movement_date": "2026-01-14"},
                             format="json")
        self.assertEqual(shrink.status_code, 201)
        s_lines = {l.account.code: l for l in
                   StockMovement.objects.get(id=shrink.data["data"]["movement"]["id"]).journal.lines.all()}
        self.assertEqual(s_lines["5150"].debit, 200_000)
        self.assertEqual(s_lines["1400"].credit, 200_000)
        # Write-up: +2 at average → Dr 1400, Cr 5150.
        writeup = client.post(base, {"quantity_delta": 2, "movement_date": "2026-01-15"},
                              format="json")
        self.assertEqual(writeup.status_code, 201)
        w_lines = {l.account.code: l for l in
                   StockMovement.objects.get(id=writeup.data["data"]["movement"]["id"]).journal.lines.all()}
        self.assertEqual(w_lines["1400"].debit, 200_000)
        self.assertEqual(w_lines["5150"].credit, 200_000)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 10)
        self.assertEqual(item.stock_value, 1_000_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_adjust_rejects_zero_delta_over_decrease_and_float_unit_cost(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        self._receive(entity, vendor, item, qty=3, unit_price=100_000)
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/{item.pk}/adjust/?entity={entity.code}"
        # A zero delta is bad input (validation → 400).
        self.assertEqual(client.post(base, {"quantity_delta": 0}, format="json").status_code, 400)
        # Decrease beyond on-hand is a domain conflict (InsufficientStockError → 409).
        self.assertEqual(client.post(base, {"quantity_delta": -5}, format="json").status_code, 409)
        # A float unit_cost must not coerce across the kobo boundary (validation → 400).
        self.assertEqual(client.post(base, {"quantity_delta": 2, "unit_cost": 12.5},
                                     format="json").status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 3)
        self.assertEqual(item.stock_value, 300_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_adjust_rejects_more_than_four_decimal_places(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = self._item(entity)
        response = self._client(entity).post(
            f"/v1/procurement/stock-items/{item.pk}/adjust/?entity={entity.code}",
            {"quantity_delta": "0.00001", "unit_cost": 100_000},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("four decimal places", str(response.data))

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_adjust_write_up_from_empty_requires_unit_cost(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        item = self._item(entity)
        client = self._client(entity)
        base = f"/v1/procurement/stock-items/{item.pk}/adjust/?entity={entity.code}"
        # No existing average and no unit_cost → the service refuses (StockError → 409).
        # Date lands in the fixture's open Jan-2026 period so the 409 is the missing-cost
        # refusal, not a closed-period rejection.
        self.assertEqual(
            client.post(base, {"quantity_delta": 5, "movement_date": "2026-01-15"},
                        format="json").status_code, 409)
        # With a unit_cost the opening write-up posts Dr 1400 / Cr 5150.
        ok = client.post(base, {"quantity_delta": 5, "unit_cost": 50_000,
                                "movement_date": "2026-01-15"}, format="json")
        self.assertEqual(ok.status_code, 201)
        item.refresh_from_db()
        self.assertEqual(item.on_hand_qty, 5)
        self.assertEqual(item.stock_value, 250_000)

    # --- summary / reports / movements ------------------------------------- #

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_summary_counts_states_and_is_entity_scoped(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        in_stock = self._item(entity, code="INSTOCK", reorder_level=2)
        low = self._item(entity, code="LOW", reorder_level=20)
        self._item(entity, code="OUT", reorder_level=2)               # never received → 0
        self._item(entity, code="RETIRED", reorder_level=2, is_active=False)
        self._receive(entity, vendor, in_stock, qty=10, unit_price=100_000)   # 10 > 2
        self._receive(entity, vendor, low, qty=10, unit_price=50_000)         # 10 <= 20
        # A foreign item must not leak into this entity's aggregate.
        other = LedgerEntity.objects.create(name="Other", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        self._item(other, code="FOREIGN")
        client = self._client(entity)
        data = client.get(f"/v1/procurement/stock-items/summary/?entity={entity.code}").data["data"]
        self.assertEqual(data["tracked"], 4)              # excludes the foreign item
        self.assertEqual(data["active"], 3)               # RETIRED excluded
        self.assertEqual(data["low_stock"], 1)            # LOW only (> 0 and <= level)
        self.assertEqual(data["out_of_stock"], 1)         # OUT only
        self.assertEqual(data["total_value"], 1_500_000)  # 1,000,000 + 500,000

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_reorder_and_valuation_reports_carry_real_state(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        low = self._item(entity, code="LOW", reorder_level=20, reorder_qty=50)
        ok = self._item(entity, code="OK", reorder_level=2, reorder_qty=10)
        self._receive(entity, vendor, low, qty=10, unit_price=100_000)
        self._receive(entity, vendor, ok, qty=10, unit_price=50_000)
        client = self._client(entity)
        reorder = client.get(f"/v1/procurement/reports/stock-reorder/?entity={entity.code}")
        self.assertEqual({r["code"] for r in reorder.data["data"]["rows"]}, {"LOW"})
        valuation = client.get(f"/v1/procurement/reports/stock-valuation/?entity={entity.code}")
        self.assertEqual(valuation.data["data"]["total_value"]["kobo"], 1_500_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_stock_reports_paginate_sql_sources_and_keep_whole_entity_total(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        client = self._client(entity)
        reorder_url = f"/v1/procurement/reports/stock-reorder/?entity={entity.code}"
        valuation_url = f"/v1/procurement/reports/stock-valuation/?entity={entity.code}"

        for url in (reorder_url, valuation_url):
            empty = client.get(url)
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.data["data"]["rows"], [])
            self.assertEqual(empty.data["pagination"]["totalItems"], 0)

        inventory = self.acc(entity, "1400")
        StockItem.objects.bulk_create([
            StockItem(
                entity=entity, code=f"PAGE-{index:03d}", name=f"Paged stock {index}",
                inventory_account=inventory,
                on_hand_qty=0, stock_value=index + 1,
                reorder_level=0, reorder_qty=10,
            )
            for index in range(105)
        ])
        expected_total = sum(range(1, 106))

        reorder_page_1 = client.get(reorder_url)
        reorder_page_2 = client.get(f"{reorder_url}&page=2")
        self.assertEqual(reorder_page_1.data["pagination"]["pageSize"], 25)
        self.assertEqual(reorder_page_1.data["pagination"]["totalItems"], 105)
        self.assertEqual(
            [row["code"] for row in reorder_page_1.data["data"]["rows"]],
            [f"PAGE-{index:03d}" for index in range(25)],
        )
        self.assertEqual(
            [row["code"] for row in reorder_page_2.data["data"]["rows"]],
            [f"PAGE-{index:03d}" for index in range(25, 50)],
        )
        capped = client.get(f"{reorder_url}&page_size=999")
        self.assertEqual(capped.data["pagination"]["pageSize"], 100)
        self.assertEqual(len(capped.data["data"]["rows"]), 100)

        valuation_page_1 = client.get(valuation_url)
        valuation_page_2 = client.get(f"{valuation_url}&page=2")
        self.assertEqual(valuation_page_1.data["pagination"]["totalItems"], 105)
        self.assertEqual(
            valuation_page_1.data["data"]["total_value"]["kobo"],
            expected_total,
        )
        self.assertEqual(
            valuation_page_2.data["data"]["total_value"]["kobo"],
            expected_total,
        )
        self.assertEqual(
            [row["code"] for row in valuation_page_2.data["data"]["rows"]],
            [f"PAGE-{index:03d}" for index in range(25, 50)],
        )

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_movements_ledger_preserves_grn_receipt_and_is_empty_shape_stable(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._item(entity)
        client = self._client(entity)
        # Empty ledger serialises as {} (paginator's empty-list shape).
        empty = client.get(f"/v1/procurement/stock-movements/?entity={entity.code}")
        self.assertIn(empty.data["data"], ({}, []))
        # A GRN receipt lands as a RECEIPT movement snapshotting the running balance.
        self._receive(entity, vendor, item, qty=10, unit_price=100_000)
        rows = client.get(
            f"/v1/procurement/stock-movements/?entity={entity.code}&movement_type=RECEIPT"
        ).data["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["movement_type"], "RECEIPT")
        self.assertEqual(rows[0]["balance_qty"], "10.0000")
        self.assertEqual(rows[0]["balance_value"], 1_000_000)


class ProcurementReportHardeningTests(_P2PFixtureMixin, TestCase):
    """Regression coverage for paged, historical, and evidence-backed reports."""

    def _client(self, entity):
        from django.contrib.auth import get_user_model
        from core.test_utils import TenantAPIClient

        user = get_user_model().objects.create_user(
            email=f"report-hardening-{entity.pk}@test.com", password="pw",
            tenant=entity.tenant, user_type="CX_STAFF", status="ACTIVE",
            first_name="Report", last_name="Tester",
        )
        return TenantAPIClient(user=user)

    def _payment(self, entity, vendor, amount, date, *, allocations=None, auto_allocate=True):
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=date,
            gross_amount=amount, payment_account=self.acc(entity, "1100"),
            approval_state=ProcApprovalState.APPROVED,
        )
        post_vendor_payment(
            payment, allocations=allocations, auto_allocate=auto_allocate,
        )
        return payment

    def _cycle_chain(self, entity, vendor):
        pr = PurchaseRequisition.objects.create(
            entity=entity, request_date=datetime.date(2026, 1, 2),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=pr, description="cycle item", quantity=10,
            estimated_unit_price=100_000,
            expense_account=self.acc(entity, "5100"), line_no=1,
        )
        submit_requisition(pr)
        approve_requisition(pr)
        po = create_po_from_requisition(
            pr, vendor=vendor, order_date=datetime.date(2026, 1, 5),
            expected_date=datetime.date(2026, 1, 9),
        )
        approve_purchase_order(po)
        po.refresh_from_db()
        grn = self.make_grn(entity, vendor, po, [(po.lines.get(), 10)])
        post_grn(grn)
        invoice = self.make_bill(
            entity, vendor, [("5100", 10, 100_000, None, po.lines.get())],
            po=po, date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice)
        return pr, po, grn, invoice

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_pagination_is_stable_and_keeps_whole_report_totals(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        vendors = [vendor]
        for code in ("BETA", "CHARLIE"):
            vendors.append(Vendor.objects.create(
                entity=entity, code=code, name=code.title(),
                payable_account=self.acc(entity, "2100"),
                default_expense_account=self.acc(entity, "5300"),
            ))
        for index, row_vendor in enumerate(vendors, start=1):
            VendorInvoice.objects.create(
                entity=entity, vendor=row_vendor, status=DocumentStatus.POSTED,
                invoice_date=datetime.date(2026, 1, 10),
                due_date=datetime.date(2026, 1, 10),
                subtotal=index * 100, total=index * 100,
            )

        url = f"/v1/procurement/reports/ap-aging/?entity={entity.code}&page_size=1"
        client = self._client(entity)
        page1 = client.get(url)
        page2 = client.get(f"{url}&page=2")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.data["pagination"]["totalItems"], 3)
        self.assertEqual(page1.data["pagination"]["pageSize"], 1)
        self.assertEqual(page1.data["data"]["rows"][0]["code"], "ACME")
        self.assertEqual(page2.data["data"]["rows"][0]["code"], "BETA")
        self.assertEqual(page1.data["data"]["total_net"]["kobo"], 600)
        self.assertEqual(page2.data["data"]["total_net"]["kobo"], 600)
        capped = client.get(f"{url}&page_size=999")
        self.assertEqual(capped.data["pagination"]["pageSize"], 100)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_empty_paginated_report_keeps_an_explicit_empty_list(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        response = self._client(entity).get(
            f"/v1/procurement/reports/ap-aging/?entity={entity.code}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pagination"]["pageSize"], 25)
        self.assertEqual(response.data["pagination"]["totalItems"], 0)
        self.assertEqual(response.data["data"]["rows"], [])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_ap_vendor_invoice_evidence_pages_without_changing_vendor_totals(self, _perm):
        entity, _, vendor, _, _ = self.build_p2p()
        for amount, date in (
            (100_000, datetime.date(2026, 1, 10)),
            (200_000, datetime.date(2026, 1, 11)),
        ):
            invoice = self.make_bill(
                entity, vendor, [("5300", 1, amount, None, None)], date=date,
            )
            post_vendor_invoice(invoice)

        url = (
            f"/v1/procurement/reports/ap-aging/vendor/?entity={entity.code}"
            f"&vendor={vendor.code}&page_size=1"
        )
        client = self._client(entity)
        page1 = client.get(url)
        page2 = client.get(f"{url}&page=2")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.data["pagination"]["totalItems"], 2)
        self.assertEqual(page2.data["pagination"]["totalItems"], 2)
        self.assertEqual(len(page1.data["data"]["invoices"]), 1)
        self.assertEqual(len(page2.data["data"]["invoices"]), 1)
        self.assertNotEqual(
            page1.data["data"]["invoices"][0]["invoice_id"],
            page2.data["data"]["invoices"][0]["invoice_id"],
        )
        self.assertEqual(page1.data["data"]["outstanding"]["kobo"], 300_000)
        self.assertEqual(page2.data["data"]["outstanding"]["kobo"], 300_000)
        self.assertEqual(page1.data["data"]["net"]["kobo"], 300_000)
        self.assertEqual(page2.data["data"]["net"]["kobo"], 300_000)

    def test_historical_ap_excludes_future_documents_and_reconciles_control(self):
        entity, period, vendor, _, _ = self.build_p2p()
        invoice = self.make_bill(
            entity, vendor, [("5300", 1, 1_000_000, None, None)],
            date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice)
        payment = self._payment(
            entity, vendor, 1_000_000, datetime.date(2026, 1, 20),
            allocations=[(invoice, 1_000_000)],
        )
        reverse_vendor_payment(payment, date=datetime.date(2026, 1, 30))
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=period.fiscal_year,
            period_no=2, name="Feb 2026",
            start_date=datetime.date(2026, 2, 1),
            end_date=datetime.date(2026, 2, 28),
        )
        future = self.make_bill(
            entity, vendor, [("5300", 1, 500_000, None, None)],
            date=datetime.date(2026, 2, 5),
        )
        post_vendor_invoice(future)

        before = ap_aging(entity, as_of=datetime.date(2026, 1, 15))
        self.assertEqual(before.total_net, 1_000_000)
        rec = reconcile_ap(entity, as_of=datetime.date(2026, 1, 15))
        self.assertEqual(rec.subledger_total, 1_000_000)
        self.assertEqual(rec.control_total, 1_000_000)
        self.assertTrue(rec.is_reconciled)
        after = ap_aging(entity, as_of=datetime.date(2026, 1, 25))
        self.assertEqual(after.total_net, 0)
        reversed_snapshot = reconcile_ap(
            entity, as_of=datetime.date(2026, 1, 31),
        )
        self.assertEqual(reversed_snapshot.subledger_total, 1_000_000)
        self.assertEqual(reversed_snapshot.control_total, 1_000_000)
        self.assertTrue(reversed_snapshot.is_reconciled)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_grir_historical_snapshot_and_balance_api(self, _perm):
        from vs_procurement.reports import grir_aging

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.get(), 10)])
        post_grn(grn)
        invoice = self.make_bill(
            entity, vendor, [("5100", 10, 100_000, None, po.lines.get())],
            po=po, date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice)

        before = grir_aging(entity, as_of=datetime.date(2026, 1, 9))
        after = grir_aging(entity, as_of=datetime.date(2026, 1, 10))
        self.assertEqual(before.total_open, 1_000_000)
        self.assertEqual(before.control_balance, 1_000_000)
        self.assertEqual(after.total_open, 0)
        self.assertEqual(after.control_balance, 0)
        response = self._client(entity).get(
            f"/v1/procurement/reports/grir/?entity={entity.code}&as_of=2026-01-09",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["as_of"], "2026-01-09")
        self.assertEqual(response.data["data"]["grir_balance"]["kobo"], 1_000_000)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_analytics_validate_ranges_and_canonical_entity_category(self, _perm):
        entity, _, _, _, _ = self.build_p2p()
        category = VendorCategory.objects.create(
            entity=entity, code="OFFICE", name="Office",
        )
        other = LedgerEntity.objects.create(
            name="Foreign report books", code="FOREIGN-REPORT",
            kind=LedgerEntity.Kind.TENANT,
        )
        VendorCategory.objects.create(entity=other, code="FOREIGN", name="Foreign")
        client = self._client(entity)
        for endpoint in ("spend-analysis", "vendor-performance", "cycle-time"):
            invalid = client.get(
                f"/v1/procurement/reports/{endpoint}/?entity={entity.code}"
                "&start_date=2026-01-11&end_date=2026-01-10",
            )
            self.assertEqual(invalid.status_code, 400, endpoint)
            same_day = client.get(
                f"/v1/procurement/reports/{endpoint}/?entity={entity.code}"
                "&start_date=2026-01-10&end_date=2026-01-10",
            )
            self.assertEqual(same_day.status_code, 200, endpoint)
        canonical = client.get(
            f"/v1/procurement/reports/spend-analysis/?entity={entity.code}"
            f"&category={category.code.lower()}",
        )
        self.assertEqual(canonical.data["data"]["category"], "OFFICE")
        for bad in ("MISSING", "FOREIGN"):
            response = client.get(
                f"/v1/procurement/reports/spend-analysis/"
                f"?entity={entity.code}&category={bad}",
            )
            self.assertEqual(response.status_code, 400)

    def test_fifo_splits_po_only_invoice_and_explicit_links_take_precedence(self):
        from vs_procurement.reports import grir_aging, grir_grn_detail

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100_000, None)])
        line = po.lines.get()
        first = self.make_grn(entity, vendor, po, [(line, 4)])
        post_grn(first)
        second = self.make_grn(entity, vendor, po, [(line, 6)])
        second.received_date = datetime.date(2026, 1, 9)
        second.save(update_fields=["received_date"])
        post_grn(second)
        invoice = self.make_bill(
            entity, vendor, [("5100", 7, 100_000, None, line)],
            po=po, date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice)

        report = grir_aging(entity)
        self.assertEqual(
            {row.grn_id: row.invoiced_value for row in report.rows},
            {second.id: 300_000},
        )
        self.assertEqual(grir_grn_detail(entity, first.id).invoiced_value, 400_000)
        self.assertEqual(grir_grn_detail(entity, second.id).invoiced_value, 300_000)

        # A new explicit line reserves the second receipt before a PO-only line
        # consumes FIFO capacity; the same clearing is never counted twice.
        explicit = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=datetime.date(2026, 1, 11),
            approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=explicit, po_line=line, grn_line=second.lines.get(),
            expense_account=line.expense_account, quantity=3, unit_price=100_000,
            line_no=1,
        )
        post_vendor_invoice(explicit)
        final = grir_aging(entity)
        self.assertEqual(final.total_open, 0)
        self.assertEqual(final.control_balance, 0)
        self.assertEqual(final.difference, 0)
        self.assertEqual(grir_grn_detail(entity, second.id).invoiced_value, 600_000)

    def test_fifo_excess_remains_visible_as_control_difference(self):
        from vs_procurement.reports import grir_aging

        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 8, 100_000, None)])
        line = po.lines.get()
        grn = self.make_grn(entity, vendor, po, [(line, 5)])
        post_grn(grn)
        invoice = self.make_bill(
            entity, vendor, [("5100", 8, 100_000, None, line)],
            po=po, date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice, allow_variance=True)

        report = grir_aging(entity)
        self.assertEqual(report.total_open, 0)
        self.assertEqual(report.control_balance, -300_000)
        self.assertEqual(report.difference, 300_000)

    def test_cash_forecast_nets_credit_only_vendor_and_respects_cutoff(self):
        from vs_procurement.reports import ap_cash_requirements

        entity, _, vendor, _, _ = self.build_p2p()
        credit_vendor = Vendor.objects.create(
            entity=entity, code="CREDIT", name="Credit Vendor",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
            kyc_status="VERIFIED",
        )
        invoice = self.make_bill(
            entity, vendor, [("5300", 1, 1_000_000, None, None)],
            date=datetime.date(2026, 1, 10),
        )
        post_vendor_invoice(invoice)
        self._payment(
            entity, credit_vendor, 250_000, datetime.date(2026, 1, 20),
            auto_allocate=False,
        )
        before = ap_cash_requirements(entity, as_of=datetime.date(2026, 1, 15))
        self.assertEqual(before.total_unallocated_credit, 0)
        after = ap_cash_requirements(entity, as_of=datetime.date(2026, 1, 20))
        credit_row = next(row for row in after.rows if row.code == "CREDIT")
        self.assertEqual(credit_row.total, 0)
        self.assertEqual(credit_row.unallocated_credit, 250_000)
        self.assertEqual(credit_row.net_total, -250_000)
        self.assertEqual(after.total_due, 1_000_000)
        self.assertEqual(after.total_unallocated_credit, 250_000)
        self.assertEqual(after.net_cash_requirement, 750_000)

    def test_cycle_uses_full_settlement_once_and_omits_partial_invoices(self):
        entity, _, vendor, _, _ = self.build_p2p()
        _, _, _, invoice = self._cycle_chain(entity, vendor)
        self._payment(
            entity, vendor, 400_000, datetime.date(2026, 1, 20),
            allocations=[(invoice, 400_000)],
        )
        partial = procurement_cycle_time(
            entity, start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 1, 24),
        )
        self.assertEqual(
            next(s for s in partial.stages if s.name == "invoice_to_payment").sample_count,
            0,
        )
        self._payment(
            entity, vendor, 600_000, datetime.date(2026, 1, 25),
            allocations=[(invoice, 600_000)],
        )
        report = procurement_cycle_time(
            entity, start_date=datetime.date(2026, 1, 25),
            end_date=datetime.date(2026, 1, 25),
        )
        payment_stage = next(
            s for s in report.stages if s.name == "invoice_to_payment"
        )
        self.assertEqual(payment_stage.sample_count, 1)
        self.assertEqual(payment_stage.avg_days, 15.0)
        self.assertEqual(report.end_to_end_count, 1)

    def test_cycle_excludes_negative_hops_without_discarding_valid_stages(self):
        entity, _, vendor, _, _ = self.build_p2p()
        pr, _, _, invoice = self._cycle_chain(entity, vendor)
        self._payment(
            entity, vendor, 1_000_000, datetime.date(2026, 1, 25),
            allocations=[(invoice, 1_000_000)],
        )
        pr.request_date = datetime.date(2026, 2, 1)
        pr.save(update_fields=["request_date", "updated_at"])
        report = procurement_cycle_time(entity)
        stages = {stage.name: stage for stage in report.stages}
        self.assertEqual(stages["req_to_po"].sample_count, 0)
        self.assertEqual(stages["req_to_po"].excluded_count, 1)
        self.assertEqual(stages["po_to_receipt"].sample_count, 1)
        self.assertEqual(stages["receipt_to_invoice"].sample_count, 1)
        self.assertEqual(stages["invoice_to_payment"].sample_count, 1)
        self.assertEqual(report.end_to_end_count, 0)
        self.assertEqual(report.end_to_end_excluded_count, 1)

    def test_cycle_end_to_end_rejects_non_monotonic_middle_with_positive_outer_span(self):
        entity, _, vendor, _, _ = self.build_p2p()
        _, _, _, invoice = self._cycle_chain(entity, vendor)
        self._payment(
            entity, vendor, 1_000_000, datetime.date(2026, 1, 25),
            allocations=[(invoice, 1_000_000)],
        )
        # Outer requisition→payment remains +23 days, but invoice now predates
        # receipt. The valid independent hops remain samples; the complete chain does not.
        invoice.invoice_date = datetime.date(2026, 1, 4)
        invoice.save(update_fields=["invoice_date", "updated_at"])

        report = procurement_cycle_time(entity)
        stages = {stage.name: stage for stage in report.stages}
        self.assertEqual(stages["req_to_po"].sample_count, 1)
        self.assertEqual(stages["po_to_receipt"].sample_count, 1)
        self.assertEqual(stages["receipt_to_invoice"].sample_count, 0)
        self.assertEqual(stages["receipt_to_invoice"].excluded_count, 1)
        self.assertEqual(stages["invoice_to_payment"].sample_count, 1)
        self.assertEqual(report.end_to_end_count, 0)
        self.assertEqual(report.end_to_end_excluded_count, 1)
