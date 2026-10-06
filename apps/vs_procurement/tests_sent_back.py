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
from .tests_returned_correction import BUYER_KEYS, JAN, _ReturnedFixture

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
        # The returned order wears Sent back, so no status word lists it.
        self.assertEqual(drafts, {draft.pk})
        self.assertEqual(pending, {waiting.pk})


class TheOrderExportTakesTheSameRowsTests(SimpleTestCase):

    def test_draft_leaves_out_orders_with_an_approver_and_pending_keeps_them(self):
        drafts, _ = _translate_purchase_orders({"status": "DRAFT"})
        pending, _ = _translate_purchase_orders({"status": "PENDING_APPROVAL"})
        partial, unmapped = _translate_purchase_orders({"status": "PARTIAL"})

        not_sent_back = {"id": "approval", "values": ["not_returned"]}
        self.assertIn({"id": "status", "values": ["DRAFT"]}, drafts)
        self.assertIn(not_sent_back, drafts)
        approval = next(f for f in drafts if f["id"] == "approval_state")
        self.assertNotIn("PENDING", approval["values"])
        self.assertEqual(pending, [{"id": "approval_state", "values": ["PENDING"]}, not_sent_back])
        self.assertEqual(partial, [not_sent_back])
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


def _january(filter_id):
    return {"id": filter_id, "start": "2026-01-01", "end": "2026-01-31"}


