"""A requisition line is put out to tender once, and only once it is approved.

Lekki asks for 2 chairs and Ikeja for 3. A head office buyer groups both into one
shared RFQ so one supplier quotes for 5 and each branch gets its own order. The
grouping must not take a requisition nobody has approved yet, and it must not take
a line already being sourced on its own: were Lekki's 2 chairs already on Lekki's
own RFQ or purchase order, a shared award would order them a second time. The
reverse holds too: a line on a shared RFQ is not ordered again on its own. The
picker lists only the lines still free, for the branches the buyer works in. A
shared RFQ cancelled or closed without award gives its lines back, as an ordinary
RFQ does.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.test import TestCase

from vs_procurement.exceptions import RequisitionError
from vs_procurement.models import (
    PurchaseRequisition,
    RequestForQuotation,
    SharedSourcingAllocation,
)
from vs_procurement.purchasing import (
    approve_requisition,
    create_po_from_requisition,
    submit_requisition,
)
from vs_finance.seed import seed_currencies

from . import tests as p2p_tests
from .tests import _P2PFixtureMixin

FREE = "/v1/procurement/rfqs/free-requisition-lines/"
#: The fixture helpers of the branch scope suite, borrowed without collecting its tests.
_fixture = p2p_tests.ProcurementBranchScopeTests.__dict__


class _SharedSourcingFixture(_P2PFixtureMixin, TestCase):
    """Lekki and Ikeja of one school, and a second school that must stay apart."""

    share_p2p_books = False

    build_books = _fixture["build_books"]
    client_for = _fixture["client_for"]
    requisition_payload = _fixture["requisition_payload"]
    make_requisition = _fixture["make_requisition"]
    approved_requisition = _fixture["approved_requisition"]
    rows = _fixture["rows"]

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.tests.helpers import make_branch, make_school

        super().setUpTestData()
        seed_currencies()
        cls.school = make_school(slug="shared-guard", name="Bright Star Group", status="ACTIVE")
        cls.lekki = make_branch(cls.school, name="Lekki Branch")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)
        cls.multi = cls.build_books("SHGUARD", cls.school.tenant)
        cls.foreign_school = make_school(slug="shared-guard-x", name="Other School", status="ACTIVE")
        cls.foreign_branch = make_branch(cls.foreign_school, name="Foreign Branch")
        cls.foreign = cls.build_books("SHGUARDX", cls.foreign_school.tenant)

    def requisition(self, client, branch, quantity, *, approved=True):
        lines = [{"description": "Chair", "quantity": quantity,
                  "estimated_unit_price": 100_000, "expense_account": "5300"}]
        if approved:
            return self.approved_requisition(client, self.multi, branch=branch.pk, lines=lines)
        return self.make_requisition(client, self.multi, branch=branch.pk, lines=lines)[0]

    def shared(self, client, *sources, title="Shared chairs"):
        """POST a shared RFQ allocating each requisition's single line in full."""
        lines = [line for req in sources for line in req.lines.all()]
        return client.post(
            f"/v1/procurement/rfqs/?entity={self.multi.entity.code}",
            {"title": title, "issue_date": "2026-01-12", "lines": [{
                "description": "Chair", "expense_account": "5300",
                "quantity": sum(line.quantity for line in lines),
                "allocations": [
                    {"requisition_line": line.pk, "quantity": line.quantity} for line in lines
                ],
            }]},
            format="json",
        )


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class SharedRfqRefusalTests(_SharedSourcingFixture):

    def setUp(self):
        self.hq = self.client_for(self.school.tenant, f"hq-{self._testMethodName}@test.com")

    def test_an_unapproved_requisition_is_refused(self, _permission):
        lekki = self.requisition(self.hq, self.lekki, 2, approved=False)
        ikeja = self.requisition(self.hq, self.ikeja, 3)

        response = self.shared(self.hq, lekki, ikeja)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("is not approved", str(response.data))
        self.assertFalse(RequestForQuotation.objects.filter(title="Shared chairs").exists())

    def test_a_line_already_on_its_own_rfq_is_refused(self, _permission):
        lekki = self.requisition(self.hq, self.lekki, 2)
        ikeja = self.requisition(self.hq, self.ikeja, 3)
        own = self.hq.post(
            f"/v1/procurement/rfqs/?entity={self.multi.entity.code}",
            {"requisition": lekki.pk, "title": "Lekki chairs", "issue_date": "2026-01-12",
             "lines": [{"description": "Chair", "quantity": 2, "expense_account": "5300",
                        "requisition_line": lekki.lines.get().pk}]},
            format="json",
        )
        self.assertEqual(own.status_code, 201, own.data)

        response = self.shared(self.hq, lekki, ikeja)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(f"already on RFQ {own.data['data']['document_number']}", str(response.data))
        self.assertFalse(SharedSourcingAllocation.objects.exists())

    def test_a_line_on_a_cancelled_rfq_is_free_again(self, _permission):
        from vs_procurement.constants import RfqStatus

        lekki = self.requisition(self.hq, self.lekki, 2)
        ikeja = self.requisition(self.hq, self.ikeja, 3)
        own = self.hq.post(
            f"/v1/procurement/rfqs/?entity={self.multi.entity.code}",
            {"requisition": lekki.pk, "title": "Lekki chairs", "issue_date": "2026-01-12",
             "lines": [{"description": "Chair", "quantity": 2, "expense_account": "5300",
                        "requisition_line": lekki.lines.get().pk}]},
            format="json",
        )
        RequestForQuotation.objects.filter(pk=own.data["data"]["id"]).update(
            rfq_status=RfqStatus.CANCELLED)

        self.assertEqual(self.shared(self.hq, lekki, ikeja).status_code, 201)

    def test_a_line_already_on_a_purchase_order_is_refused(self, _permission):
        lekki = self.requisition(self.hq, self.lekki, 2)
        ikeja = self.requisition(self.hq, self.ikeja, 3)
        po = create_po_from_requisition(
            ikeja, vendor=self.multi.vendor, order_date=datetime.date(2026, 1, 12))

        response = self.shared(self.hq, lekki, ikeja)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(f"already on purchase order {po.document_number}", str(response.data))

    def test_a_line_already_on_another_shared_rfq_is_refused_by_name(self, _permission):
        lekki = self.requisition(self.hq, self.lekki, 2)
        ikeja = self.requisition(self.hq, self.ikeja, 3)
        first = self.shared(self.hq, lekki, ikeja)
        self.assertEqual(first.status_code, 201, first.data)

        again = self.shared(self.hq, lekki, ikeja, title="Again")

        self.assertEqual(again.status_code, 400, again.data)
        self.assertIn(
            f"already on shared RFQ {first.data['data']['document_number']}", str(again.data))

    def test_a_shared_line_is_not_ordered_again_on_its_own(self, _permission):
        lekki = self.requisition(self.hq, self.lekki, 2)
        ikeja = self.requisition(self.hq, self.ikeja, 3)
        self.assertEqual(self.shared(self.hq, lekki, ikeja).status_code, 201)

        with self.assertRaises(RequisitionError):
            create_po_from_requisition(
                lekki, vendor=self.multi.vendor, order_date=datetime.date(2026, 1, 12))
        own_rfq = self.hq.post(
            f"/v1/procurement/rfqs/?entity={self.multi.entity.code}",
            {"requisition": lekki.pk, "title": "Lekki again", "issue_date": "2026-01-12",
             "lines": [{"description": "Chair", "quantity": 2, "expense_account": "5300",
                        "requisition_line": lekki.lines.get().pk}]},
            format="json",
        )
        self.assertEqual(own_rfq.status_code, 400, own_rfq.data)
        self.assertIn("already on shared RFQ", str(own_rfq.data))


