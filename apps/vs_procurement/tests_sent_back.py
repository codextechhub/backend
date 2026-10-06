"""Procurement lists say plainly what is with an approver and what has been sent back.

An order stays a draft while Mr Eze decides it, so it read "Draft" though it was
with him, where a requisition in the same place reads Pending Approval. It now
reads Pending Approval first, and the list's status filter agrees: Draft leaves
it out, Pending Approval keeps it, and the quick export takes the same rows.

Every procurement list offers "Sent back" (``?approval=returned``): only the
documents Mr Eze returned to whoever sent them, inside the reader's branches.
"""
from __future__ import annotations

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from vs_finance.constants import DocumentStatus

from .approvals import submit_for_approval
from .export_datasets import _translate_purchase_orders
from .purchasing import create_po_from_requisition
from .tests_returned_correction import JAN, _ReturnedFixture

PROCUREMENT_LISTS = (
    "requisitions/", "purchase-orders/", "vendor-invoices/", "vendor-payments/",
    "vendor-credit-notes/",
)


class _SentBackFixture(_ReturnedFixture):

    def listed(self, user, path, books=None, **params):
        query = "".join(f"&{key}={value}" for key, value in params.items())
        response = self.as_(user).get(self.url(books or self.books, path) + query)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def ids(self, response):
        return {row["id"] for row in response.data["data"]}

    def order(self):
        req = self.raise_requisition(self.bello, self.books)
        submit_for_approval(req, actor_user=self.bello)
        self.approve(req, self.eze)
        return create_po_from_requisition(req, vendor=self.vendor, order_date=JAN)


class AnOrderWithItsApproverReadsPendingTests(_SentBackFixture):

    def test_the_status_and_the_filter_agree(self):
        draft = self.order()
        waiting = self.order()
        submit_for_approval(waiting, actor_user=self.bello)
        returned = self.sent_and_returned(self.order(), self.bello, self.eze)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, DocumentStatus.DRAFT)

        rows = {row["id"]: row for row in self.listed(self.adaeze, "purchase-orders/").data["data"]}
        drafts = self.ids(self.listed(self.adaeze, "purchase-orders/", status="DRAFT"))
        pending = self.ids(self.listed(self.adaeze, "purchase-orders/", status="PENDING_APPROVAL"))

        self.assertEqual(rows[draft.pk]["display_status"], "DRAFT")
        self.assertEqual(rows[waiting.pk]["display_status"], "PENDING_APPROVAL")
        self.assertEqual(rows[returned.pk]["display_status"], "PENDING_APPROVAL")
        self.assertTrue(rows[returned.pk]["approval_returned"])
        self.assertEqual(drafts, {draft.pk})
        self.assertEqual(pending, {waiting.pk, returned.pk})


class TheOrderExportTakesTheSameRowsTests(SimpleTestCase):

    def test_draft_leaves_out_orders_with_an_approver_and_pending_keeps_them(self):
        drafts, _ = _translate_purchase_orders({"status": "DRAFT"})
        pending, _ = _translate_purchase_orders({"status": "PENDING_APPROVAL"})
        partial, unmapped = _translate_purchase_orders({"status": "PARTIAL"})

        self.assertIn({"id": "status", "values": ["DRAFT"]}, drafts)
        approval = next(f for f in drafts if f["id"] == "approval_state")
        self.assertNotIn("PENDING", approval["values"])
        self.assertEqual(pending, [{"id": "approval_state", "values": ["PENDING"]}])
        self.assertEqual(partial, [])
        self.assertEqual([u.param for u in unmapped], ["status"])


class SentBackFilterTests(_SentBackFixture):

    def test_it_keeps_only_documents_back_with_their_sender_inside_the_reader_branches(self):
        ikeja = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        waiting = self.raise_requisition(self.bello, self.books)
        submit_for_approval(waiting, actor_user=self.bello)
        never_sent = self.raise_requisition(self.bello, self.books)
        lekki = self.sent_and_returned(
            self.raise_requisition(self.ngozi, self.books), self.ngozi, self.eze)

        whole = self.ids(self.listed(self.adaeze, "requisitions/", approval="returned"))
        lekki_only = self.ids(self.listed(self.ngozi, "requisitions/", approval="returned"))

        self.assertEqual(whole, {ikeja.pk, lekki.pk})
        self.assertEqual(lekki_only, {lekki.pk})
        self.assertNotIn(waiting.pk, whole)
        self.assertNotIn(never_sent.pk, whole)

    def test_a_one_branch_school_sees_its_own_and_no_other_school(self):
        solo = self.sent_and_returned(
            self.raise_requisition(self.ada, self.solo_books), self.ada, self.femi)
        corona = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)

        self.assertEqual(
            self.ids(self.listed(self.ada, "requisitions/", self.solo_books, approval="returned")),
            {solo.pk})
        self.assertEqual(
            self.ids(self.listed(self.adaeze, "requisitions/", approval="returned")), {corona.pk})

    def test_the_page_count_and_the_query_count(self):
        self.sent_and_returned(self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        with CaptureQueriesContext(connection) as one:
            self.listed(self.adaeze, "requisitions/", approval="returned")
        for _ in range(2):
            self.sent_and_returned(
                self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        self.raise_requisition(self.bello, self.books)
        with CaptureQueriesContext(connection) as three:
            response = self.listed(self.adaeze, "requisitions/", approval="returned", page_size=2)

        self.assertEqual(response.data["pagination"]["totalItems"], 3)
        self.assertEqual(response.data["pagination"]["totalPages"], 2)
        self.assertEqual(len(three), len(one))

    def test_every_procurement_list_takes_the_filter_and_refuses_a_wrong_value(self):
        order = self.sent_and_returned(self.order(), self.bello, self.eze)
        self.assertEqual(
            self.ids(self.listed(self.adaeze, "purchase-orders/", approval="returned")),
            {order.pk})
        for path in PROCUREMENT_LISTS:
            with self.subTest(path=path):
                self.listed(self.adaeze, path, approval="returned")
                wrong = self.as_(self.adaeze).get(self.url(self.books, path) + "&approval=sent")
                self.assertEqual(wrong.status_code, 400, wrong.data)
                self.assertIn("approval", wrong.data["error"]["detail"])
