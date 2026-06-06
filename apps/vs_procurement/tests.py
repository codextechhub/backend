"""Phase 3 tests — Procure-to-Pay / Accounts Payable.

Exercises the acceptance criteria: the full PR→PO→GRN→VendorInvoice→VendorPayment
chain posts correct journals, GR/IR nets to zero on a clean three-way match, the AP
sub-ledger reconciles to the AP control account, and vendor payments split AP / bank /
WHT correctly. Run against MySQL:

    ../cx/bin/python manage.py test vs_procurement --settings=apps.settings.local
"""
import datetime

from django.test import TestCase

from vs_finance.constants import DocumentStatus, FinanceAuditStatus, InvoicePaymentStatus
from vs_finance.models import (
    Account,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    TaxCode,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies

from vs_procurement.constants import MatchStatus
from vs_procurement.exceptions import ThreeWayMatchError
from vs_procurement.models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    Vendor,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
)
from vs_procurement.payables import (
    post_vendor_invoice,
    post_vendor_payment,
)
from vs_procurement.purchasing import (
    approve_requisition,
    create_po_from_requisition,
    post_grn,
    submit_requisition,
)
from vs_procurement.reports import ap_aging, grir_balance, reconcile_ap


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
        )
        for i, (code, qty, price, tax, po_line) in enumerate(lines, start=1):
            VendorInvoiceLine.objects.create(
                vendor_invoice=vi, po_line=po_line,
                expense_account=self.acc(entity, code), quantity=qty,
                unit_price=price, tax_code=tax, line_no=i,
            )
        return vi


class GoodsReceiptTests(_P2PFixtureMixin, TestCase):
    def test_grn_posts_dr_expense_cr_grir(self):
        entity, _, vendor, _, _ = self.build_p2p()
        po = self.make_po(entity, vendor, [("5100", 10, 100000, None)])
        grn = self.make_grn(entity, vendor, po, [(po.lines.first(), 10)])
        post_grn(grn)

        grn.refresh_from_db()
        self.assertEqual(grn.status, DocumentStatus.POSTED)
        self.assertEqual(grn.total_value, 1_000_000)
        self.assertTrue(grn.document_number.startswith("CFX-TBOOK-GRN-"))

        lines = {l.account.code: l for l in grn.journal.lines.all()}
        self.assertEqual(lines["5100"].debit, 1_000_000)
        self.assertEqual(lines["2150"].credit, 1_000_000)
        # GR/IR now holds the uninvoiced liability.
        self.assertEqual(grir_balance(entity), 1_000_000)
        # PO line received quantity advanced.
        self.assertEqual(po.lines.first().received_qty, 10)


class VendorInvoiceTests(_P2PFixtureMixin, TestCase):
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

    def test_partial_payment_marks_partial(self):
        entity, _, vendor, _, _ = self.build_p2p()
        vi = self._posted_bill(entity, vendor, total=1_000_000)

        pay = VendorPayment.objects.create(
            entity=entity, vendor=vendor, payment_date=datetime.date(2026, 1, 15),
            gross_amount=400_000, wht_amount=0,
            payment_account=self.acc(entity, "1100"),
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
        )
        post_vendor_payment(pay)

        vi.refresh_from_db()
        self.assertEqual(vi.payment_status, InvoicePaymentStatus.PAID)
        self.assertTrue(reconcile_ap(entity).is_reconciled)
        self.assertEqual(reconcile_ap(entity).control_total, 0)
