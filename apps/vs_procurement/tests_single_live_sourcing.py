"""A requisition line sits on one live sourcing at a time, ordinary or shared.

Ikeja asks for 40 chairs and the request is approved. Mrs Bello, the buyer, puts
the line on a quote request. While that request is a draft or out with vendors,
or once its award has raised an order, the 40 chairs are taken: a second quote
request, a shared one, or an order raised straight from the requisition is
refused and told where the chairs already are. Were it allowed, both quote
requests could be awarded and 80 chairs ordered. The chairs are free again when
the quote request is cancelled or closed without award, or when the order is
cancelled.

Awarding is the same sourcing carrying on, not a second one: the order an award
raises is never refused by its own quote request, and from then on the order is
what holds the line.
"""
from __future__ import annotations

import datetime
import threading
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, tag

from vs_finance.seed import seed_currencies
from vs_procurement.constants import RfqStatus
from vs_procurement.exceptions import RequisitionError, SourcingError
from vs_procurement.models import (
    PurchaseOrder,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    SharedSourcingAllocation,
    VendorQuotation,
)
from vs_procurement.purchasing import cancel_purchase_order, create_po_from_requisition
from vs_procurement.sourcing import (
    award_quotation,
    cancel_rfq,
    issue_rfq,
    set_rfq_invitations,
    submit_quotation,
)

from . import tests as p2p_tests
from . import tests_shared_sourcing_guards as guards

ORDER_DATE = datetime.date(2026, 1, 12)


class _SourcingHelpers:
    """Requests a buyer makes, shared by the single-process and the race tests."""

    def rfq_url(self, books, path=""):
        return f"/v1/procurement/rfqs/{path}?entity={books.entity.code}"

    def ordinary_rfq(self, client, books, requisition, *, title="Ikeja chairs", **extra):
        """POST an ordinary RFQ for ``requisition``'s single line."""
        line = requisition.lines.get()
        return client.post(self.rfq_url(books), {
            "requisition": requisition.pk, "title": title, "issue_date": "2026-01-12",
            "lines": [{"description": "Chair", "quantity": str(line.quantity),
                       "expense_account": "5300", "requisition_line": line.pk}],
            **extra,
        }, format="json")


