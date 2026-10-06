"""The batch list's words, its rows and its tiles agree, and none of them counts a batch sent back.

Corona's bursar, Mrs Okafor, builds four payout batches at Ikeja. She leaves
one as a draft, sends one to Mr Adeyemi who still has it, sends one he returns
to her, and one is approved and paid out. The Drafts tile ("awaiting submit")
counts the one she left, Awaiting approval the one with Mr Adeyemi, and Sent
back the one he returned; each status word lists the batches its tile counts,
and ``?approval=returned`` lists the returned one. A Lekki clerk sees none of
Ikeja's, and the one-branch school sees only its own.
"""
from __future__ import annotations

import itertools

from django.contrib.contenttypes.models import ContentType

from core.test_utils import TenantAPIClient
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_workflow.constants import WorkflowInstanceStatus as S
from vs_workflow.models import WorkflowInstance, WorkflowTemplate

from .constants import PayoutBatchStatus
from .models import PayoutBatch

_refs = itertools.count(1)


class BatchWordsTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okafor = cls.user_for(cls.tenant, "okafor@corona.test")
        cls.template = WorkflowTemplate.objects.create(
            tenant=cls.tenant, branch=None, document_type="payments.payout_batch",
            code="standard", name="Payout approval")
        cls.solo_template = WorkflowTemplate.objects.create(
            tenant=cls.solo_tenant, branch=None, document_type="payments.payout_batch",
            code="standard", name="Payout approval")
        cls.left = cls.batch(cls.books, cls.ikeja)
        cls.with_approver = cls.batch(cls.books, cls.ikeja, request=S.IN_PROGRESS)
        cls.sent_back = cls.batch(cls.books, cls.ikeja, request=S.RETURNED)
        cls.approved = cls.batch(cls.books, cls.ikeja, request=S.APPROVED)
        cls.rejected = cls.batch(cls.books, cls.ikeja, request=S.REJECTED)
        cls.paid = cls.batch(cls.books, cls.ikeja, status=PayoutBatchStatus.COMPLETED,
                             request=S.APPROVED)
        cls.lekki_sent_back = cls.batch(cls.books, cls.lekki, request=S.RETURNED)
        cls.solo_sent_back = cls.batch(cls.solo_books, cls.solo_main, request=S.RETURNED)
        keys = ("payments.payout.view",)
        cls.whole = cls.grant(cls.user_for(cls.tenant, "hq@corona.test"), *keys,
                              tenant=cls.tenant, role_key="hq-payouts")
        cls.lekki_clerk = cls.grant(cls.user_for(cls.tenant, "lekki@corona.test"), *keys,
                                    tenant=cls.tenant, role_key="lekki-payouts",
                                    branch=cls.lekki)
        cls.solo_clerk = cls.grant(cls.user_for(cls.solo_tenant, "solo@single.test"), *keys,
                                   tenant=cls.solo_tenant, role_key="solo-payouts")

    @classmethod
    def batch(cls, books, branch, *, status=PayoutBatchStatus.DRAFT, request=None):
        batch = PayoutBatch.objects.create(
            entity=books, branch=branch, provider="PAYSTACK",
            reference=f"PB-WORDS-{next(_refs)}", status=status,
            total_amount=10_000, item_count=1)
        if request is not None:
            WorkflowInstance.all_objects.create(
                tenant=books.tenant, branch=branch,
                template=cls.template if books == cls.books else cls.solo_template,
                document_content_type=ContentType.objects.get_for_model(PayoutBatch),
                document_object_id=str(batch.pk), document_type="payments.payout_batch",
                status=request, requested_by=cls.okafor)
        return batch.pk

    def get(self, user, path, books=None, **params):
        books = books or self.books
        query = "".join(f"&{key}={value}" for key, value in params.items())
        return TenantAPIClient(user=user).get(
            f"/v1/payments/payout-batches/{path}?entity={books.code}{query}")

    def listed(self, user=None, books=None, **params):
        response = self.get(user or self.whole, "", books, page_size=100, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"]: row for row in response.data["data"]}

    def summary(self, user=None, books=None):
        response = self.get(user or self.whole, "summary/", books)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_each_word_lists_the_batches_wearing_it_and_never_one_sent_back(self):
        expected = {
            "DRAFT": {self.left, self.rejected},
            "PENDING_APPROVAL": {self.with_approver},
            "PROCESSING": {self.approved},
            "COMPLETED": {self.paid},
            "PARTIALLY_COMPLETED": set(),
            "FAILED": set(),
        }
        for word, pks in expected.items():
            with self.subTest(word=word):
                rows = self.listed(status=word.lower())
                self.assertEqual(set(rows), pks)
                self.assertTrue(all(row["display_status"] == word for row in rows.values()))
        returned = self.listed(approval="returned")
        self.assertEqual(set(returned), {self.sent_back, self.lekki_sent_back})
        self.assertTrue(all(row["approval_returned"] for row in returned.values()))
        self.assertEqual(returned[self.sent_back]["display_status"], "DRAFT")

    def test_the_tiles_count_what_their_words_list(self):
        summary = self.summary()

        self.assertEqual(summary["drafts"], len(self.listed(status="DRAFT")))
        self.assertEqual(summary["drafts"], 2)
        self.assertEqual(summary["pending_approval"], len(self.listed(status="PENDING_APPROVAL")))
        self.assertEqual(summary["pending_approval"], 1)
        self.assertEqual(summary["sent_back"], len(self.listed(approval="returned")))
        self.assertEqual(summary["sent_back"], 2)
        self.assertEqual(summary["total"], len(self.listed()))

    def test_a_branch_clerk_counts_and_lists_only_their_branch(self):
        self.assertEqual(set(self.listed(self.lekki_clerk, approval="returned")),
                         {self.lekki_sent_back})
        summary = self.summary(self.lekki_clerk)
        self.assertEqual((summary["drafts"], summary["sent_back"]), (0, 1))

    def test_a_one_branch_school_sees_its_own_and_never_another_school_batches(self):
        listed = self.listed(self.solo_clerk, self.solo_books, approval="returned")
        self.assertEqual(set(listed), {self.solo_sent_back})
        self.assertEqual(self.summary(self.solo_clerk, self.solo_books)["sent_back"], 1)

    def test_a_word_or_approval_value_the_list_does_not_have_is_refused(self):
        for params in ({"status": "SENT_BACK"}, {"approval": "yes"}):
            with self.subTest(params=params):
                response = self.get(self.whole, "", **params)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(next(iter(params)), response.data["error"]["detail"])
