"""The requisition pickers on the RFQ and order forms leave out what cannot be sourced.

Ikeja's 40 chairs are on a live quote request, so a buyer picking a requisition
to put out to tender or to order from should not be offered them: every line is
taken, and the form would be refused on save. ``?has_free_lines=true`` keeps a
requisition with at least one line no live RFQ or order holds (what the RFQ form
can still use), and ``?all_lines_free=true`` one whose every line is free (what
an order raised straight from a requisition needs, since it takes every line).
Both answer on the requisition list's own key, so a buyer who raises orders but
holds no RFQ key uses them too.

The rule is the same one the free-lines endpoint and every save path use
(:func:`vs_procurement.purchasing.free_to_source`). Sunrise School has one branch
and sees only its own requisitions.
"""
from __future__ import annotations

from unittest.mock import patch

from vs_procurement.models import PurchaseRequisitionLine
from vs_procurement.purchasing import approve_requisition, create_po_from_requisition, submit_requisition

from .tests_single_live_sourcing import ORDER_DATE, _SingleSourcingFixture


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class RequisitionPickerTests(_SingleSourcingFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ordered = cls.stored(cls.multi, cls.lekki, 7)
        create_po_from_requisition(cls.ordered, vendor=cls.multi.vendor, order_date=ORDER_DATE)
        cls.draft = cls.stored(cls.multi, cls.ikeja, 3, approved=False)
        cls.mixed = cls.stored(cls.multi, cls.ikeja, 9, approved=False)
        PurchaseRequisitionLine.objects.create(
            requisition=cls.mixed, description="Desk", quantity=2,
            estimated_unit_price=100_000, expense_account=cls.acc(cls.multi.entity, "5300"),
            line_no=2,
        )
        submit_requisition(cls.mixed)
        approve_requisition(cls.mixed)

    def picked(self, client, books, **params):
        query = "&".join(f"{k}={v}" for k, v in {"entity": books.entity.code, **params}.items())
        response = client.get(f"/v1/procurement/requisitions/?{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in self.rows(response)}

    def hold_mixed_chairs(self):
        chair = self.mixed.lines.get(line_no=1)
        response = self.bello.post(self.rfq_url(self.multi), {
            "requisition": self.mixed.pk, "title": "Chairs only", "issue_date": "2026-01-12",
            "lines": [{"description": "Chair", "quantity": "9", "expense_account": "5300",
                       "requisition_line": chair.pk}],
        }, format="json")
        self.created(response)

    def test_a_requisition_whose_lines_are_all_held_is_left_out(self, _permission):
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        listed = self.picked(self.bello, self.multi, status="APPROVED", has_free_lines="true")

        self.assertNotIn(self.ikeja_chairs.pk, listed)
        self.assertNotIn(self.ordered.pk, listed)
        self.assertNotIn(self.draft.pk, listed)
        self.assertEqual(listed, {self.lekki_chairs.pk, self.mixed.pk})

    def test_any_keeps_a_partly_held_requisition_and_all_leaves_it_out(self, _permission):
        self.hold_mixed_chairs()

        self.assertIn(self.mixed.pk, self.picked(self.bello, self.multi, has_free_lines="true"))
        whole = self.picked(self.bello, self.multi, all_lines_free="true")
        self.assertNotIn(self.mixed.pk, whole)
        self.assertEqual(whole, {self.ikeja_chairs.pk, self.lekki_chairs.pk})

    def test_cancelling_the_rfq_brings_the_requisition_back(self, _permission):
        from vs_procurement.sourcing import cancel_rfq

        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        self.assertNotIn(self.ikeja_chairs.pk, self.picked(self.bello, self.multi, all_lines_free="true"))

        cancel_rfq(rfq, reason="Wrong supplier list")

        self.assertIn(self.ikeja_chairs.pk, self.picked(self.bello, self.multi, all_lines_free="true"))

    def test_without_the_filter_the_list_is_unchanged(self, _permission):
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        listed = self.picked(self.bello, self.multi, status="APPROVED")

        self.assertIn(self.ikeja_chairs.pk, listed)

    def test_an_unknown_value_is_refused(self, _permission):
        response = self.bello.get(
            f"/v1/procurement/requisitions/?entity={self.multi.entity.code}&has_free_lines=some")

        self.assertEqual(response.status_code, 400, response.data)

    def test_a_branch_bound_buyer_sees_only_their_branch(self, _permission):
        lekki = self.client_for(self.school.tenant, "lekki-picker@test.com", branch=self.lekki)

        self.assertEqual(self.picked(lekki, self.multi, has_free_lines="true"), {self.lekki_chairs.pk})

    def test_the_picker_costs_the_same_queries_however_many_requisitions(self, _permission):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as few:
            self.picked(self.bello, self.multi, status="APPROVED", has_free_lines="true")
        for quantity in range(1, 6):
            self.stored(self.multi, self.ikeja, quantity)
        with CaptureQueriesContext(connection) as many:
            listed = self.picked(self.bello, self.multi, status="APPROVED", has_free_lines="true")

        self.assertEqual(len(listed), 8)
        self.assertEqual(len(many), len(few))

    def test_a_one_branch_school_sees_its_own_free_requisition(self, _permission):
        sunrise = self.client_for(self.solo_school.tenant, "sunrise-picker@test.com")

        self.assertEqual(self.picked(sunrise, self.solo, all_lines_free="true"), {self.solo_chairs.pk})


class RequisitionPickerPermissionTests(_SingleSourcingFixture):
    """The picker answers on the requisition list's key, never on the RFQ keys."""

    def picker_url(self):
        return (f"/v1/procurement/requisitions/?entity={self.multi.entity.code}"
                f"&status=APPROVED&all_lines_free=true")

    def test_a_reader_without_the_view_key_is_refused(self):
        response = self.bello.get(self.picker_url())

        self.assertEqual(response.status_code, 403, response.data)

    def test_an_order_only_buyer_without_rfq_keys_uses_the_order_picker(self):
        """Mr Obi raises purchase orders and never sees a quote request.

        He holds the requisition list and order keys and no RFQ key. Ikeja's
        chairs on Mrs Bello's live RFQ are left out of his order picker; Lekki's,
        which nothing holds, are offered. He still cannot open the RFQ list.
        """
        from .tests import _BranchTenantsFixture

        with patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True):
            self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        obi = self.client_for(self.school.tenant, "obi-orders@test.com")
        for key in ("procurement.requisition.view", "procurement.purchase_order.view",
                    "procurement.purchase_order.create"):
            _BranchTenantsFixture.grant(obi.test_user, key, tenant=self.school.tenant,
                                        role_key="order-only-buyer")

        picked = obi.get(self.picker_url())
        rfqs = obi.get(f"/v1/procurement/rfqs/?entity={self.multi.entity.code}")

        self.assertEqual(picked.status_code, 200, picked.data)
        self.assertEqual({row["id"] for row in self.rows(picked)}, {self.lekki_chairs.pk})
        self.assertEqual(rfqs.status_code, 403, rfqs.data)
