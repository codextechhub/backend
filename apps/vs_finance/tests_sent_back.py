"""Sent back: what a document returned to its sender reads as, and what can still be done to it.

Mr Adeyemi returns Mrs Okafor's journal, write-off request and expense claim to
her. Each list offers "Sent back" (``?approval=returned``), which finds exactly
the documents whose latest approval request is back with whoever sent it, the
rows reading ``approval_returned: true``: not the ones still with Mr Adeyemi,
not drafts never sent, and not one that was withdrawn and sent again. The filter
narrows inside the reader's branch reach and tenant, before the page is cut.

A returned document is not ended underneath its open request: rejecting the
returned expense claim is refused until the request is withdrawn.
"""
from __future__ import annotations

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

from vs_workflow.models import WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import withdraw
from vs_workflow.services.submission import submit_for_approval

from .constants import DocumentStatus
from .models import ExpenseClaim, JournalEntry, WriteOffRequest
from .tests_returned_correction import ENTRY_KEYS, _ReturnedFixture

VIEW_KEYS = (
    "finance.journal.view", "finance.creditnote.view", "finance.refund.view",
    "finance.writeoff.view", "finance.provision.view", "finance.credittransfer.view",
    "finance.concession.view", "finance.banktransaction.view", "finance.banktransfer.view",
    "finance.interbranch.view", "finance.expenseclaim.view", "finance.pettycash.view",
)

#: Every finance list that can show a document an approver sent back.
FINANCE_LISTS = (
    "journals/", "credit-notes/", "refunds/", "write-offs/", "ar-adjustments/",
    "provisions/", "credit-transfers/", "concessions/", "bank-transactions/",
    "bank-transfers/", "inter-branch-transfers/", "expense-claims/", "petty-cash-returns/",
)