class _SingleSourcingFixture(_SourcingHelpers, guards._SharedSourcingFixture):
    """Bright Star Group with Lekki and Ikeja, and Sunrise School with one branch."""

    stored = guards.FreeRequisitionLinesTests.__dict__["stored"]

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.tests.helpers import make_branch, make_school

        super().setUpTestData()
        cls.solo_school = make_school(slug="single-source-solo", name="Sunrise School",
                                      status="ACTIVE")
        cls.solo_branch = make_branch(cls.solo_school, name="Main Branch")
        cls.solo = cls.build_books("SOLOSRC", cls.solo_school.tenant)
        cls.ikeja_chairs = cls.stored(cls.multi, cls.ikeja, 40)
        cls.lekki_chairs = cls.stored(cls.multi, cls.lekki, 60)
        cls.solo_chairs = cls.stored(cls.solo, cls.solo_branch, 25)

    def setUp(self):
        self.bello = self.client_for(self.school.tenant, f"bello-{self._testMethodName}@test.com")

    def created(self, response):
        self.assertEqual(response.status_code, 201, response.data)
        return RequestForQuotation.objects.get(pk=response.data["data"]["id"])

    def refused(self, response, where):
        """Assert a 400 that names the requisition, its line and where it is sourced."""
        self.assertEqual(response.status_code, 400, response.data)
        text = str(response.data)
        self.assertIn("Line 1 ('Chair')", text)
        self.assertIn(self.ikeja_chairs.document_number, text)
        self.assertIn(where, text)

    def issued(self, rfq):
        set_rfq_invitations(rfq, [self.multi.vendor])
        return issue_rfq(rfq, competition_exception_reason="One supplier stocks them.")

    def free_ids(self):
        response = self.bello.get(self.rfq_url(self.multi, "free-requisition-lines/"))
        self.assertEqual(response.status_code, 200, response.data)
        return {row["requisition_id"] for row in self.rows(response)}

    def quote_and_award(self, rfq, quantity):
        """Have Acme quote for every line of ``rfq`` and award it; returns the orders."""
        response = self.bello.post(f"/v1/procurement/quotations/?entity={self.multi.entity.code}", {
            "rfq": rfq.pk, "vendor": self.multi.vendor.code, "quote_date": "2026-01-13",
            "lines": [{"rfq_line": line.pk, "description": "Chair", "quantity": quantity,
                       "unit_price": 100_000, "expense_account": "5300"}
                      for line in rfq.lines.filter(is_active=True)],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        quotation = VendorQuotation.objects.get(pk=response.data["data"]["id"])
        submit_quotation(quotation)
        return quotation


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class OrdinaryRfqSingleSourcingTests(_SingleSourcingFixture):
    """A line on a live ordinary RFQ is not put out to tender or ordered again."""

    def test_a_second_live_rfq_for_the_same_line_is_refused(self, _permission):
        first = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        again = self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again")

        self.refused(again, f"already on RFQ {first.document_number}")
        self.assertEqual(RequestForQuotation.objects.filter(title="Again").count(), 0)

    def test_an_issued_rfq_holds_its_line_too(self, _permission):
        self.issued(self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))

        again = self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again")

        self.assertEqual(again.status_code, 400, again.data)

    def test_cancelling_the_first_lets_the_line_go_on_a_new_rfq(self, _permission):
        first = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        cancelled = self.bello.post(self.rfq_url(self.multi, f"{first.pk}/cancel/"),
                                    {"reason": "Wrong spec"}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)

        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again"))

    def test_closing_the_first_without_award_frees_the_line(self, _permission):
        first = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        closed = self.bello.post(self.rfq_url(self.multi, f"{first.pk}/close/"),
                                 {"reason": "No fair price"}, format="json")
        self.assertEqual(closed.status_code, 200, closed.data)

        self.assertIn(self.ikeja_chairs.pk, self.free_ids())
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again"))

    def test_a_line_on_a_live_ordinary_rfq_is_not_free(self, _permission):
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        self.assertEqual(self.free_ids(), {self.lekki_chairs.pk})

    def test_a_shared_rfq_cannot_take_a_line_on_a_live_ordinary_rfq(self, _permission):
        first = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        shared = self.shared(self.bello, self.lekki_chairs, self.ikeja_chairs)

        self.refused(shared, f"already on RFQ {first.document_number}")

    def test_an_order_from_the_requisition_is_refused_while_the_rfq_is_live(self, _permission):
        first = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))

        with self.assertRaisesMessage(RequisitionError, f"already on RFQ {first.document_number}"):
            create_po_from_requisition(
                self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)
        self.assertFalse(PurchaseOrder.objects.filter(requisition=self.ikeja_chairs).exists())

    def test_editing_a_draft_keeps_its_own_line(self, _permission):
        rfq = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        line = self.ikeja_chairs.lines.get()

        edited = self.bello.patch(self.rfq_url(self.multi, f"{rfq.pk}/"), {"lines": [{
            "description": "Chair", "quantity": "40", "expense_account": "5300",
            "requisition_line": line.pk,
        }]}, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)

    def test_editing_a_draft_cannot_take_a_line_on_another_rfq(self, _permission):
        first = self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))
        other = self.created(self.bello.post(self.rfq_url(self.multi), {
            "title": "Standalone", "issue_date": "2026-01-12", "branch": self.ikeja.pk,
            "lines": [{"description": "Desk", "quantity": 1, "expense_account": "5300"}],
        }, format="json"))

        edited = self.bello.patch(self.rfq_url(self.multi, f"{other.pk}/"), {"lines": [{
            "description": "Chair", "quantity": "40", "expense_account": "5300",
            "requisition_line": self.ikeja_chairs.lines.get().pk,
        }]}, format="json")

        self.refused(edited, f"already on RFQ {first.document_number}")

    def test_amending_an_issued_rfq_keeps_its_own_line(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))

        amended = self.bello.post(self.rfq_url(self.multi, f"{rfq.pk}/amendments/"), {
            "summary": "Armless chairs", "lines": [{
                "description": "Chair", "quantity": "40", "expense_account": "5300",
                "requisition_line": self.ikeja_chairs.lines.get().pk,
            }],
        }, format="json")

        self.assertEqual(amended.status_code, 200, amended.data)

    def test_a_one_branch_school_is_held_to_the_same_rule(self, _permission):
        sunrise = self.client_for(self.solo_school.tenant, "sunrise-buyer@test.com")
        first = self.created(self.ordinary_rfq(sunrise, self.solo, self.solo_chairs))

        again = self.ordinary_rfq(sunrise, self.solo, self.solo_chairs, title="Again")

        self.assertEqual(again.status_code, 400, again.data)
        self.assertIn(f"already on RFQ {first.document_number}", str(again.data))

    def test_another_schools_rfq_cannot_name_this_schools_line(self, _permission):
        sunrise = self.client_for(self.solo_school.tenant, "sunrise-thief@test.com")
        line = self.ikeja_chairs.lines.get()

        response = sunrise.post(self.rfq_url(self.solo), {
            "title": "Borrowed", "issue_date": "2026-01-12",
            "lines": [{"description": "Chair", "quantity": "40", "expense_account": "5300",
                       "requisition_line": line.pk}],
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotIn(self.ikeja_chairs.document_number, str(response.data))


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class OrderSingleSourcingTests(_SingleSourcingFixture):
    """A line on a live purchase order is not put out to tender or ordered again."""

    def test_a_line_on_a_live_order_is_refused_on_a_new_rfq(self, _permission):
        po = create_po_from_requisition(
            self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)

        response = self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)

        self.refused(response, f"already on purchase order {po.document_number}")

    def test_a_second_order_from_the_same_requisition_is_refused(self, _permission):
        po = create_po_from_requisition(
            self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)

        with self.assertRaisesMessage(
            RequisitionError, f"already on purchase order {po.document_number}",
        ):
            create_po_from_requisition(
                self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)

    def test_cancelling_the_order_frees_the_line(self, _permission):
        po = create_po_from_requisition(
            self.ikeja_chairs, vendor=self.multi.vendor, order_date=ORDER_DATE)
        cancel_purchase_order(po, reason="Supplier closed down")

        self.assertIn(self.ikeja_chairs.pk, self.free_ids())
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs))


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class AwardSingleSourcingTests(_SingleSourcingFixture):
    """An award's order is its own RFQ carrying on, and then holds the line itself."""

    def test_awarding_raises_its_order_and_the_order_holds_the_line(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        quotation = self.quote_and_award(rfq, 40)

        po = award_quotation(quotation, competition_exception_reason="One supplier.")

        self.assertEqual(po.lines.get().requisition_line, self.ikeja_chairs.lines.get())
        self.assertNotIn(self.ikeja_chairs.pk, self.free_ids())
        self.refused(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again"),
                     f"already on purchase order {po.document_number}")

    def test_cancelling_an_awarded_order_frees_the_line(self, _permission):
        rfq = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        po = award_quotation(self.quote_and_award(rfq, 40),
                             competition_exception_reason="One supplier.")

        cancel_purchase_order(po, reason="Supplier closed down")

        self.assertIn(self.ikeja_chairs.pk, self.free_ids())
        self.created(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs, title="Again"))

    def test_two_live_rfqs_from_before_the_rule_award_only_once_one_is_cancelled(self, _permission):
        first = self.issued(self.created(
            self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs)))
        second = self.issued(self.created(self.bello.post(self.rfq_url(self.multi), {
            "title": "Second", "issue_date": "2026-01-12", "branch": self.ikeja.pk,
            "lines": [{"description": "Chair", "quantity": "40", "expense_account": "5300"}],
        }, format="json")))
        # The second request was raised before the rule, naming the same line.
        RfqLine.objects.filter(rfq=second).update(requisition_line=self.ikeja_chairs.lines.get())
        first_quote = self.quote_and_award(first, 40)
        second_quote = self.quote_and_award(second, 40)

        with self.assertRaisesMessage(
            SourcingError, f"already on RFQ {second.document_number}",
        ):
            award_quotation(first_quote, competition_exception_reason="One supplier.")
        self.assertFalse(PurchaseOrder.objects.filter(entity=self.multi.entity).exists())

        cancel_rfq(second, reason="Raised twice")
        po = award_quotation(first_quote, competition_exception_reason="One supplier.")

        self.assertEqual(po.lines.get().requisition_line, self.ikeja_chairs.lines.get())
        second.refresh_from_db()
        self.assertEqual(second.rfq_status, RfqStatus.CANCELLED)

    def test_a_shared_award_hands_each_line_to_its_branch_order(self, _permission):
        made = self.shared(self.bello, self.lekki_chairs, self.ikeja_chairs)
        rfq = self.issued(self.created(made))
        quotation = self.quote_and_award(rfq, 100)

        award_quotation(quotation, actor_user=self.bello.test_user,
                        competition_exception_reason="One supplier.")

        self.assertFalse(SharedSourcingAllocation.objects.filter(
            group__rfq=rfq, released_at__isnull=True).exists())
        ikeja_order = PurchaseOrder.objects.get(
            shared_sourcing_order__group__rfq=rfq, branch=self.ikeja)
        self.assertEqual(self.free_ids(), set())
        self.refused(self.ordinary_rfq(self.bello, self.multi, self.ikeja_chairs),
                     f"already on purchase order {ikeja_order.document_number}")

        cancel_purchase_order(ikeja_order, reason="Ikeja found chairs elsewhere")

        self.assertEqual(self.free_ids(), {self.ikeja_chairs.pk})
        again = self.shared(self.bello, self.ikeja_chairs, self.stored(self.multi, self.lekki, 5))
        self.assertEqual(again.status_code, 201, again.data)


