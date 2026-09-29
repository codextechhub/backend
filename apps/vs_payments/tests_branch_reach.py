"""The payments console works within the caller's branches, on reads and on writes.

Corona runs Ikeja, Lekki and Yaba. Ikeja's clerk works payments for Ikeja. She
must not raise a payment request or a virtual account for a Lekki family, or pay
out to a vendor Lekki keeps to itself, any more than the finance screens let
her. Nor may she see, count or change Lekki's collections, virtual accounts,
payouts or their log. Each is answered exactly as a record that does not exist,
while Ikeja's own and the school-wide ones behave as before, and a reader who
covers the whole school sees everything.
"""
from __future__ import annotations

import itertools

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.models import Account
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .models import CollectionIntent, PayoutBatch, VirtualAccount

_clerks = itertools.count(1)


class PaymentsNameOnlyWhatTheClerkReachesTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        e = self.books
        self.ikeja_customer = self.customer(e, "CIKJP", self.ikeja)
        self.lekki_customer = self.customer(e, "CLEKP", self.lekki)
        self.shared_customer = self.customer(e, "CALLP", None)

    def clerk(self, *keys):
        n = next(_clerks)
        return TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"clerk-{n}@corona.test"), *keys,
            tenant=self.tenant, role_key=f"clerk-{n}", branch=self.ikeja,
        ))

    def post(self, client, path, body, **extra):
        return client.post(f"/v1/payments/{path}?entity={self.books.code}", body,
                           format="json", **extra)

    def vendor(self, code, branch):
        from vs_procurement.models import Vendor

        return Vendor.objects.create(
            entity=self.books, code=code, name=f"Vendor {code}", branch=branch,
            payable_account=Account.objects.get(entity=self.books, code="2100"),
        )

    def test_a_payment_request_for_another_branchs_customer(self):
        client = self.clerk("payments.collection.create")
        refused = self.post(client, "collections/", {"amount": 5_000, "customer": "CLEKP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No customer 'CLEKP' in this entity.", str(refused.data))
        self.assertFalse(CollectionIntent.objects.filter(customer=self.lekki_customer).exists())
        for code in ("CIKJP", "CALLP"):
            with self.subTest(customer=code):
                accepted = self.post(client, "collections/", {"amount": 5_000, "customer": code})
                self.assertNotIn("No customer", str(accepted.data))

    def test_a_payment_request_against_another_branchs_invoice(self):
        lekki_invoice = self.invoice(self.books, self.lekki_customer, self.lekki)
        refused = self.post(self.clerk("payments.collection.create"), "collections/", {
            "amount": 5_000, "customer": "CALLP", "invoice": lekki_invoice.pk,
        })
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(f"No invoice '{lekki_invoice.pk}' in this entity.", str(refused.data))

    def _collections_ledgers(self):
        """A bank ledger per branch, Ikeja's and Lekki's, keyed by branch tag."""
        from vs_finance.models import BankAccount

        cash_type = Account.objects.get(entity=self.books, code="1000").account_type
        ledgers = {}
        for tag, branch in (("IKJ", self.ikeja), ("LEK", self.lekki)):
            ledgers[tag] = Account.objects.create(
                entity=self.books, code=f"117{len(ledgers)}", name=f"Collections {tag}",
                account_type=cash_type, is_postable=True)
            BankAccount.objects.create(entity=self.books, name=f"Collections {tag}",
                                       branch=branch, gl_account=ledgers[tag])
        return ledgers

    def _clerk_for_ikeja_and_lekki(self):
        n = next(_clerks)
        user = self.user_for(self.tenant, f"clerk-{n}@corona.test")
        for branch in (self.ikeja, self.lekki):
            self.grant(user, "payments.collection.create", tenant=self.tenant,
                       role_key=f"clerk-{n}-{branch.pk}", branch=branch)
        return TenantAPIClient(user=user)

    def test_a_payment_request_for_an_invoice_deposits_into_the_invoices_branch(self):
        """The invoice decides the branch, as it decides the receipt's, whoever is named.

        The Okafor family is filed under Ikeja and owes a Lekki invoice. The money
        is Lekki's, so it deposits into Lekki's bank whether the request names the
        family or only the invoice, and Ikeja's bank is refused. A clerk covering
        both branches is held to it, because the rule belongs to the money.
        """
        okafor = self.customer(self.books, "COKAF", self.ikeja)
        lekki_invoice = self.invoice(self.books, okafor, self.lekki)
        ledgers = self._collections_ledgers()
        clerk = self._clerk_for_ikeja_and_lekki()

        for named in ({"customer": "COKAF"}, {}):
            with self.subTest(named=named):
                refused = self.post(clerk, "collections/", {
                    "amount": 5_000, "invoice": lekki_invoice.pk,
                    "deposit_account": ledgers["IKJ"].code, **named,
                })
                self.assertEqual(refused.status_code, 400, refused.data)
                self.assertIn("This payment request belongs to Lekki Branch. "
                              "Deposit it into a Lekki Branch account or a school-wide one.",
                              str(refused.data))
                self.assertFalse(CollectionIntent.objects.filter(invoice=lekki_invoice).exists())

                accepted = self.post(clerk, "collections/", {
                    "amount": 5_000, "invoice": lekki_invoice.pk,
                    "deposit_account": ledgers["LEK"].code, **named,
                })
                self.assertNotIn("belongs to", str(accepted.data))

    def test_a_payment_request_naming_no_invoice_deposits_into_the_customers_branch(self):
        """A top-up has no invoice, so the family's own branch takes the money."""
        self.customer(self.books, "COKAF", self.ikeja)
        ledgers = self._collections_ledgers()
        clerk = self._clerk_for_ikeja_and_lekki()

        refused = self.post(clerk, "collections/", {
            "amount": 5_000, "customer": "COKAF", "deposit_account": ledgers["LEK"].code,
        })
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("This payment request belongs to Ikeja Branch.", str(refused.data))

        accepted = self.post(clerk, "collections/", {
            "amount": 5_000, "customer": "COKAF", "deposit_account": ledgers["IKJ"].code,
        })
        self.assertNotIn("belongs to", str(accepted.data))

    def test_a_virtual_account_for_another_branchs_customer(self):
        refused = self.post(self.clerk("payments.virtual_account.create"),
                            "virtual-accounts/", {"customer": "CLEKP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No customer 'CLEKP' in this entity.", str(refused.data))
        self.assertFalse(VirtualAccount.objects.filter(customer=self.lekki_customer).exists())

    def test_a_payout_to_a_vendor_another_branch_keeps(self):
        lekki_vendor = self.vendor("VLEK", self.lekki)
        client = self.clerk("payments.payout.create")
        refused = self.post(client, "payouts/", {"amount": 5_000, "vendor": lekki_vendor.pk},
                            HTTP_IDEMPOTENCY_KEY="reach-single-1")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        for n, vendor in enumerate((self.vendor("VIKJ", self.ikeja), self.vendor("VALL", None))):
            with self.subTest(vendor=vendor.code):
                accepted = self.post(client, "payouts/", {"amount": 5_000, "vendor": vendor.pk},
                                     HTTP_IDEMPOTENCY_KEY=f"reach-single-ok-{n}")
                self.assertNotIn("No such vendor", str(accepted.data))

    def test_a_payout_batch_line_to_a_vendor_another_branch_keeps(self):
        lekki_vendor = self.vendor("VLEKB", self.lekki)
        before = PayoutBatch.objects.count()
        refused = self.post(
            self.clerk("payments.payout.create"), "payout-batches/",
            {"items": [{"amount": 5_000, "vendor": lekki_vendor.pk}]},
            HTTP_IDEMPOTENCY_KEY="reach-batch-1",
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        self.assertEqual(PayoutBatch.objects.count(), before)


class PaymentsShowOnlyWhatTheClerkReachesTests(_FinanceBranchFixture):
    """Every payments list, summary, detail and status change stays within reach.

    Corona's books hold a gateway record for each of Ikeja, Lekki and the whole
    school, on every screen. Ikeja's clerk sees Ikeja's and the school-wide ones
    and counts only those; a Lekki record is a 404 to her, on reading it and on
    changing it, and nothing changes. The bursar, who covers the whole school,
    sees all of them exactly as before.
    """

    KEYS = (
        "payments.collection.view", "payments.virtual_account.view",
        "payments.virtual_account.update", "payments.payout.view",
        "payments.payout.create", "payments.payout_batch.submit", "payments.report.view",
        "payments.webhook.view", "payments.webhook.replay",
    )

    def setUp(self):
        from django.utils import timezone

        from vs_finance.models import BankAccount, BankStatementLine

        from .constants import CollectionStatus, PaymentAuditAction, PayoutStatus
        from .models import PaymentEvent, PayoutInstruction, WebhookEvent

        super().setUp()
        e = self.books
        now = timezone.now()
        ikeja_c = self.customer(e, "CIKJ", self.ikeja)
        lekki_c = self.customer(e, "CLEK", self.lekki)
        shared_c = self.customer(e, "CALL", None)
        lekki_invoice = self.invoice(e, shared_c, self.lekki)

        def collection(ref, customer, invoice=None):
            return CollectionIntent.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, amount=1_000,
                customer=customer, invoice=invoice, status=CollectionStatus.SUCCEEDED,
                confirmed_at=now)

        self.collections = {
            "COL-IKJ": collection("COL-IKJ", ikeja_c),
            "COL-LEK": collection("COL-LEK", lekki_c),
            "COL-ALL": collection("COL-ALL", shared_c),
            "COL-LEKINV": collection("COL-LEKINV", shared_c, lekki_invoice),
            "COL-NONE": collection("COL-NONE", None),
        }
        self.vas = {
            code: VirtualAccount.objects.create(
                entity=e, provider="PAYSTACK", customer=customer,
                account_number=f"90{n}", provider_reference=f"VA-{code}")
            for n, (code, customer) in enumerate(
                (("IKJ", ikeja_c), ("LEK", lekki_c), ("ALL", shared_c)))
        }
        vendors = {
            "IKJ": self.vendor("VIKJ", self.ikeja), "LEK": self.vendor("VLEK", self.lekki),
            "ALL": self.vendor("VALL", None),
        }
        self.batches = {
            code: PayoutBatch.objects.create(entity=e, provider="PAYSTACK", reference=code)
            for code in ("BAT-IKJ", "BAT-MIXED", "BAT-ALL")
        }

        def payout(ref, vendor, batch):
            return PayoutInstruction.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, amount=2_000,
                beneficiary_name=vendor.name, beneficiary_account_number="0123456789",
                vendor_source_type="vs_procurement.Vendor", vendor_source_id=str(vendor.pk),
                batch=self.batches[batch], status=PayoutStatus.PAID, confirmed_at=now)

        payout("PAY-IKJ", vendors["IKJ"], "BAT-IKJ")
        payout("PAY-LEK", vendors["LEK"], "BAT-MIXED")
        payout("PAY-ALL", vendors["ALL"], "BAT-MIXED")
        payout("PAY-ALL2", vendors["ALL"], "BAT-ALL")

        for ref in (*self.collections, "PAY-IKJ", "PAY-LEK", "PAY-ALL", *self.batches):
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.COLLECTION_INITIATED, reference=ref)
        for code, va in self.vas.items():
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.VIRTUAL_ACCOUNT_CREATED,
                reference=f"REQ-{code}", metadata={"virtual_account_id": va.pk})
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.VIRTUAL_ACCOUNT_STATUS_CHANGED,
                reference=va.provider_reference)
        for ref in ("COL-IKJ", "COL-LEK"):
            WebhookEvent.objects.create(
                provider="PAYSTACK", dedupe_key=f"wh-{ref}", provider_reference=f"WH-{ref}",
                collection=self.collections[ref], status="FAILED")

        cash_type = Account.objects.get(entity=e, code="1000").account_type
        for n, (tag, branch) in enumerate((("IKJ", self.ikeja), ("LEK", self.lekki),
                                           ("ALL", None))):
            gl = Account.objects.create(entity=e, code=f"116{n}", name=f"Cash {tag}",
                                        account_type=cash_type, is_postable=True)
            bank = BankAccount.objects.create(entity=e, name=f"Bank {tag}", branch=branch,
                                              gl_account=gl)
            BankStatementLine.objects.create(
                bank_account=bank, txn_date=tenant_today(e.tenant), amount=777,
                description=f"LINE-{tag}", reference=f"LINE-{tag}")

    vendor = PaymentsNameOnlyWhatTheClerkReachesTests.vendor

    def reader(self, *, branch):
        n = next(_clerks)
        return TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"reader-{n}@corona.test"), *self.KEYS,
            tenant=self.tenant, role_key=f"reader-{n}", branch=branch,
        ))

    def get(self, client, path):
        joiner = "&" if "?" in path else "?"
        response = client.get(f"/v1/payments/{path}{joiner}entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def refs(self, client, path, key="reference"):
        return {row[key] for row in self.get(client, path)["data"]}

    # -- lists ---------------------------------------------------------------- #

    def test_each_list_holds_only_rows_in_reach(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        cases = (
            ("collections/", "reference", {"COL-IKJ", "COL-ALL", "COL-NONE"},
             set(self.collections)),
            ("virtual-accounts/", "provider_reference", {"VA-IKJ", "VA-ALL"},
             {"VA-IKJ", "VA-LEK", "VA-ALL"}),
            ("payouts/", "reference", {"PAY-IKJ", "PAY-ALL", "PAY-ALL2"},
             {"PAY-IKJ", "PAY-LEK", "PAY-ALL", "PAY-ALL2"}),
            ("payout-batches/", "reference", {"BAT-IKJ", "BAT-ALL"}, set(self.batches)),
            ("movements/", "reference",
             {"COL-IKJ", "COL-ALL", "COL-NONE", "PAY-IKJ", "PAY-ALL", "PAY-ALL2"},
             set(self.collections) | {"PAY-IKJ", "PAY-LEK", "PAY-ALL", "PAY-ALL2"}),
            ("transactions/", "reference",
             {"COL-IKJ", "COL-ALL", "COL-NONE", "PAY-IKJ", "PAY-ALL", "BAT-IKJ", "BAT-ALL",
              "REQ-IKJ", "REQ-ALL", "VA-IKJ", "VA-ALL"},
             set(self.collections) | {"PAY-IKJ", "PAY-LEK", "PAY-ALL", *self.batches,
                                      "REQ-IKJ", "REQ-LEK", "REQ-ALL",
                                      "VA-IKJ", "VA-LEK", "VA-ALL"}),
            ("webhooks/?status=ALL", "provider_reference", {"WH-COL-IKJ"},
             {"WH-COL-IKJ", "WH-COL-LEK"}),
        )
        for path, key, clerk_sees, bursar_sees in cases:
            with self.subTest(path=path):
                self.assertEqual(self.refs(clerk, path, key), clerk_sees)
                self.assertEqual(self.refs(bursar, path, key), bursar_sees)

    def test_settlement_reconciliation_holds_only_rows_in_reach(self):
        def seen(client):
            data = self.get(client, "reports/settlement-reconciliation/")["data"]
            return ({row["reference"] for row in data["rows"]},
                    {line["reference"] for line in data["unmatched_bank_lines"]})

        rows, lines = seen(self.reader(branch=self.ikeja))
        self.assertEqual(rows, {"COL-IKJ", "COL-ALL", "COL-NONE", "PAY-IKJ", "PAY-ALL", "PAY-ALL2"})
        self.assertEqual(lines, {"LINE-IKJ", "LINE-ALL"})
        rows, lines = seen(self.reader(branch=None))
        self.assertIn("COL-LEK", rows)
        self.assertIn("PAY-LEK", rows)
        self.assertEqual(lines, {"LINE-IKJ", "LINE-LEK", "LINE-ALL"})

    # -- summaries ------------------------------------------------------------ #

    def test_each_summary_counts_only_rows_in_reach(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        cases = (
            ("collections/summary/", lambda d: (d["total"], d["collected"]["kobo"]),
             (3, 3_000), (5, 5_000)),
            ("virtual-accounts/", lambda d: d["kpis"]["total"], 2, 3),
            ("payouts/summary/", lambda d: (d["total"], d["settled7d"]["kobo"]),
             (3, 6_000), (4, 8_000)),
            ("payout-batches/summary/", lambda d: d["total"], 2, 3),
            ("movements/summary/", lambda d: (d["in7d"]["kobo"], d["out7d"]["kobo"]),
             (3_000, 6_000), (5_000, 8_000)),
            ("webhooks/summary/", lambda d: d["failed"], 1, 2),
        )
        for path, pick, clerk_counts, bursar_counts in cases:
            with self.subTest(path=path):
                clerk_data, bursar_data = self.get(clerk, path), self.get(bursar, path)
                if "kpis" not in clerk_data:
                    clerk_data, bursar_data = clerk_data["data"], bursar_data["data"]
                self.assertEqual(pick(clerk_data), clerk_counts)
                self.assertEqual(pick(bursar_data), bursar_counts)

    # -- detail and status changes -------------------------------------------- #

    def test_another_branchs_rows_are_unknown_on_detail(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        lekki = {
            "collections": self.collections["COL-LEK"].pk,
            "virtual-accounts": self.vas["LEK"].pk,
            "payout-batches": self.batches["BAT-MIXED"].pk,
        }
        for path, pk in lekki.items():
            with self.subTest(path=path):
                url = f"/v1/payments/{path}/{pk}/?entity={self.books.code}"
                self.assertEqual(clerk.get(url).status_code, 404)
                self.assertEqual(bursar.get(url).status_code, 200)
        own = f"/v1/payments/collections/{self.collections['COL-IKJ'].pk}/?entity={self.books.code}"
        self.assertEqual(clerk.get(own).status_code, 200)

    def test_another_branchs_virtual_account_cannot_be_suspended(self):
        clerk = self.reader(branch=self.ikeja)
        lekki, ikeja = self.vas["LEK"], self.vas["IKJ"]
        refused = clerk.patch(
            f"/v1/payments/virtual-accounts/{lekki.pk}/?entity={self.books.code}",
            {"status": "INACTIVE"}, format="json")
        self.assertEqual(refused.status_code, 404, refused.data)
        lekki.refresh_from_db()
        self.assertEqual(lekki.status, "ACTIVE")
        accepted = clerk.patch(
            f"/v1/payments/virtual-accounts/{ikeja.pk}/?entity={self.books.code}",
            {"status": "INACTIVE"}, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        ikeja.refresh_from_db()
        self.assertEqual(ikeja.status, "INACTIVE")
        from .models import PaymentEvent

        logged = PaymentEvent.objects.filter(action="VIRTUAL_ACCOUNT_STATUS_CHANGED").latest("id")
        self.assertEqual(logged.metadata.get("virtual_account_id"), ikeja.pk)

    def test_the_exports_hold_only_rows_in_reach(self):
        from vs_exports.catalogue import ScopeContext

        from .export_datasets import _collections, _payouts

        clerk = self.grant(self.user_for(self.tenant, "export-clerk@corona.test"),
                           "payments.collection.view", tenant=self.tenant,
                           role_key="export-clerk", branch=self.ikeja)
        scope = ScopeContext(tenant=self.tenant, entity=self.books, user=clerk)
        self.assertEqual(set(_collections(scope).values_list("reference", flat=True)),
                         {"COL-IKJ", "COL-ALL", "COL-NONE"})
        self.assertEqual(set(_payouts(scope).values_list("reference", flat=True)),
                         {"PAY-IKJ", "PAY-ALL", "PAY-ALL2"})
        whole = ScopeContext(tenant=self.tenant, entity=self.books, user=None)
        self.assertEqual(_collections(whole).count(), 5)

    def test_another_branchs_batch_and_webhook_cannot_be_acted_on(self):
        from .models import WebhookEvent

        clerk = self.reader(branch=self.ikeja)
        batch = self.batches["BAT-MIXED"]
        for path in (f"payout-batches/{batch.pk}/submit-for-approval/",
                     f"payout-batches/{batch.pk}/"):
            with self.subTest(path=path):
                response = clerk.post(f"/v1/payments/{path}?entity={self.books.code}", {},
                                      format="json")
                self.assertEqual(response.status_code, 404, response.data)
        batch.refresh_from_db()
        self.assertEqual((batch.status, batch.submitted_at), ("DRAFT", None))
        event = WebhookEvent.objects.get(collection=self.collections["COL-LEK"])
        response = clerk.post(
            f"/v1/payments/webhooks/{event.pk}/replay/?entity={self.books.code}", {},
            format="json")
        self.assertEqual(response.status_code, 404, response.data)
        event.refresh_from_db()
        self.assertEqual(event.status, "FAILED")



class PayoutBatchApprovalsStayWithinReachTests(_FinanceBranchFixture):
    """The payout approval inbox holds only the batches the approver can reach.

    Mrs Bello is Ikeja's bursar and is named on Corona's payout approver group, so
    the approval route puts her on every batch; a batch has no branch of its own.
    One batch pays an Ikeja vendor, another pays a Lekki vendor and a school-wide
    one. The payout screens already keep the second from her. The approval inbox,
    the instance behind it and its approve and reject answer the same way, while
    the head bursar, who covers the whole school, sees and decides both.
    """

    def setUp(self):
        from vs_workflow.constants import GroupMemberKind
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        from .approvals import ensure_tenant_approval_templates
        from .constants import WF_DEFAULT_APPROVE_GROUP, PayoutStatus
        from .models import PayoutInstruction
        from .services import submit_payout_batch_for_approval

        super().setUp()
        e = self.books
        vendor = PaymentsNameOnlyWhatTheClerkReachesTests.vendor
        vendors = {"IKJ": vendor(self, "AVIKJ", self.ikeja),
                   "LEK": vendor(self, "AVLEK", self.lekki),
                   "ALL": vendor(self, "AVALL", None)}

        def batch(ref, *codes):
            row = PayoutBatch.objects.create(entity=e, provider="PAYSTACK", reference=ref)
            for n, code in enumerate(codes):
                PayoutInstruction.objects.create(
                    entity=e, provider="PAYSTACK", reference=f"{ref}-{n}", amount=2_000,
                    beneficiary_name=vendors[code].name, beneficiary_account_number="0123456789",
                    vendor_source_type="vs_procurement.Vendor",
                    vendor_source_id=str(vendors[code].pk), batch=row,
                    status=PayoutStatus.PENDING)
            return row

        self.ikeja_batch = batch("ABAT-IKJ", "IKJ")
        self.mixed_batch = batch("ABAT-MIXED", "LEK", "ALL")

        ensure_tenant_approval_templates(self.tenant)
        self.bello = self.grant(self.user_for(self.tenant, "bello@corona.test"),
                                "payments.payout.view", tenant=self.tenant,
                                role_key="ikeja-bursar", branch=self.ikeja)
        self.head = self.grant(self.user_for(self.tenant, "head@corona.test"),
                               "payments.payout.view", tenant=self.tenant,
                               role_key="head-bursar")
        group = WorkflowApproverGroup.all_objects.get(
            tenant=self.tenant, code=WF_DEFAULT_APPROVE_GROUP)
        for user in (self.bello, self.head):
            WorkflowApproverGroupMember.objects.create(
                group=group, kind=GroupMemberKind.USER, user=user)

        clerk = self.user_for(self.tenant, "payout-clerk@corona.test")
        self.instances = {
            b.reference: submit_payout_batch_for_approval(b, requested_by=clerk)
            for b in (self.ikeja_batch, self.mixed_batch)
        }

    def inbox(self, user):
        response = TenantAPIClient(user=user).get("/v1/workflow/dashboard/pending/")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["document_object_id"] for row in response.data["results"]}

    def act(self, user, reference, action):
        return TenantAPIClient(user=user).post(
            f"/v1/workflow/instances/{self.instances[reference].pk}/actions/",
            {"action": action, "comment": "Checked."}, format="json")

    def test_the_inbox_holds_only_the_batches_in_reach(self):
        self.assertEqual(self.inbox(self.bello), {str(self.ikeja_batch.pk)})
        self.assertEqual(self.inbox(self.head),
                         {str(self.ikeja_batch.pk), str(self.mixed_batch.pk)})

    def test_the_finance_dashboard_counts_only_the_batches_in_reach(self):
        from vs_finance.dashboard_blocks import approvals_waiting_on

        self.assertEqual(approvals_waiting_on(self.books, self.bello)["total"], 1)
        self.assertEqual(approvals_waiting_on(self.books, self.head)["total"], 2)

    def test_a_batch_out_of_reach_cannot_be_read_approved_or_rejected(self):
        instance = self.instances["ABAT-MIXED"]
        client = TenantAPIClient(user=self.bello)
        self.assertEqual(
            client.get(f"/v1/workflow/instances/{instance.pk}/").status_code, 404)
        for action in ("APPROVED", "REJECTED"):
            with self.subTest(action=action):
                self.assertEqual(self.act(self.bello, "ABAT-MIXED", action).status_code, 404)
        instance.refresh_from_db()
        self.assertEqual(instance.status, "IN_PROGRESS")

        self.assertEqual(self.act(self.bello, "ABAT-IKJ", "REJECTED").status_code, 200)
        self.assertEqual(self.act(self.head, "ABAT-MIXED", "REJECTED").status_code, 200)

class PaymentsViewsStartFromTheReachTests(SimpleTestCase):
    """No payments view reaches a gateway table except through :class:`PaymentsReach`.

    A view that filtered ``CollectionIntent.objects`` itself would show Ikeja's
    clerk the whole school again, and nothing else would notice.
    """

    def test_views_name_no_gateway_manager(self):
        import inspect
        import re

        from . import views

        source = inspect.getsource(views)
        unattributed = inspect.getsource(views._unattributed_webhooks)
        found = re.findall(
            r"\b(CollectionIntent|VirtualAccount|PayoutInstruction|PayoutBatch|PaymentEvent"
            r"|WebhookEvent)\.(?:objects|all_objects)\b",
            source.replace(unattributed, ""))
        self.assertEqual(found, [], "Start from vs_payments.reach.PaymentsReach instead.")
