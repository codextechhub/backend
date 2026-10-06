"""An RFQ line keeps its requisition link through every edit that does not change it.

Ikeja asks for 40 chairs and Mrs Bello puts the request on a quote request. The
edit form reads the RFQ back and sends its lines again, each with its ``id``,
when she only retitles it or, once it is out with vendors, amends the quantity.
A line matched by ``id`` keeps the requisition line it sources unless the body
names another: were the link dropped, the chairs would count as free and a
purchase order could be raised for the same 40 chairs again.

On an issued RFQ the link is part of what vendors were invited to quote for, so
an amendment may not move a line to another requisition line or unlink it. A
body that sends no line ids at all, while the RFQ holds requisition lines it
would drop, is refused rather than read as a whole replacement.

Sunrise School has one branch and is held to the same rule; another school's
buyer cannot reach the RFQ at all.
"""
from __future__ import annotations

from unittest.mock import patch

from vs_procurement.exceptions import RequisitionError
from vs_procurement.models import PurchaseOrder, RfqLine
from vs_procurement.purchasing import create_po_from_requisition

from .tests_single_live_sourcing import ORDER_DATE, _SingleSourcingFixture


def _echo(rfq, **changes):
    """The RFQ's active lines as its edit form sends them back: read shape plus ``changes``."""
    return [
        {"id": line.pk, "description": line.description, "quantity": str(line.quantity),
         "expense_account": "5300", **changes}
        for line in rfq.lines.filter(is_active=True).order_by("line_no")
    ]


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class DraftEditKeepsLinkTests(_SingleSourcingFixture):

    def test_retitling_a_draft_with_its_lines_echoed_keeps_the_link(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        line = self.ikeja_chairs.lines.get()

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "title": "Ikeja chairs, armless", "lines": _echo(rfq),
        }, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(
            [row["requisition_line_id"] for row in edited.data["data"]["lines"]], [line.pk])
        with self.assertRaisesMessage(RequisitionError, f"already on RFQ {rfq.document_number}"):
            create_po_from_requisition(
                self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)
        self.assertFalse(PurchaseOrder.objects.filter(requisition=self.ikeja_chairs).exists())

    def test_retitling_with_no_lines_keeps_the_link(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"),
                                  {"title": "Retitled"}, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())

    def test_a_draft_may_relink_a_line_explicitly_to_a_free_requisition_line(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        rfq.requisition = None
        rfq.save(update_fields=["requisition"])
        other = self.stored(self.multi, self.ikeja, 10)

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "lines": _echo(rfq, requisition_line_id=other.lines.get().pk),
        }, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertIn(self.ikeja_chairs.pk, self.free_ids())
        self.assertNotIn(other.pk, self.free_ids())

    def test_a_draft_may_unlink_a_line_explicitly(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "lines": _echo(rfq, requisition_line=None),
        }, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertIn(self.ikeja_chairs.pk, self.free_ids())

    def test_lines_sent_without_ids_that_would_drop_a_link_are_refused(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        legacy = [{k: v for k, v in row.items() if k != "id"} for row in _echo(rfq)]

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "title": "Retitled", "lines": legacy,
        }, format="json")

        self.assertEqual(edited.status_code, 400, edited.data)
        self.assertIn("id", str(edited.data))
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())
        rfq.refresh_from_db()
        self.assertEqual(rfq.title, "Ikeja chairs")

    def test_a_line_id_from_another_rfq_is_refused(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        other = self.created(self.ordinary_rfq(self.bello, self.multi, self.lekki_chairs,
                                               title="Lekki chairs"))

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "lines": _echo(other),
        }, format="json")

        self.assertEqual(edited.status_code, 400, edited.data)
        self.assertNotIn(self.lekki_chairs.pk, self.free_ids())

    def test_a_one_branch_school_keeps_its_link_the_same_way(self, _permission):
        sunrise = self.client_for(self.solo_school.tenant, "sunrise-links@test.com")
        rfq = self.created(self.ordinary_rfq(sunrise, self.solo, self.solo_chairs))

        edited = sunrise.patch(self.rfq_url(self.solo, f"{rfq.pk}/"), {
            "title": "Retitled", "lines": _echo(rfq),
        }, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(RfqLine.objects.get(rfq=rfq).requisition_line_id,
                         self.solo_chairs.lines.get().pk)
        with self.assertRaises(RequisitionError):
            create_po_from_requisition(
                self.solo_chairs, vendor=self.solo.vendor, order_date=ORDER_DATE)

    def test_another_school_cannot_edit_the_rfq(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        sunrise = self.client_for(self.solo_school.tenant, "sunrise-editor@test.com")

        edited = sunrise.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {
            "lines": _echo(rfq, requisition_line=None),
        }, format="json")

        self.assertIn(edited.status_code, (403, 404), edited.data)
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class AmendmentKeepsLinkTests(_SingleSourcingFixture):

    def amend(self, rfq, lines):
        return self.bello.post(self.rfq_url(self.multi, f"{rfq.pk}/amendments/"), {
            "summary": "Quantity changed", "lines": lines,
        }, format="json")

    def test_amending_the_quantity_without_the_link_keeps_it(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))

        amended = self.amend(rfq, _echo(rfq, quantity="35"))

        self.assertEqual(amended.status_code, 200, amended.data)
        active = RfqLine.objects.get(rfq=rfq, is_active=True)
        self.assertEqual((active.quantity, active.requisition_line_id),
                         (35, self.ikeja_chairs.lines.get().pk))
        with self.assertRaisesMessage(RequisitionError, f"already on RFQ {rfq.document_number}"):
            create_po_from_requisition(
                self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)

    def test_an_amendment_may_not_unlink_a_line(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))

        amended = self.amend(rfq, _echo(rfq, requisition_line=None))

        self.assertEqual(amended.status_code, 400, amended.data)
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())
        rfq.refresh_from_db()
        self.assertEqual(rfq.version, 1)

    def test_an_amendment_may_not_move_a_line_to_another_requisition_line(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        rfq.requisition = None
        rfq.save(update_fields=["requisition"])
        other = self.stored(self.multi, self.ikeja, 10)

        amended = self.amend(rfq, _echo(rfq, requisition_line_id=other.lines.get().pk))

        self.assertEqual(amended.status_code, 400, amended.data)
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())

    def test_an_amendment_without_line_ids_that_drops_the_link_is_refused(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        legacy = [{k: v for k, v in row.items() if k != "id"} for row in _echo(rfq, quantity="35")]

        amended = self.amend(rfq, legacy)

        self.assertEqual(amended.status_code, 400, amended.data)
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())
