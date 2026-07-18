"""Phase 3 tests — Procure-to-Pay / Accounts Payable.

Exercises the acceptance criteria: the full PR→PO→GRN→VendorInvoice→VendorPayment
chain posts correct journals, GR/IR nets to zero on a clean three-way match, the AP
sub-ledger reconciles to the AP control account, and vendor payments split AP / bank /
WHT correctly. Run against MySQL:

    ../cx/bin/python manage.py test vs_procurement --settings=apps.settings.local
"""
import datetime
from unittest.mock import patch

from django.test import TestCase

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
    QuotationStatus,
    RfqStatus,
)
from vs_procurement.exceptions import ContractError, SourcingError, ThreeWayMatchError
from vs_procurement.models import (
    CatalogItem,
    ContractMilestone,
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    StockItem,
    StockMovement,
    Vendor,
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
    issue_rfq,
    submit_quotation,
)
from vs_procurement.payables import (
    post_vendor_invoice,
    post_vendor_payment,
    reverse_vendor_payment,
)
from vs_procurement.purchasing import (
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
        """accepts: [(po_line, accepted_qty)] — unit price taken from the PO line."""
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

        # Edit adjusts the first line AND adds a line that was never on the receipt —
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
        post_grn(grn)

        grn.refresh_from_db()
        self.assertEqual(grn.status, DocumentStatus.POSTED)
        self.assertEqual(grn.total_value, 1_000_000)
        self.assertTrue(grn.document_number.startswith("TBO-GN-"))

        lines = {l.account.code: l for l in grn.journal.lines.all()}
        self.assertEqual(lines["5100"].debit, 1_000_000)
        self.assertEqual(lines["2150"].credit, 1_000_000)
        # GR/IR now holds the uninvoiced liability.
        self.assertEqual(grir_balance(entity), 1_000_000)
        # PO line received quantity advanced.
        self.assertEqual(po.lines.first().received_qty, 10)


class VendorInvoiceTests(_P2PFixtureMixin, TestCase):
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

        # A directly-raised PO (no requisition) whose line is classified to IT — it
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


# --------------------------------------------------------------------------- #
# Sourcing: RFQ → quotations → award → PO                                     #
# --------------------------------------------------------------------------- #

class SourcingTests(_P2PFixtureMixin, TestCase):
    """RFQ lifecycle, quotation submission and award-into-PO conversion."""

    def _make_rfq(self, entity, *, lines=None):
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Office chairs",
            issue_date=datetime.date(2026, 1, 3),
        )
        for i, (desc, qty) in enumerate(lines or [("Mesh chair", 10)], start=1):
            RfqLine.objects.create(
                rfq=rfq, description=desc, quantity=qty, line_no=i,
                expense_account=self.acc(entity, "5300"),
            )
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

    def test_quotation_only_submits_against_issued_rfq(self):
        entity, _, vendor, _, _ = self.build_p2p()
        rfq = self._make_rfq(entity)
        quo = self._make_quotation(entity, rfq, vendor, lines=[("Mesh chair", 10, 200_000)])
        # RFQ still DRAFT — cannot submit.
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
        # Ends soon, 30-day notice — inside window as of 2026-12-15.
        soon = self._contract(
            entity, vendor, ref="C-SOON", start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31), notice=30,
        )
        activate_contract(soon)
        # Ends far away — not yet in window.
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


# --------------------------------------------------------------------------- #
# AP cash-requirements forecast + GR/IR aging                                 #
# --------------------------------------------------------------------------- #

class CashForecastTests(_P2PFixtureMixin, TestCase):
    def test_open_bill_buckets_by_days_until_due(self):
        from vs_procurement.reports import ap_cash_requirements

        entity, _, vendor, _, _ = self.build_p2p()
        vi = self.make_bill(
            entity, vendor, [("5300", 1, 1_000_000, None, None)],
            date=datetime.date(2026, 1, 10),
        )
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
        # Aging total matches the GL control magnitude.
        self.assertEqual(report.total_open, abs(grir_balance(entity)))
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
        self.make_po(entity, vendor, [("5300", 7, 100_000, None)])  # left DRAFT
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

        # Re-running upserts in place — still exactly four templates / two stages.
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

    def _receive_via_grn(self, entity, vendor, item, *, qty, unit_price,
                         received_date=datetime.date(2026, 1, 8)):
        """Build & post a single-line stock GRN, returning the posted GRN."""
        po = PurchaseOrder.objects.create(
            entity=entity, vendor=vendor, order_date=datetime.date(2026, 1, 5),
        )
        po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, description=item.name,
            expense_account=self.acc(entity, "5100"), quantity=qty,
            unit_price=unit_price, line_no=1,
        )
        from vs_procurement.purchasing import price_po
        price_po(po)
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po, received_date=received_date,
        )
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, stock_item=item,
            expense_account=self.acc(entity, "5100"),
            accepted_qty=qty, unit_price=unit_price, line_no=1,
        )
        post_grn(grn)
        item.refresh_from_db()      # GRN posted against a fresh instance; sync the caller's
        return grn

    def test_grn_into_stock_capitalises_to_inventory(self):
        entity, _, vendor, _, _ = self.build_p2p()
        item = self._stock_item(entity)
        grn = self._receive_via_grn(entity, vendor, item, qty=10, unit_price=100_000)

        # GL: Dr inventory (1400), Cr GR/IR (2150) — not the line expense account.
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