class SentBackExportTests(_SentBackFixture):
    """The Export button on a list filtered to Sent back exports what the list shows.

    Mrs Bello filters her requisitions to Sent back and sees the one Mr Eze
    returned; the file she exports holds that one, not every requisition in the
    books. Each export dataset behind a procurement list reads ``approval``
    through the rule the list reads it by, so the two cannot disagree.
    """

    RETURNED = {"id": "approval", "values": ["returned"]}

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        export_keys = BUYER_KEYS + ("exports.catalogue.view",)
        cls.okon = cls.person(cls.tenant, "okon", export_keys, None)
        cls.lekki_okon = cls.person(cls.tenant, "lekki-okon", export_keys, cls.lekki)

    def exported(self, user, dataset_key, *filters, books=None):
        from vs_exports.catalogue import ScopeContext, get_dataset
        from vs_exports.engine import build_queryset

        books = books or self.books
        scope = ScopeContext(tenant=books.tenant, entity=books, user=user)
        return set(build_queryset(get_dataset(dataset_key), scope, list(filters))
                   .values_list("pk", flat=True))

    def from_screen(self, user, screen, books=None, **params):
        query = "".join(f"&{key}={value}" for key, value in params.items())
        return self.as_(user).get(
            f"/v1/exports/from-screen/?screen={screen}&entity={(books or self.books).code}{query}")

    def requisitions(self):
        ikeja = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        waiting = self.raise_requisition(self.bello, self.books)
        submit_for_approval(waiting, actor_user=self.bello)
        never_sent = self.raise_requisition(self.bello, self.books)
        lekki = self.sent_and_returned(
            self.raise_requisition(self.ngozi, self.books), self.ngozi, self.eze)
        return ikeja, waiting, never_sent, lekki

    def test_it_holds_only_the_documents_sent_back_inside_the_reader_branches(self):
        ikeja, _, _, lekki = self.requisitions()
        january = _january("request_date")

        whole = self.exported(self.adaeze, "procurement.requisitions", january, self.RETURNED)
        lekki_only = self.exported(self.ngozi, "procurement.requisitions", january, self.RETURNED)

        self.assertEqual(whole, {ikeja.pk, lekki.pk})
        self.assertEqual(lekki_only, {lekki.pk})

    def test_without_it_the_export_is_unchanged(self):
        documents = {doc.pk for doc in self.requisitions()}
        january = _january("request_date")

        self.assertEqual(self.exported(self.adaeze, "procurement.requisitions", january), documents)
        self.assertEqual(
            self.exported(self.adaeze, "procurement.requisitions", january,
                          {"id": "approval", "values": []}),
            documents)

    def test_every_procurement_export_behind_a_sent_back_list_takes_it(self):
        requisition = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        self.raise_requisition(self.bello, self.books)
        order = self.sent_and_returned(self.order(), self.bello, self.eze)
        waiting_order = self.order()
        submit_for_approval(waiting_order, actor_user=self.bello)
        bill = self.sent_and_returned(self.raise_bill(self.bello, self.books), self.bello, self.eze)

        self.assertEqual(
            self.exported(self.adaeze, "procurement.requisitions",
                          _january("request_date"), self.RETURNED),
            {requisition.pk})
        self.assertEqual(
            self.exported(self.adaeze, "procurement.purchase_orders",
                          _january("order_date"), self.RETURNED),
            {order.pk})
        self.assertEqual(
            self.exported(self.adaeze, "procurement.vendor_invoices",
                          _january("invoice_date"), self.RETURNED),
            {bill.pk})

    def test_a_one_branch_school_exports_its_own_and_never_another_school_rows(self):
        solo = self.sent_and_returned(
            self.raise_requisition(self.ada, self.solo_books), self.ada, self.femi)
        corona = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        january = _january("request_date")

        self.assertEqual(
            self.exported(self.ada, "procurement.requisitions", january, self.RETURNED,
                          books=self.solo_books),
            {solo.pk})
        self.assertEqual(
            self.exported(self.adaeze, "procurement.requisitions", january, self.RETURNED),
            {corona.pk})
        other_school = self.from_screen(
            self.okon, "procurement.requisitions", self.solo_books, approval="returned")
        self.assertIn(other_school.status_code, (403, 404), other_school.data)

    def test_the_screen_export_carries_it_exactly(self):
        self.requisitions()

        response = self.from_screen(self.okon, "procurement.requisitions", approval="returned")
        lekki = self.from_screen(self.lekki_okon, "procurement.requisitions", approval="returned")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertIn(self.RETURNED, data["config"]["filters"])
        self.assertIn("approval", data["carried"])
        self.assertEqual(data["unmapped"], [])
        self.assertTrue(data["exact"])
        self.assertEqual(data["matching_rows"], 2)
        self.assertEqual(lekki.status_code, 200, lekki.data)
        self.assertEqual(lekki.data["data"]["matching_rows"], 1)

    def test_a_wrong_value_is_refused_as_the_list_refuses_it(self):
        from vs_exports.catalogue import FilterError, compile_filter, get_dataset

        listed = self.as_(self.okon).get(self.url(self.books, "requisitions/") + "&approval=sent")
        exported = self.from_screen(self.okon, "procurement.requisitions", approval="sent")

        self.assertEqual(listed.status_code, 400, listed.data)
        self.assertEqual(exported.status_code, 400, exported.data)
        self.assertEqual(exported.data["error"]["detail"]["approval"],
                         listed.data["error"]["detail"]["approval"])
        with self.assertRaises(FilterError) as refused:
            compile_filter(get_dataset("procurement.requisitions"),
                           {"id": "approval", "values": ["sent"]})
        self.assertEqual(refused.exception.filter_id, "approval")
        self.assertIn(str(refused.exception), str(listed.data["error"]["detail"]["approval"]))