class FreeRequisitionLinesTests(_SharedSourcingFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_free = cls.stored(cls.multi, cls.lekki, 2)
        cls.ikeja_free = cls.stored(cls.multi, cls.ikeja, 3)
        cls.lekki_draft = cls.stored(cls.multi, cls.lekki, 4, approved=False)
        cls.ikeja_ordered = cls.stored(cls.multi, cls.ikeja, 5)
        create_po_from_requisition(
            cls.ikeja_ordered, vendor=cls.multi.vendor, order_date=datetime.date(2026, 1, 12))
        cls.stored(cls.foreign, cls.foreign_branch, 6)

    @classmethod
    def stored(cls, books, branch, quantity, *, approved=True):
        """A requisition for ``quantity`` chairs written directly, approved unless told not."""
        from vs_procurement.models import PurchaseRequisitionLine

        req = PurchaseRequisition.objects.create(
            entity=books.entity, branch=branch, title="Chairs",
            request_date=datetime.date(2026, 1, 10),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=req, description="Chair", quantity=quantity,
            estimated_unit_price=100_000, expense_account=cls.acc(books.entity, "5300"),
            line_no=1,
        )
        if approved:
            submit_requisition(req)
            approve_requisition(req)
        req.refresh_from_db()
        return req

    def get(self, client, **params):
        query = "&".join(f"{k}={v}" for k, v in {"entity": self.multi.entity.code, **params}.items())
        return client.get(f"{FREE}?{query}")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_only_approved_unsourced_lines_of_this_school_are_listed(self, _permission):
        hq = self.client_for(self.school.tenant, "hq-reader@test.com")

        response = self.get(hq)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("pagination", response.data)
        self.assertEqual(
            {row["requisition_id"] for row in self.rows(response)},
            {self.lekki_free.pk, self.ikeja_free.pk},
        )
        row = next(r for r in self.rows(response) if r["requisition_id"] == self.ikeja_free.pk)
        self.assertEqual(row["branch_name"], "Ikeja Branch")
        self.assertEqual(row["requisition_number"], self.ikeja_free.document_number)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_a_branch_bound_buyer_sees_only_their_branch(self, _permission):
        lekki_buyer = self.client_for(self.school.tenant, "lekki-buyer@test.com", branch=self.lekki)

        own = self.get(lekki_buyer)
        other = self.get(lekki_buyer, branch=self.ikeja.pk)

        self.assertEqual([r["requisition_id"] for r in self.rows(own)], [self.lekki_free.pk])
        # Procurement lists answer a branch outside reach with nothing, as every list does.
        self.assertEqual(other.status_code, 200, other.data)
        self.assertEqual(self.rows(other), [])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_a_whole_school_buyer_narrows_to_one_branch(self, _permission):
        hq = self.client_for(self.school.tenant, "hq-narrow@test.com")

        response = self.get(hq, branch=self.ikeja.pk)

        self.assertEqual([r["requisition_id"] for r in self.rows(response)], [self.ikeja_free.pk])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_a_line_on_a_shared_rfq_leaves_the_list(self, _permission):
        hq = self.client_for(self.school.tenant, "hq-share@test.com")
        made = self.shared(hq, self.lekki_free, self.ikeja_free)
        self.assertEqual(made.status_code, 201, made.data)

        self.assertEqual(self.rows(self.get(hq)), [])

    def test_without_the_rfq_view_key_it_is_refused(self):
        nobody = self.client_for(self.school.tenant, "no-key@test.com")

        self.assertEqual(self.get(nobody).status_code, 403)


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class SharedRfqEndedWithoutAwardTests(_SharedSourcingFixture):
    """A shared RFQ cancelled or closed without award gives its lines back.

    Mrs Bello put Lekki's 60 chairs and Ikeja's 40 on one shared RFQ, then cancels
    it to fix a line, as the screen tells her to. The chairs must be offered again
    and be accepted on a new shared RFQ, an ordinary RFQ or a purchase order, as
    they would be after an ordinary RFQ is cancelled. While the shared RFQ is open
    they stay taken, and once it is awarded each branch's order holds its own.
    """

    stored = FreeRequisitionLinesTests.__dict__["stored"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_chairs = cls.stored(cls.multi, cls.lekki, 60)
        cls.ikeja_chairs = cls.stored(cls.multi, cls.ikeja, 40)

    def setUp(self):
        self.hq = self.client_for(self.school.tenant, f"bello-{self._testMethodName}@test.com")

    def url(self, path):
        return f"/v1/procurement/rfqs/{path}?entity={self.multi.entity.code}"

    def free_ids(self):
        response = self.hq.get(self.url("free-requisition-lines/"))
        self.assertEqual(response.status_code, 200, response.data)
        return {row["requisition_id"] for row in self.rows(response)}

    def shared_rfq(self, title="Shared chairs"):
        made = self.shared(self.hq, self.lekki_chairs, self.ikeja_chairs, title=title)
        self.assertEqual(made.status_code, 201, made.data)
        return RequestForQuotation.objects.get(pk=made.data["data"]["id"])

    def issued(self, rfq):
        from vs_procurement.sourcing import issue_rfq, set_rfq_invitations

        set_rfq_invitations(rfq, [self.multi.vendor])
        return issue_rfq(rfq, competition_exception_reason="One supplier stocks them.")

    def own_rfq(self, requisition):
        return self.hq.post(self.url(""), {
            "requisition": requisition.pk, "title": "Lekki chairs", "issue_date": "2026-01-12",
            "lines": [{"description": "Chair", "quantity": 60, "expense_account": "5300",
                       "requisition_line": requisition.lines.get().pk}],
        }, format="json")

    def test_cancelling_offers_the_lines_again(self, _permission):
        rfq = self.shared_rfq()
        self.assertEqual(self.free_ids(), set())

        cancelled = self.hq.post(self.url(f"{rfq.pk}/cancel/"), {"reason": "Fix a line"},
                                 format="json")

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(self.free_ids(), {self.lekki_chairs.pk, self.ikeja_chairs.pk})

    def test_cancelled_lines_go_on_a_new_shared_rfq(self, _permission):
        first = self.shared_rfq()
        self.hq.post(self.url(f"{first.pk}/cancel/"), {}, format="json")

        second = self.shared_rfq(title="Shared chairs, fixed")

        self.assertEqual(
            SharedSourcingAllocation.objects.filter(group__rfq=second).count(), 2)
        again = self.shared(self.hq, self.lekki_chairs, self.ikeja_chairs, title="Third")
        self.assertEqual(again.status_code, 400, again.data)
        self.assertIn(f"already on shared RFQ {second.document_number}", str(again.data))

    def test_cancelled_lines_go_on_an_ordinary_rfq_or_order(self, _permission):
        rfq = self.shared_rfq()
        self.hq.post(self.url(f"{rfq.pk}/cancel/"), {}, format="json")

        own = self.own_rfq(self.lekki_chairs)
        po = create_po_from_requisition(
            self.ikeja_chairs, vendor=self.multi.vendor, order_date=datetime.date(2026, 1, 12))

        self.assertEqual(own.status_code, 201, own.data)
        self.assertEqual(po.branch_id, self.ikeja.pk)

    def test_closing_without_award_offers_the_lines_again(self, _permission):
        rfq = self.issued(self.shared_rfq())
        self.assertEqual(self.free_ids(), set())

        closed = self.hq.post(self.url(f"{rfq.pk}/close/"), {"reason": "No fair price"},
                              format="json")

        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(self.free_ids(), {self.lekki_chairs.pk, self.ikeja_chairs.pk})

    def test_an_open_shared_rfq_keeps_its_lines(self, _permission):
        self.issued(self.shared_rfq())

        self.assertEqual(self.free_ids(), set())
        self.assertEqual(self.own_rfq(self.lekki_chairs).status_code, 400)
        with self.assertRaises(RequisitionError):
            create_po_from_requisition(
                self.ikeja_chairs, vendor=self.multi.vendor,
                order_date=datetime.date(2026, 1, 12))

    def test_an_awarded_shared_rfq_keeps_its_lines(self, _permission):
        from vs_procurement.exceptions import SourcingError
        from vs_procurement.models import VendorQuotation
        from vs_procurement.sourcing import award_quotation, cancel_rfq, submit_quotation

        rfq = self.issued(self.shared_rfq())
        quote = self.hq.post(f"/v1/procurement/quotations/?entity={self.multi.entity.code}", {
            "rfq": rfq.pk, "vendor": self.multi.vendor.code, "quote_date": "2026-01-13",
            "lines": [{"rfq_line": rfq.lines.get().pk, "description": "Chair", "quantity": 100,
                       "unit_price": 100_000, "expense_account": "5300"}],
        }, format="json")
        self.assertEqual(quote.status_code, 201, quote.data)
        quotation = VendorQuotation.objects.get(pk=quote.data["data"]["id"])
        submit_quotation(quotation)
        award_quotation(quotation, actor_user=self.hq.test_user,
                        competition_exception_reason="One supplier stocks them.")

        with self.assertRaises(SourcingError):
            cancel_rfq(rfq)
        self.assertEqual(self.free_ids(), set())
        self.assertEqual(self.own_rfq(self.lekki_chairs).status_code, 400)
        again = self.shared(self.hq, self.lekki_chairs, self.ikeja_chairs, title="Again")
        self.assertEqual(again.status_code, 400, again.data)

    def test_the_cancelled_rfq_keeps_its_branch_split_and_boundary(self, _permission):
        rfq = self.shared_rfq()
        self.hq.post(self.url(f"{rfq.pk}/cancel/"), {}, format="json")
        self.shared_rfq(title="Shared chairs, fixed")
        lekki_buyer = self.client_for(
            self.school.tenant, "lekki-only@test.com", branch=self.lekki)

        detail = self.hq.get(self.url(f"{rfq.pk}/"))
        hidden = lekki_buyer.get(self.url(f"{rfq.pk}/"))

        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["data"]["rfq_status"], "CANCELLED")
        self.assertEqual(len(detail.data["data"]["shared_sourcing"]["allocations"]), 2)
        self.assertEqual(hidden.status_code, 404, hidden.data)


class ReleaseBackfillTests(_SharedSourcingFixture):
    """Shared RFQs already cancelled or closed when release arrived give their lines back."""

    stored = FreeRequisitionLinesTests.__dict__["stored"]

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_only_ended_rfqs_are_released(self, _permission):
        import importlib

        from django.apps import apps

        from vs_procurement.constants import RfqStatus

        hq = self.client_for(self.school.tenant, "backfill@test.com")
        made = {}
        for status in (RfqStatus.CANCELLED, RfqStatus.CLOSED, RfqStatus.AWARDED, RfqStatus.ISSUED):
            lekki = self.stored(self.multi, self.lekki, 2)
            ikeja = self.stored(self.multi, self.ikeja, 3)
            response = self.shared(hq, lekki, ikeja, title=status)
            self.assertEqual(response.status_code, 201, response.data)
            made[status] = response.data["data"]["id"]
            RequestForQuotation.objects.filter(pk=made[status]).update(rfq_status=status)
        migration = importlib.import_module(
            "vs_procurement.migrations.0046_shared_sourcing_release")

        migration.release_ended_allocations(apps, None)

        released = set(
            SharedSourcingAllocation.objects.filter(released_at__isnull=False)
            .values_list("group__rfq_id", flat=True))
        self.assertEqual(released, {made[RfqStatus.CANCELLED], made[RfqStatus.CLOSED]})
