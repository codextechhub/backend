"""A finance list's filter, its row word and its export select the same documents.

Mrs Okafor filters the expense claims to Approved and sees the claims posted and
not yet reimbursed; Part-paid has its own word, and a claim Mr Adeyemi sent back
wears Sent back, so neither shows under Approved, and the file she exports holds
the same claims. Every word is read in any case, and a word the list does not
have is refused (400) rather than answered with every row.
"""
from __future__ import annotations

import datetime

from django.db.models import Q

from core.test_utils import TenantAPIClient

from .models import Customer, ExpenseClaim, Invoice, JournalEntry, Payment
from .tests_sent_back import VIEW_KEYS, _SentBackFixture

D = datetime.date
EVER = {"start": "2000-01-01", "end": "2100-12-31"}
READ_KEYS = VIEW_KEYS + (
    "finance.invoice.view", "finance.customer.view", "finance.payment.view",
    "exports.catalogue.view",
)


class _WordsFixture(_SentBackFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.hq = cls.bursar(None, keys=READ_KEYS)

    def listed(self, path, user=None, **params):
        response = self.as_(user or self.hq).get(self.url(
            path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")))
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def refused(self, path, **params):
        response = self.as_(self.hq).get(self.url(
            path + "?" + "&".join(f"{k}={v}" for k, v in params.items())))
        self.assertEqual(response.status_code, 400, response.data)
        return response.data["error"]["detail"]

    def exported(self, screen, **params):
        """The pks a quick export of ``screen`` with ``params`` would write."""
        from vs_exports.catalogue import ScopeContext, get_screen, resolve_screen
        from vs_exports.engine import build_queryset

        binding = get_screen(screen)
        filters = resolve_screen(binding, params, today=D(2026, 1, 31))["filters"]
        dataset = binding.dataset
        primary = next(f.id for f in dataset.filters if f.is_primary_date)
        filters = [f for f in filters if f["id"] != primary] + [{"id": primary, **EVER}]
        scope = ScopeContext(tenant=self.tenant, entity=self.books, user=self.hq)
        return set(build_queryset(dataset, scope, filters).values_list("pk", flat=True))


class DraftLeavesSentBackOutTests(_WordsFixture):

    def test_draft_lists_drafts_and_not_the_journals_sent_back(self):
        returned = self.direct_entry(self.okafor)
        self.returned(JournalEntry, returned)
        with_approver = self.direct_entry(self.okafor)
        never_sent = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=D(2026, 1, 15),
            period=JournalEntry.objects.get(pk=returned).period, narration="Draft").pk

        self.assertEqual(self.listed("journals/", status="DRAFT") & {returned, never_sent},
                         {never_sent})
        self.assertIn(never_sent, self.listed("journals/", status="draft"))
        self.assertEqual(self.listed("journals/", status="PENDING_APPROVAL") & {with_approver, returned},
                         {with_approver})
        self.assertEqual(self.listed("journals/", approval="returned"), {returned})

    def test_the_summary_counts_each_tab_as_the_tab_lists_it(self):
        returned = self.direct_entry(self.okafor)
        self.returned(JournalEntry, returned)
        self.direct_entry(self.okafor)
        JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=D(2026, 1, 15),
            period=JournalEntry.objects.get(pk=returned).period, narration="Draft")

        summary = self.as_(self.hq).get(self.url("journals/summary/")).data["data"]

        for status in ("DRAFT", "PENDING_APPROVAL"):
            with self.subTest(status=status):
                self.assertEqual(summary["by_status"].get(status, 0),
                                 len(self.listed("journals/", status=status)))
        self.assertEqual(summary["sent_back"], len(self.listed("journals/", approval="returned")))
        self.assertEqual(summary["sent_back"], 1)
        self.assertEqual(summary["total"], len(self.listed("journals/", page_size=100)))

    def test_every_finance_document_list_refuses_a_status_it_does_not_have(self):
        for path in ("journals/", "credit-notes/", "refunds/", "write-offs/", "credit-transfers/",
                     "concessions/", "bank-transactions/", "bank-transfers/",
                     "inter-branch-transfers/", "expense-claims/", "petty-cash-returns/"):
            with self.subTest(path=path):
                self.assertIn("status", self.refused(path, status="NOPE"))
                self.listed(path, user=self.reader, status="draft")


class ExpenseClaimWordsTests(_WordsFixture):

    def made(self, *, submit):
        made = self.client_.post(self.url("expense-claims/"), {
            "claimant_name": "Mrs Okafor", "claim_date": "2026-01-10", "title": "Bank visit",
            "lines": [{"description": "Taxi", "expense_account": "5300",
                       "quantity": 1, "unit_price": 5_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        if submit:
            self.client_.post(self.url(f"expense-claims/{pk}/submit/"), {}, format="json")
        return pk

    def claims(self):
        claims = {
            "DRAFT": self.made(submit=False), "PENDING": self.made(submit=True),
            "APPROVED": self.made(submit=False), "PART_PAID": self.made(submit=False),
            "PAID": self.made(submit=False), "REJECTED": self.made(submit=False),
        }
        for word, status, paid in (("APPROVED", "POSTED", "UNPAID"),
                                   ("PART_PAID", "POSTED", "PARTIAL"),
                                   ("PAID", "POSTED", "PAID"), ("REJECTED", "CANCELLED", "UNPAID")):
            ExpenseClaim.objects.filter(pk=claims[word]).update(status=status, payment_status=paid)
        sent_back = self.made(submit=True)
        self.returned(ExpenseClaim, sent_back)
        return claims, sent_back

    def test_each_word_lists_the_claims_wearing_it_and_the_export_writes_the_same(self):
        claims, sent_back = self.claims()

        for word, pk in claims.items():
            with self.subTest(word=word):
                listed = self.listed("expense-claims/", display_status=word.lower())
                self.assertEqual(listed, {pk})
                self.assertEqual(self.exported("finance.expense_claims", display_status=word), {pk})
        self.assertEqual(self.listed("expense-claims/", approval="returned"), {sent_back})
        self.assertEqual(self.exported("finance.expense_claims", approval="returned"), {sent_back})
        summary = self.as_(self.hq).get(self.url("expense-claims/summary/")).data["data"]
        self.assertEqual(summary["sent_back"], 1)

    def test_a_word_the_list_does_not_have_is_refused_by_the_list_and_the_export(self):
        from rest_framework.exceptions import ValidationError

        self.assertIn("display_status", self.refused("expense-claims/", display_status="SENT_BACK"))
        with self.assertRaises(ValidationError):
            self.exported("finance.expense_claims", display_status="SENT_BACK")


class ReceivableWordsTests(_WordsFixture):

    def invoice(self, *, status="POSTED", paid="UNPAID", due=D(2099, 1, 1)):
        invoice = self.posted_invoice(self.tunde, self.ikeja, amount=10_000)
        Invoice.objects.filter(pk=invoice.pk).update(status=status, payment_status=paid, due_date=due)
        return invoice.pk

    def test_each_invoice_tab_lists_its_invoices_and_the_export_writes_the_same(self):
        tabs = {
            "draft": self.invoice(status="DRAFT"),
            "paid": self.invoice(paid="PAID"),
            "issued": self.invoice(),
            "partial": self.invoice(paid="PARTIAL"),
            "overdue": self.invoice(paid="PARTIAL", due=D(2000, 1, 1)),
        }
        mine = set(tabs.values())

        for tab, pk in tabs.items():
            with self.subTest(tab=tab):
                self.assertEqual(self.listed("invoices/", bucket=tab.upper()) & mine, {pk})
                self.assertEqual(self.exported("finance.invoices", bucket=tab) & mine, {pk})
        self.assertEqual(self.listed("invoices/", bucket="open") & mine,
                         {tabs["issued"], tabs["partial"], tabs["overdue"]})
        self.assertEqual(self.exported("finance.invoices", bucket="open") & mine,
                         {tabs["issued"], tabs["partial"], tabs["overdue"]})
        self.assertIn("bucket", self.refused("invoices/", bucket="nonsense"))

    def test_each_customer_status_lists_the_customers_wearing_it_and_the_export_matches(self):
        Invoice.objects.filter(pk=self.bill.pk).update(due_date=D(2000, 1, 1))
        quiet = self.customer(self.books, "QUIET", self.ikeja).pk
        gone = self.customer(self.books, "GONE", self.ikeja)
        Customer.objects.filter(pk=gone.pk).update(is_active=False)

        active = self.listed("customers/", status="active")
        self.assertIn(quiet, active)
        self.assertNotIn(self.tunde.pk, active)
        self.assertIn(self.tunde.pk, self.listed("customers/", status="OVERDUE"))
        for word in ("ACTIVE", "INACTIVE", "CREDIT", "OVERDUE"):
            with self.subTest(word=word):
                self.assertEqual(self.exported("finance.customers", status=word),
                                 self.listed("customers/", status=word))
        self.assertIn("status", self.refused("customers/", status="nonsense"))

    def test_each_receipt_allocation_word_lists_its_receipts_and_the_export_matches(self):
        def receipt(allocated=0, refunded=0):
            payment = Payment.objects.create(
                entity=self.books, branch=self.ikeja, customer=self.tunde,
                payment_date=D(2026, 1, 12), amount=10_000,
                deposit_account=self.ikeja_bank.gl_account,
            )
            Payment.objects.filter(pk=payment.pk).update(
                status="POSTED", allocated_amount=allocated, refunded_amount=refunded)
            return payment.pk

        words = {"ALLOCATED": receipt(10_000), "PARTIAL": receipt(4_000),
                 "UNALLOCATED": receipt(), "REFUNDED": receipt(4_000, 6_000)}
        mine = set(words.values())
        for word, pk in words.items():
            with self.subTest(word=word):
                self.assertEqual(self.listed("payments/", status=word.lower()) & mine, {pk})
                self.assertEqual(self.exported("finance.receipts", status=word) & mine, {pk})
        self.assertIn("status", self.refused("payments/", status="POSTED"))


class InterBranchWordsTests(_WordsFixture):
    """The transfer and held-receipt filters read the stage each row's pill shows."""

    def test_transfer_stage_words_follow_the_stage(self):
        from .constants import DocumentStatus
        from .models import InterBranchTransfer
        from .inter_branch import transfer_stage_rules

        rules = transfer_stage_rules()
        cases = [
            (DocumentStatus.DRAFT, None, None, "REQUESTED"),
            (DocumentStatus.APPROVED, None, None, "REQUESTED"),
            (DocumentStatus.PENDING_APPROVAL, None, None, "PENDING_APPROVAL"),
            (DocumentStatus.POSTED, None, None, "SENT"),
            (DocumentStatus.POSTED, D(2026, 1, 2), None, "RECEIVED"),
            (DocumentStatus.CANCELLED, None, D(2026, 1, 2), "DECLINED"),
            (DocumentStatus.CANCELLED, None, None, "NOT_SENT"),
            (DocumentStatus.REVERSED, None, None, "VOIDED"),
        ]
        stages = ("REQUESTED", "PENDING_APPROVAL", "SENT", "RECEIVED", "DECLINED",
                  "NOT_SENT", "VOIDED")
        for status, received, declined, stage in cases:
            with self.subTest(status=status, stage=stage):
                row = InterBranchTransfer(status=status, received_at=received, declined_at=declined)
                self.assertEqual(row.stage, stage)
                matching = [word for word in stages if _matches(rules[word], row)]
                self.assertEqual(matching, [stage])


def _matches(condition, row) -> bool:
    """Whether an unsaved ``row`` meets a ``Q`` of plain field lookups."""
    if isinstance(condition, Q):
        results = [_matches(child, row) for child in condition.children]
        met = all(results) if condition.connector == Q.AND else any(results)
        return not met if condition.negated else met
    field, value = condition
    name, _, lookup = field.partition("__")
    actual = getattr(row, name)
    if lookup == "in":
        return actual in value
    if lookup == "isnull":
        return (actual is None) == value
    return actual == value


class HeldReceiptWordsTests(_WordsFixture):

    def test_held_receipt_words_follow_the_pill_and_refuse_others(self):
        from .constants import DocumentStatus
        from .models import HeldForBranchReceipt, InterBranchTransfer

        def held(code):
            return HeldForBranchReceipt.objects.create(
                entity=self.books, branch=self.ikeja, for_branch=self.lekki,
                bank_account=self.ikeja_bank, customer=self.tunde, amount=10_000,
                receipt_date=D(2026, 1, 15), status=DocumentStatus.POSTED,
                document_number=code,
            )

        def forward(receipt, status):
            InterBranchTransfer.objects.create(
                entity=self.books, branch=self.ikeja, to_branch=self.lekki, held_receipt=receipt,
                amount=10_000, transfer_date=D(2026, 1, 16), status=status,
                from_bank_account=self.ikeja_bank, to_bank_account=self.lekki_bank,
            )

        plain = held("HF-PLAIN")
        let_go = held("HF-LETGO")
        forward(let_go, DocumentStatus.CANCELLED)
        on_its_way = held("HF-WAY")
        forward(on_its_way, DocumentStatus.PENDING_APPROVAL)
        arrived = held("HF-DONE")
        forward(arrived, DocumentStatus.POSTED)
        voided = held("HF-VOID")
        HeldForBranchReceipt.objects.filter(pk=voided.pk).update(status=DocumentStatus.REVERSED)
        mine = {plain.pk, let_go.pk, on_its_way.pk, arrived.pk, voided.pk}

        expected = {"held": {plain.pk, let_go.pk}, "Forwarding": {on_its_way.pk},
                    "FORWARDED": {arrived.pk}, "voided": {voided.pk}}
        for word, pks in expected.items():
            with self.subTest(word=word):
                self.assertEqual(self.listed("held-receipts/", status=word) & mine, pks)
        self.assertIn("status", self.refused("held-receipts/", status="SENT"))