class StatusWordsLeaveSentBackOutTests(_SentBackFixture):
    """A procurement status word lists the rows wearing it; one sent back wears Sent back.

    A requisition, order or bill an approver returns keeps its pending approval
    state, but its row reads Sent back, so Pending Approval leaves it out and
    ``?approval=returned`` finds it. A word the list does not have is refused.
    """

    def test_pending_approval_leaves_out_what_was_sent_back_in_the_list_and_the_export(self):
        from vs_exports.catalogue import ScopeContext, get_screen, resolve_screen
        from vs_exports.engine import build_queryset

        returned = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        waiting = self.raise_requisition(self.bello, self.books)
        submit_for_approval(waiting, actor_user=self.bello)

        listed = self.ids(self.listed(self.adaeze, "requisitions/", status="pending_approval"))
        binding = get_screen("procurement.requisitions")
        filters = resolve_screen(binding, {"status": "PENDING_APPROVAL"},
                                 today=JAN)["filters"]
        exported = set(build_queryset(
            binding.dataset, ScopeContext(tenant=self.tenant, entity=self.books, user=self.adaeze),
            filters,
        ).values_list("pk", flat=True))

        self.assertEqual(listed, {waiting.pk})
        self.assertEqual(exported, {waiting.pk})
        self.assertNotIn(returned.pk, listed | exported)

    def test_a_bill_tab_is_read_in_any_case_and_the_bill_sent_back_is_under_no_tab(self):
        returned = self.sent_and_returned(
            self.raise_bill(self.bello, self.books), self.bello, self.eze)

        pending = self.ids(self.listed(self.adaeze, "vendor-invoices/",
                                       display_status="pending_approval"))
        drafts = self.ids(self.listed(self.adaeze, "vendor-invoices/", display_status="Draft"))

        self.assertNotIn(returned.pk, pending | drafts)
        self.assertEqual(
            self.ids(self.listed(self.adaeze, "vendor-invoices/", approval="returned")),
            {returned.pk})

    def test_each_summary_tile_counts_what_its_tab_lists_and_sent_back_has_its_own(self):
        self.sent_and_returned(self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        waiting = self.raise_requisition(self.bello, self.books)
        submit_for_approval(waiting, actor_user=self.bello)
        self.raise_requisition(self.bello, self.books)
        self.sent_and_returned(self.raise_bill(self.bello, self.books), self.bello, self.eze)
        self.sent_and_returned(self.order(), self.bello, self.eze)

        def summary(path):
            response = self.as_(self.adaeze).get(self.url(self.books, path))
            self.assertEqual(response.status_code, 200, response.data)
            return response.data["data"]

        def count(path, **params):
            return len(self.ids(self.listed(self.adaeze, path, **params)))

        reqs = summary("requisitions/summary/")
        self.assertEqual(reqs["pending_approval"]["count"],
                         count("requisitions/", status="PENDING_APPROVAL"))
        self.assertEqual(reqs["pending_approval"]["count"], 1)
        self.assertEqual(reqs["draft"]["count"], count("requisitions/", status="DRAFT"))
        self.assertEqual(reqs["sent_back"]["count"], count("requisitions/", approval="returned"))
        bills = summary("vendor-invoices/summary/")
        self.assertEqual(bills["under_review"]["count"],
                         count("vendor-invoices/", display_status="PENDING_APPROVAL"))
        self.assertEqual(bills["under_review"]["count"], 0)
        self.assertEqual(bills["sent_back"]["count"], 1)
        self.assertEqual(summary("purchase-orders/summary/")["sent_back"]["count"],
                         count("purchase-orders/", approval="returned"))

    def test_every_procurement_list_refuses_a_word_it_does_not_have(self):
        for path, param in (("requisitions/", "status"), ("purchase-orders/", "status"),
                            ("vendor-invoices/", "display_status"),
                            ("vendor-invoices/", "status"), ("vendor-payments/", "status"),
                            ("vendor-payments/", "approval_state"),
                            ("vendor-credit-notes/", "status")):
            with self.subTest(path=path, param=param):
                wrong = self.as_(self.adaeze).get(self.url(self.books, path) + f"&{param}=NOPE")
                self.assertEqual(wrong.status_code, 400, wrong.data)
                self.assertIn(param, wrong.data["error"]["detail"])


class AScreenWhoseExportCannotReadApprovalSaysSoTests(SimpleTestCase):

    def test_the_vendor_export_reports_approval_as_unmapped(self):
        import datetime

        from vs_exports.catalogue import get_screen, resolve_screen

        resolved = resolve_screen(
            get_screen("procurement.vendors"), {"approval": "returned"},
            today=datetime.date(2026, 1, 31))

        self.assertFalse(resolved["exact"])
        self.assertEqual([u["param"] for u in resolved["unmapped"]], ["approval"])
