"""A commitment nobody will fulfil has to be closable, and closing it is guarded.

A purchase order had no way to reach CANCELLED through the API, which left two
documents stuck behind it: the order itself, and the requisition it was raised
from, whose approval cannot be reversed while an order still stands against it.
The refusal a buyer met told them to cancel the order, and there was nothing to
cancel it with.

Cancelling is refused wherever the order has already had an effect somebody else
is relying on: goods on the shelf, a vendor bill, or an approval still being
decided. Each refusal is its own error code, because a buyer does something
different about each one.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import (
    Account, FinanceAuditLog, FiscalPeriod, FiscalYear, LedgerEntity,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.constants import (
    ProcApprovalState,
    PurchaseOrderVendorDeliverySource,
    PurchaseOrderVendorDeliveryStatus,
)
from vs_procurement.models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderVendorDelivery,
    Vendor,
    VendorInvoice,
)
from vs_procurement.purchasing import post_grn, price_po
from vs_rbac.tests.helpers import make_branch, make_school


ORDERED = datetime.date(2026, 1, 5)


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class PurchaseOrderCancellationTests(TestCase):
    """One school, two branches, and an order standing at one of them."""

    def setUp(self):
        seed_currencies()
        self.school = make_school(slug="po-cancel", name="Cancel Group")
        self.tenant = self.school.tenant
        self.lekki = make_branch(self.school, name="Lekki Branch")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)

        self.entity = LedgerEntity.objects.create(
            name="Cancel Books", code="POCANC", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        seed_chart_of_accounts(self.entity)
        year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        self.vendor = Vendor.objects.create(
            entity=self.entity, code="ACME", name="Acme Supplies",
            payable_account=self.acc("2100"),
        )
        self.order = self.issued_order()
        self.buyer = self.client_for("lekki-buyer@test.com", branch=self.lekki)
        self.other_branch = self.client_for("ikeja-buyer@test.com", branch=self.ikeja)

    # -- fixture ------------------------------------------------------------- #

    def acc(self, code):
        return Account.objects.get(entity=self.entity, code=code)

    def issued_order(self, *, branch=None):
        """An approved order at a branch, as one looks after it reaches its vendor."""
        order = PurchaseOrder.objects.create(
            entity=self.entity, vendor=self.vendor, order_date=ORDERED,
            branch=branch or self.lekki,
            status=DocumentStatus.APPROVED,
            approval_state=ProcApprovalState.APPROVED,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=order, description="Chair", line_no=1,
            expense_account=self.acc("5300"), quantity=2, unit_price=100_000,
        )
        price_po(order)
        order.refresh_from_db()
        PurchaseOrderVendorDelivery.objects.create(
            purchase_order=order,
            source=PurchaseOrderVendorDeliverySource.AUTOMATIC,
            status=PurchaseOrderVendorDeliveryStatus.SENT,
        )
        return order

    def client_for(self, email, *, branch):
        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=self.tenant, branch=branch,
            status="ACTIVE", first_name="Buyer", last_name="Person",
        )
        return TenantAPIClient(user=user)

    def receive_goods(self, order, qty=1):
        line = order.lines.first()
        grn = GoodsReceivedNote.objects.create(
            entity=self.entity, vendor=self.vendor, purchase_order=order,
            received_date=datetime.date(2026, 1, 8),
        )
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=line, expense_account=line.expense_account,
            accepted_qty=qty, unit_price=line.unit_price, line_no=1,
        )
        return post_grn(grn)

    def cancel(self, client, order, reason="The vendor stopped trading"):
        body = {} if reason is None else {"reason": reason}
        return client.post(
            f"/v1/procurement/purchase-orders/{order.pk}/cancel/"
            f"?entity={self.entity.code}", body, format="json",
        )

    # -- the permitted case -------------------------------------------------- #

    def test_an_issued_order_with_nothing_against_it_is_cancelled(self, _perm):
        """The case the refusal elsewhere points at: an order nobody will fulfil."""
        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.CANCELLED)

    def test_the_cancellation_records_who_and_why(self, _perm):
        """Somebody will ask in three months why the order was withdrawn."""
        self.cancel(self.buyer, self.order, reason="Vendor went out of business")

        row = FinanceAuditLog.objects.filter(
            entity=self.entity, action=FinanceAuditAction.PURCHASE_ORDER_CANCELLED,
        ).first()
        self.assertIsNotNone(row)
        self.assertIn("Vendor went out of business", row.message)

    def test_the_approval_behind_a_cancelled_order_still_reads_approved(self, _perm):
        """It was approved, and hiding that would lose who authorised the spend."""
        self.cancel(self.buyer, self.order)

        self.order.refresh_from_db()
        self.assertEqual(self.order.approval_state, ProcApprovalState.APPROVED)

    def test_a_cancelled_order_leaves_the_pipeline_kpis(self, _perm):
        """The header counts commitments a vendor is still fulfilling."""
        url = f"/v1/procurement/purchase-orders/summary/?entity={self.entity.code}"
        before = self.buyer.get(url).data["data"]["open"]["count"]

        self.cancel(self.buyer, self.order)

        after = self.buyer.get(url).data["data"]["open"]["count"]
        self.assertEqual(before, 1)
        self.assertEqual(after, 0)

    # -- the refusals -------------------------------------------------------- #

    def test_cancelling_is_refused_once_goods_are_received(self, _perm):
        """Cancelling returns no stock and clears no GR/IR liability."""
        self.receive_goods(self.order)

        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "PURCHASE_ORDER_ALREADY_RECEIVED")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.APPROVED)

    def test_cancelling_is_refused_while_a_bill_stands_against_the_order(self, _perm):
        """A billed order is unwound through the bill, not underneath it."""
        VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendor, purchase_order=self.order,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 20),
        )

        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "PURCHASE_ORDER_ALREADY_BILLED")

    def test_a_cancelled_bill_does_not_block_the_cancellation(self, _perm):
        """A bill somebody already withdrew is not a payable to protect."""
        VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendor, purchase_order=self.order,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 20),
            status=DocumentStatus.CANCELLED,
        )

        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 200)

    def test_an_order_still_under_approval_cannot_be_cancelled(self, _perm):
        """The engine owns that decision while it runs; withdraw it first."""
        self.order.approval_state = ProcApprovalState.PENDING
        self.order.save(update_fields=["approval_state", "updated_at"])

        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "PURCHASE_ORDER_UNDER_APPROVAL")

    def test_cancelling_twice_is_refused_the_second_time(self, _perm):
        """Nothing is left to withdraw, and saying so beats a silent success."""
        self.cancel(self.buyer, self.order)

        response = self.cancel(self.buyer, self.order)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "PURCHASE_ORDER_CANCEL_REFUSED")

    def test_a_cancellation_needs_a_reason(self, _perm):
        """The audit row is where the answer lives, so it cannot be blank."""
        response = self.cancel(self.buyer, self.order, reason=None)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["error"]["code"], "PURCHASE_ORDER_CANCEL_REASON_REQUIRED")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.APPROVED)

    # -- branch scope -------------------------------------------------------- #

    def test_another_branchs_buyer_cannot_cancel_the_order(self, _perm):
        """Cancelling reaches no further than reading: the same answer, 404."""
        response = self.cancel(self.other_branch, self.order)

        self.assertEqual(response.status_code, 404)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.APPROVED)