class _SentBackFixture(_ReturnedFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reader = cls.bursar(None, keys=VIEW_KEYS + ENTRY_KEYS)
        cls.lekki_reader = cls.bursar(cls.lekki, keys=VIEW_KEYS + ENTRY_KEYS)
        cls.solo_reader = cls.bursar(None, tenant=cls.solo_tenant, keys=VIEW_KEYS + ENTRY_KEYS)
        template = WorkflowTemplate.objects.create(
            tenant=cls.tenant, branch=None, document_type="finance.write_off",
            code="standard", name="Second signature")
        WorkflowStage.objects.create(
            template=template, code="signer", label="Second signature", order=10,
            approver_role_key="finance-approver")

    def listed(self, user, path="journals/", books=None, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.as_(user).get(self.url(f"{path}?{query}" if query else path, books))
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def ids(self, response):
        return {row["id"] for row in response.data["data"]}

    def claim(self):
        made = self.client_.post(self.url("expense-claims/"), {
            "claimant_name": "Mrs Okafor", "claim_date": "2026-01-10", "title": "Bank visit",
            "lines": [{"description": "Taxi", "expense_account": "5300",
                       "quantity": 1, "unit_price": 5_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        sent = self.client_.post(self.url(f"expense-claims/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        return pk


class SentBackFilterTests(_SentBackFixture):

    def test_it_keeps_only_documents_back_with_their_sender(self):
        returned = self.direct_entry(self.okafor)
        self.returned(JournalEntry, returned)
        with_approver = self.direct_entry(self.okafor)
        never_sent = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=datetime.date(2026, 1, 15),
            period=JournalEntry.objects.get(pk=returned).period, narration="Draft").pk

        response = self.listed(self.reader, approval="returned")

        self.assertEqual(self.ids(response), {returned})
        self.assertTrue(response.data["data"][0]["approval_returned"])
        everything = self.ids(self.listed(self.reader))
        self.assertTrue({returned, with_approver, never_sent} <= everything)

    def test_a_document_withdrawn_and_sent_again_is_judged_by_its_latest_request(self):
        again_with_approver = self.direct_entry(self.okafor)
        returned_again = self.direct_entry(self.okafor)
        for pk in (again_with_approver, returned_again):
            instance = self.returned(JournalEntry, pk)
            withdraw(instance.id, instance.requested_by)
            sent = self.client_.post(self.url(f"journals/{pk}/submit/"), {}, format="json")
            self.assertEqual(sent.status_code, 200, sent.data)
        self.returned(JournalEntry, returned_again)

        self.assertEqual(self.ids(self.listed(self.reader, approval="returned")), {returned_again})

    def test_it_stays_inside_the_reader_branches(self):
        ikeja = self.direct_entry(self.okafor)
        self.returned(JournalEntry, ikeja)
        lekki = self.direct_entry(self.lekki_bursar)
        self.returned(JournalEntry, lekki)

        self.assertEqual(self.ids(self.listed(self.lekki_reader, approval="returned")), {lekki})
        self.assertEqual(self.ids(self.listed(self.reader, approval="returned")), {ikeja, lekki})

    def test_a_one_branch_school_finds_its_own_and_never_another_school_rows(self):
        corona = self.direct_entry(self.okafor)
        self.returned(JournalEntry, corona)
        solo = self.direct_entry(self.solo_bursar, books=self.solo_books)
        self.returned(JournalEntry, solo)

        self.assertEqual(
            self.ids(self.listed(self.solo_reader, books=self.solo_books, approval="returned")),
            {solo},
        )
        self.assertEqual(self.ids(self.listed(self.reader, approval="returned")), {corona})

    def test_the_page_count_counts_only_the_documents_sent_back(self):
        for _ in range(3):
            self.returned(JournalEntry, self.direct_entry(self.okafor))
        self.direct_entry(self.okafor)

        response = self.listed(self.reader, approval="returned", page_size=2)

        self.assertEqual(response.data["pagination"]["totalItems"], 3)
        self.assertEqual(response.data["pagination"]["totalPages"], 2)
        self.assertEqual(len(response.data["data"]), 2)

    def test_a_page_costs_the_same_queries_however_many_were_sent_back(self):
        self.returned(JournalEntry, self.direct_entry(self.okafor))
        with CaptureQueriesContext(connection) as one:
            self.listed(self.reader, approval="returned")
        for _ in range(3):
            self.returned(JournalEntry, self.direct_entry(self.okafor))
        with CaptureQueriesContext(connection) as four:
            response = self.listed(self.reader, approval="returned")

        self.assertEqual(len(response.data["data"]), 4)
        self.assertEqual(len(four), len(one))

    def test_every_finance_list_takes_the_filter_and_refuses_a_wrong_value(self):
        for path in FINANCE_LISTS:
            with self.subTest(path=path):
                self.listed(self.reader, path, approval="returned")
                wrong = self.as_(self.reader).get(self.url(f"{path}?approval=yes"))
                self.assertEqual(wrong.status_code, 400, wrong.data)
                self.assertIn("approval", wrong.data["error"]["detail"])


class SentBackExportTests(_SentBackFixture):
    """The Export button on a finance list filtered to Sent back exports what the list shows.

    Mrs Okafor filters the journals to Sent back and sees the one Mr Adeyemi
    returned; the postings she exports are that journal's lines, not every line
    in January. The postings dataset reads a line's journal, the claims dataset
    the claim itself, both through the rule the lists use.
    """

    RETURNED = {"id": "approval", "values": ["returned"]}
    JANUARY = {"start": "2026-01-01", "end": "2026-01-31"}

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.exporter = cls.bursar(None, keys=VIEW_KEYS + ("exports.catalogue.view",))

    def exported(self, user, dataset_key, *filters, books=None, read="pk"):
        from vs_exports.catalogue import ScopeContext, get_dataset
        from vs_exports.engine import build_queryset

        books = books or self.books
        scope = ScopeContext(tenant=books.tenant, entity=books, user=user)
        return set(build_queryset(get_dataset(dataset_key), scope, list(filters))
                   .values_list(read, flat=True))

    def postings(self, user, *filters, books=None):
        return self.exported(user, "finance.gl_postings", {"id": "entry_date", **self.JANUARY},
                             *filters, books=books, read="entry_id")

    def test_postings_hold_only_the_lines_of_journals_sent_back_inside_the_reader_branches(self):
        ikeja = self.direct_entry(self.okafor)
        self.returned(JournalEntry, ikeja)
        lekki = self.direct_entry(self.lekki_bursar)
        self.returned(JournalEntry, lekki)
        with_approver = self.direct_entry(self.okafor)

        self.assertEqual(self.postings(self.reader, self.RETURNED), {ikeja, lekki})
        self.assertEqual(self.postings(self.lekki_reader, self.RETURNED), {lekki})
        self.assertTrue({ikeja, lekki, with_approver} <= self.postings(self.reader))

    def test_claims_hold_only_the_claims_sent_back(self):
        returned = self.claim()
        self.returned(ExpenseClaim, returned)
        with_approver = self.claim()
        claims = ("finance.expense_claims", {"id": "claim_date", **self.JANUARY})

        self.assertEqual(self.exported(self.reader, *claims, self.RETURNED), {returned})
        self.assertEqual(self.exported(self.reader, *claims), {returned, with_approver})

    def test_a_one_branch_school_exports_its_own_and_never_another_school_rows(self):
        corona = self.direct_entry(self.okafor)
        self.returned(JournalEntry, corona)
        solo = self.direct_entry(self.solo_bursar, books=self.solo_books)
        self.returned(JournalEntry, solo)

        self.assertEqual(
            self.postings(self.solo_reader, self.RETURNED, books=self.solo_books), {solo})
        self.assertEqual(self.postings(self.reader, self.RETURNED), {corona})

    def test_the_screen_export_carries_it_and_refuses_what_the_list_refuses(self):
        returned = self.claim()
        self.returned(ExpenseClaim, returned)
        self.claim()

        def from_screen(value):
            return self.as_(self.exporter).get(
                "/v1/exports/from-screen/?screen=finance.expense_claims"
                f"&entity={self.books.code}&approval={value}")

        carried = from_screen("returned")
        refused = from_screen("yes")
        listed = self.as_(self.exporter).get(self.url("expense-claims/?approval=yes"))

        self.assertEqual(carried.status_code, 200, carried.data)
        self.assertIn(self.RETURNED, carried.data["data"]["config"]["filters"])
        self.assertTrue(carried.data["data"]["exact"])
        self.assertEqual(carried.data["data"]["matching_rows"], 1)
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["error"]["detail"]["approval"],
                         listed.data["error"]["detail"]["approval"])


class RefundsAndWriteOffsTests(_SentBackFixture):

    def write_off(self, amount):
        return WriteOffRequest.objects.create(
            entity=self.books, branch=self.ikeja, invoice=self.bill, amount=amount,
            write_off_date=datetime.date(2026, 1, 20), reason="Family left Lagos",
        )

    def test_each_row_says_where_it_stands_and_sent_back_finds_the_returned_one(self):
        returned = self.write_off(10_000_00)
        submit_for_approval(returned, requested_by=self.okafor)
        self.returned(WriteOffRequest, returned.pk)
        draft = self.write_off(20_000_00)

        rows = {row["write_off_id"]: row for row in
                self.listed(self.reader, "ar-adjustments/").data["data"]}
        sent_back = self.listed(self.reader, "ar-adjustments/", approval="returned").data["data"]

        self.assertEqual((rows[returned.pk]["approval_state"], rows[returned.pk]["approval_returned"]),
                         ("PENDING", True))
        self.assertEqual((rows[draft.pk]["approval_state"], rows[draft.pk]["approval_returned"]),
                         ("NOT_SUBMITTED", False))
        self.assertEqual([row["write_off_id"] for row in sent_back], [returned.pk])

    def test_the_rows_are_read_in_a_fixed_number_of_queries(self):
        self.write_off(1_000_00)
        with CaptureQueriesContext(connection) as one:
            self.listed(self.reader, "ar-adjustments/")
        for amount in (2_000_00, 3_000_00, 4_000_00):
            self.write_off(amount)
        with CaptureQueriesContext(connection) as four:
            self.listed(self.reader, "ar-adjustments/")

        self.assertEqual(len(four), len(one))


class AReturnedClaimIsNotRejectedUnderItsRequestTests(_SentBackFixture):

    def test_rejecting_a_returned_claim_is_refused_until_its_request_is_withdrawn(self):
        approver = self.bursar(self.ikeja, keys=ENTRY_KEYS + ("finance.expenseclaim.post",))
        pk = self.claim()
        instance = self.returned(ExpenseClaim, pk)

        refused = self.as_(approver).post(self.url(f"expense-claims/{pk}/reject/"), {}, format="json")

        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "APPROVAL_REQUEST_OPEN")
        self.assertIn("Withdraw the approval request first", refused.data["message"])
        self.assertEqual(ExpenseClaim.objects.get(pk=pk).status, DocumentStatus.DRAFT)

        withdraw(instance.id, instance.requested_by)
        rejected = self.as_(approver).post(self.url(f"expense-claims/{pk}/reject/"), {}, format="json")
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(ExpenseClaim.objects.get(pk=pk).status, DocumentStatus.CANCELLED)