class AwardedReleaseBackfillTests(_SingleSourcingFixture):
    """Shared RFQs awarded before awards released their allocations are released."""

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_only_awarded_rfqs_are_released(self, _permission):
        import importlib

        from django.apps import apps

        made = {}
        for status in (RfqStatus.AWARDED, RfqStatus.ISSUED):
            response = self.shared(self.bello, self.stored(self.multi, self.lekki, 2),
                                   self.stored(self.multi, self.ikeja, 3), title=status)
            made[status] = self.created(response).pk
            RequestForQuotation.objects.filter(pk=made[status]).update(rfq_status=status)
        migration = importlib.import_module(
            "vs_procurement.migrations.0047_release_awarded_shared_lines")

        migration.release_awarded_allocations(apps, None)

        released = set(
            SharedSourcingAllocation.objects.filter(released_at__isnull=False)
            .values_list("group__rfq_id", flat=True))
        self.assertEqual(released, {made[RfqStatus.AWARDED]})


@tag("slow")
class RfqRaceTests(_SourcingHelpers, p2p_tests._P2PFixtureMixin, TransactionTestCase):
    """Two buyers putting the same chairs on two quote requests at once: one wins."""

    serialized_rollback = True

    build_books = guards._fixture["build_books"]
    client_for = guards._fixture["client_for"]

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_two_rfqs_racing_for_one_line_only_one_wins(self, _permission):
        from vs_procurement.purchasing import approve_requisition, submit_requisition
        from vs_procurement.models import PurchaseRequisition
        from vs_procurement.views import orders as order_views
        from vs_rbac.tests.helpers import make_branch, make_school

        seed_currencies()
        school = make_school(slug="race-school", name="Bright Star Race", status="ACTIVE")
        branch = make_branch(school, name="Ikeja Branch")
        books = self.build_books("RACESRC", school.tenant)
        requisition = PurchaseRequisition.objects.create(
            entity=books.entity, branch=branch, title="Chairs",
            request_date=datetime.date(2026, 1, 10),
        )
        PurchaseRequisitionLine.objects.create(
            requisition=requisition, description="Chair", quantity=40,
            estimated_unit_price=100_000, expense_account=self.acc(books.entity, "5300"),
            line_no=1,
        )
        submit_requisition(requisition)
        approve_requisition(requisition)
        clients = {name: self.client_for(school.tenant, f"{name}@race.test")
                   for name in ("first", "second")}

        first_written = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_done = threading.Event()
        responses = {}
        real_invite = order_views.sourcing.set_rfq_invitations

        def holding_invite(rfq, vendors, **kwargs):
            # The first request holds its transaction open after writing its line.
            if rfq.title == "first":
                first_written.set()
                if not release_first.wait(5):
                    raise TimeoutError("the race did not release the first request")
            return real_invite(rfq, vendors, **kwargs)

        def worker(name, *, attempting=None, done=None):
            close_old_connections()
            try:
                if attempting:
                    attempting.set()
                responses[name] = self.ordinary_rfq(
                    clients[name], books, requisition, title=name, invited_vendors=[])
            finally:
                close_old_connections()
                if done:
                    done.set()

        first = threading.Thread(target=worker, args=("first",), daemon=True)
        second = threading.Thread(
            target=worker, args=("second",),
            kwargs={"attempting": second_attempting, "done": second_done}, daemon=True,
        )
        with patch.object(order_views.sourcing, "set_rfq_invitations", side_effect=holding_invite):
            first.start()
            try:
                self.assertTrue(first_written.wait(5))
                second.start()
                self.assertTrue(second_attempting.wait(5))
                # The second waits on the line lock the first holds.
                self.assertFalse(second_done.wait(0.25))
            finally:
                release_first.set()
            first.join(5)
            second.join(5)

        self.assertEqual(responses["first"].status_code, 201, responses["first"].data)
        self.assertEqual(responses["second"].status_code, 400, responses["second"].data)
        self.assertIn("already on RFQ", str(responses["second"].data))
        self.assertEqual(RfqLine.objects.filter(
            requisition_line__requisition=requisition).count(), 1)
