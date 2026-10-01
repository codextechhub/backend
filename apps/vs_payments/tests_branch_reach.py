"""The payments console works within the caller's branches, on reads and on writes.

Corona runs Ikeja, Lekki and Yaba. Ikeja's clerk works payments for Ikeja. They
must not raise a payment request or a virtual account for a Lekki family, or pay
out to a vendor Lekki keeps to itself or from a bank that is not Ikeja's, any
more than the finance screens let them. Nor may they see, count or change Lekki's collections, virtual accounts,
payouts or their log. Every gateway record names its branch in its own column
and is read exclusively: a payout from Lekki's bank is Lekki's whoever it pays,
and a record not yet given a branch is not theirs either, because nothing says
whose money it is. Each is answered exactly as a record that does not exist,
while Ikeja's own behave as before, and a reader who covers the whole school
sees everything.
"""
from __future__ import annotations

import itertools

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.models import Account
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .models import CollectionIntent, PayoutBatch, VirtualAccount
from .providers import registry
from .providers.fake import FakeProvider

_clerks = itertools.count(1)


def _fake_the_provider(test):
    """Open checkouts against the in-memory provider, never the real Paystack API."""
    registry.register("PAYSTACK", FakeProvider(secret="test-secret"))
    test.addCleanup(registry.unregister, "PAYSTACK")


class PaymentsNameOnlyWhatTheClerkReachesTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        _fake_the_provider(self)
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
        shared = self.post(client, "collections/", {"amount": 5_000, "customer": "CALLP"})
        self.assertEqual(shared.status_code, 400, shared.data)
        self.assertIn("No customer 'CALLP' in this entity.", str(shared.data))
        accepted = self.post(client, "collections/", {"amount": 5_000, "customer": "CIKJP"})
        self.assertNotIn("No customer", str(accepted.data))
        self.assertIn(accepted.status_code, (200, 201), accepted.data)

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
                              "Deposit it into a Lekki Branch account.",
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
        self.assertIn(accepted.status_code, (200, 201), accepted.data)

    def _tola_and_the_okafors(self):
        """Tola keeps Lekki's books; the Okafors are filed under Ikeja and owe Lekki."""
        from vs_finance.models import InvoiceLine
        from vs_finance.receivables import post_invoice

        okafor = self.customer(self.books, "COKAF", self.ikeja)
        invoice = self.invoice(self.books, okafor, self.lekki)
        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=self.books, code="4100"))
        post_invoice(invoice)
        n = next(_clerks)
        tola = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"tola-{n}@corona.test"), "payments.collection.create",
            tenant=self.tenant, role_key=f"tola-{n}", branch=self.lekki))
        return tola, invoice

    def test_the_family_owing_the_invoice_may_be_named_from_the_invoices_branch(self):
        """Tola names the Okafors beside their Lekki invoice and starts the checkout."""
        tola, invoice = self._tola_and_the_okafors()

        for customer in ("COKAF", str(invoice.customer_id)):
            with self.subTest(customer=customer):
                accepted = self.post(tola, "collections/", {
                    "amount": 5_000, "customer": customer, "invoice": invoice.pk})
                self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(
            set(CollectionIntent.objects.filter(invoice=invoice)
                .values_list("customer__code", flat=True)), {"COKAF"})

    def test_another_branchs_family_is_unknown_unless_it_owes_the_named_invoice(self):
        """Without the invoice, or beside one they do not owe, the Okafors do not exist to Tola."""
        tola, invoice = self._tola_and_the_okafors()
        self.customer(self.books, "CADE", self.ikeja)

        for body in ({"customer": "COKAF"},
                     {"customer": "CADE", "invoice": invoice.pk},
                     {"customer": "CIKJP", "invoice": invoice.pk}):
            with self.subTest(body=body):
                refused = self.post(tola, "collections/", {"amount": 5_000, **body})
                self.assertEqual(refused.status_code, 400, refused.data)
                self.assertIn(f"No customer '{body['customer']}' in this entity.",
                              str(refused.data))
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_virtual_account_for_another_branchs_customer(self):
        refused = self.post(self.clerk("payments.virtual_account.create"),
                            "virtual-accounts/", {"customer": "CLEKP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No customer 'CLEKP' in this entity.", str(refused.data))
        self.assertFalse(VirtualAccount.objects.filter(customer=self.lekki_customer).exists())

    def test_a_payout_to_a_vendor_another_branch_keeps(self):
        """A vendor is shared master data: Ikeja's own and every branch's are payable, Lekki's is not."""
        ledgers = self._collections_ledgers()
        lekki_vendor = self.vendor("VLEK", self.lekki)
        client = self.clerk("payments.payout.create")
        refused = self.post(client, "payouts/", {
            "amount": 5_000, "vendor": lekki_vendor.pk, "source_account": ledgers["IKJ"].code,
        }, HTTP_IDEMPOTENCY_KEY="reach-single-1")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        for vendor in (self.vendor("VALL", None), self.vendor("VIKJ", self.ikeja)):
            with self.subTest(vendor=vendor.code):
                accepted = self.post(client, "payouts/", {
                    "amount": 5_000, "vendor": vendor.pk, "source_account": ledgers["IKJ"].code,
                }, HTTP_IDEMPOTENCY_KEY=f"reach-single-{vendor.code}")
                self.assertNotIn("No such vendor", str(accepted.data))
                self.assertNotIn("your own branch", str(accepted.data))

    def test_a_payout_leaves_only_a_bank_of_the_clerks_own_branch(self):
        """Ikeja's clerk pays from Ikeja's bank; Lekki's bank, or none named, is refused.

        Naming no account pays from the default cash account, which is no bank of
        Ikeja's here, so the payout would spend money that is not Ikeja's and be
        one Ikeja's clerk could never open.
        """
        from vs_procurement.constants import VendorKycStatus

        from .approvals import ensure_tenant_approval_templates
        from .models import PayoutInstruction

        ensure_tenant_approval_templates(self.tenant)
        ledgers = self._collections_ledgers()
        vendor = self.vendor("VIKJ", self.ikeja)
        type(vendor).objects.filter(pk=vendor.pk).update(
            kyc_status=VendorKycStatus.VERIFIED, bank_account_name=vendor.name,
            bank_account_number="0123456789", bank_code="058")
        client = self.clerk("payments.payout.create")
        for source, message in ((None, "Pay this from a bank account of your own branch."),
                                (ledgers["LEK"].code, f"No account '{ledgers['LEK'].code}'")):
            body = {"amount": 5_000, "vendor": vendor.pk}
            if source:
                body["source_account"] = source
            for path, payload in (("payouts/", body),
                                  ("payout-batches/", {**body, "items": [body]})):
                with self.subTest(source=source, path=path):
                    refused = self.post(client, path, payload,
                                        HTTP_IDEMPOTENCY_KEY=f"reach-source-{path[:-1]}-{source}")
                    self.assertEqual(refused.status_code, 400, refused.data)
                    self.assertIn(message, str(refused.data))
        self.assertFalse(PayoutInstruction.objects.exists())

        accepted = self.post(client, "payouts/", {
            "amount": 5_000, "vendor": vendor.pk, "source_account": ledgers["IKJ"].code,
        }, HTTP_IDEMPOTENCY_KEY="reach-source-ok")
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(
            set(PayoutInstruction.objects.values_list("branch_id", flat=True)), {self.ikeja.pk})

    def test_a_payout_batch_line_to_a_vendor_another_branch_keeps(self):
        lekki_vendor = self.vendor("VLEKB", self.lekki)
        before = PayoutBatch.objects.count()
        refused = self.post(
            self.clerk("payments.payout.create"), "payout-batches/",
            {"items": [{"amount": 5_000, "vendor": lekki_vendor.pk}],
             "source_account": self._collections_ledgers()["IKJ"].code},
            HTTP_IDEMPOTENCY_KEY="reach-batch-1",
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("No such vendor in this entity.", str(refused.data))
        self.assertEqual(PayoutBatch.objects.count(), before)


class SharedFamilyTopUpNamesABranchTests(_FinanceBranchFixture):
    """A top-up for a family every branch shares belongs to the branch it is paid into.

    The Adeyemi family is shared by Ikeja and Lekki and pays a 5,000 top-up with no
    invoice. Nothing on the family says whose money that is, so the account it is
    deposited into does: Mr Eze, the whole-school clerk, deposits it into Lekki's
    collection account, the receipt is Lekki's, and Lekki's clerk, not Ikeja's,
    reaches the collection. Naming no account, or one that is no branch's, is a 400.
    """

    def setUp(self):
        super().setUp()
        _fake_the_provider(self)
        self.ikeja_customer = self.customer(self.books, "CIKJP", self.ikeja)
        self.shared_customer = self.customer(self.books, "CALLP", None)

    post = PaymentsNameOnlyWhatTheClerkReachesTests.post
    _collections_ledgers = PaymentsNameOnlyWhatTheClerkReachesTests._collections_ledgers

    def head_clerk(self):
        n = next(_clerks)
        return TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"head-{n}@corona.test"), "payments.collection.create",
            tenant=self.tenant, role_key=f"head-{n}",
        ))

    def test_a_top_up_must_name_a_branchs_account(self):
        ledgers = self._collections_ledgers()
        head = self.head_clerk()

        refused = self.post(head, "collections/", {"amount": 5_000, "customer": "CALLP"})
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("decides whose money it is", str(refused.data))
        self.assertFalse(CollectionIntent.objects.filter(customer=self.shared_customer).exists())

        accepted = self.post(head, "collections/", {
            "amount": 5_000, "customer": "CALLP", "deposit_account": ledgers["LEK"].code})
        self.assertNotIn("decides whose money", str(accepted.data))
        self.assertNotIn("belongs to", str(accepted.data))
        self.assertIn(accepted.status_code, (200, 201), accepted.data)

    def test_the_receipt_takes_the_deposit_accounts_branch(self):
        from .services import collection_branch_id

        ledgers = self._collections_ledgers()
        self.assertEqual(
            collection_branch_id(customer=self.shared_customer, deposit_account=ledgers["LEK"]),
            self.lekki.pk)
        self.assertEqual(
            collection_branch_id(customer=self.ikeja_customer, deposit_account=ledgers["LEK"]),
            self.ikeja.pk)

    def test_the_collection_is_reached_by_the_deposit_accounts_branch(self):
        from .reach import PaymentsReach
        from vs_rbac.scoping import BranchScope

        ledgers = self._collections_ledgers()
        from .services import collection_branch_id

        top_up = CollectionIntent.objects.create(
            entity=self.books, provider="PAYSTACK", reference="COL-TOPUP", amount=5_000,
            customer=self.shared_customer, deposit_account=ledgers["LEK"],
            branch_id=collection_branch_id(
                customer=self.shared_customer, deposit_account=ledgers["LEK"]))

        def reached(branch):
            scope = BranchScope(frozenset({branch.pk}), include_shared=False)
            return set(PaymentsReach(self.books, scope).collections().values_list("pk", flat=True))

        self.assertIn(top_up.pk, reached(self.lekki))
        self.assertNotIn(top_up.pk, reached(self.ikeja))


class PaymentsShowOnlyWhatTheClerkReachesTests(_FinanceBranchFixture):
    """Every payments list, summary, detail and status change stays within reach.

    Corona's books hold gateway records for Ikeja, for Lekki and for no branch yet,
    on every screen, each carrying the branch its creation gives it. Ikeja's clerk
    sees Ikeja's and counts only those; a Lekki record, or one with no branch, is a
    404 to them, on reading it and on changing it, and nothing changes. The
    bursar, who covers the whole school, sees all of them.

    A payout is the branch of the bank it leaves. Ikeja's bank pays the Ikeja
    vendor (PAY-IKJ) and a vendor every branch shares (PAY-ALL), both in BAT-IKJ;
    Lekki's bank pays the Ikeja vendor (PAY-LEK, in BAT-LEK); and a bank account
    not yet given a branch pays the Ikeja vendor (PAY-NONE, in BAT-NONE). Ikeja's
    clerk reaches the first two and neither of the others.
    """

    KEYS = (
        "payments.collection.view", "payments.virtual_account.view",
        "payments.virtual_account.update", "payments.payout.view",
        "payments.payout.create", "payments.payout_batch.submit", "payments.report.view",
        "payments.webhook.view", "payments.webhook.replay",
    )

    @classmethod
    def setUpTestData(cls):
        """Build every screen's rows once; each test rolls back to them."""
        from django.utils import timezone

        from vs_finance.models import BankAccount, BankStatementLine

        from .constants import CollectionStatus, PaymentAuditAction, PayoutStatus
        from .models import PaymentEvent, PayoutInstruction, WebhookEvent
        from .services import collection_branch_id, payout_branch_id, virtual_account_branch_id

        super().setUpTestData()
        e = cls.books
        vendor = PaymentsNameOnlyWhatTheClerkReachesTests.vendor
        now = timezone.now()
        cls.ikeja_customer = ikeja_c = cls.customer(e, "CIKJ", cls.ikeja)
        lekki_c = cls.customer(e, "CLEK", cls.lekki)
        shared_c = cls.customer(e, "CALL", None)
        lekki_invoice = cls.invoice(e, shared_c, cls.lekki)

        def collection(ref, customer, invoice=None):
            return CollectionIntent.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, amount=1_000,
                customer=customer, invoice=invoice, status=CollectionStatus.SUCCEEDED,
                confirmed_at=now,
                branch_id=collection_branch_id(customer=customer, invoice=invoice))

        cls.collections = {
            "COL-IKJ": collection("COL-IKJ", ikeja_c),
            "COL-LEK": collection("COL-LEK", lekki_c),
            "COL-ALL": collection("COL-ALL", shared_c),
            "COL-LEKINV": collection("COL-LEKINV", shared_c, lekki_invoice),
            "COL-NONE": collection("COL-NONE", None),
        }
        cls.vas = {
            code: VirtualAccount.objects.create(
                entity=e, provider="PAYSTACK", customer=customer,
                account_number=f"90{n}", provider_reference=f"VA-{code}",
                branch_id=virtual_account_branch_id(customer=customer))
            for n, (code, customer) in enumerate(
                (("IKJ", ikeja_c), ("LEK", lekki_c), ("ALL", shared_c)))
        }

        cash_type = Account.objects.get(entity=e, code="1000").account_type
        cls.banks = {}
        for n, (tag, branch) in enumerate((("IKJ", cls.ikeja), ("LEK", cls.lekki),
                                           ("NONE", None))):
            gl = Account.objects.create(entity=e, code=f"116{n}", name=f"Cash {tag}",
                                        account_type=cash_type, is_postable=True)
            bank = BankAccount.objects.create(entity=e, name=f"Bank {tag}", branch=branch,
                                              gl_account=gl)
            BankStatementLine.objects.create(
                bank_account=bank, txn_date=tenant_today(e.tenant), amount=777,
                description=f"LINE-{tag}", reference=f"LINE-{tag}")
            cls.banks[tag] = gl

        vendors = {"IKJ": vendor(cls, "VIKJ", cls.ikeja), "ALL": vendor(cls, "VALL", None)}
        cls.batches = {
            ref: PayoutBatch.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, source_account=cls.banks[bank],
                branch_id=payout_branch_id(e, cls.banks[bank]))
            for ref, bank in (("BAT-IKJ", "IKJ"), ("BAT-LEK", "LEK"), ("BAT-NONE", "NONE"))
        }

        def payout(ref, vendor, bank, batch):
            return PayoutInstruction.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, amount=2_000,
                beneficiary_name=vendor.name, beneficiary_account_number="0123456789",
                vendor_source_type="vs_procurement.Vendor", vendor_source_id=str(vendor.pk),
                source_account=cls.banks[bank], branch_id=payout_branch_id(e, cls.banks[bank]),
                batch=cls.batches[batch], status=PayoutStatus.PAID, confirmed_at=now)

        cls.payouts = {
            "PAY-IKJ": payout("PAY-IKJ", vendors["IKJ"], "IKJ", "BAT-IKJ"),
            "PAY-ALL": payout("PAY-ALL", vendors["ALL"], "IKJ", "BAT-IKJ"),
            "PAY-LEK": payout("PAY-LEK", vendors["IKJ"], "LEK", "BAT-LEK"),
            "PAY-NONE": payout("PAY-NONE", vendors["IKJ"], "NONE", "BAT-NONE"),
        }

        for ref in (*cls.collections, *cls.payouts, *cls.batches):
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.COLLECTION_INITIATED, reference=ref)
        for code, va in cls.vas.items():
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.VIRTUAL_ACCOUNT_CREATED,
                reference=f"REQ-{code}", metadata={"virtual_account_id": va.pk})
            PaymentEvent.objects.create(
                entity=e, action=PaymentAuditAction.VIRTUAL_ACCOUNT_STATUS_CHANGED,
                reference=va.provider_reference)
        for ref in ("COL-IKJ", "COL-LEK"):
            WebhookEvent.objects.create(
                provider="PAYSTACK", dedupe_key=f"wh-{ref}", provider_reference=f"WH-{ref}",
                collection=cls.collections[ref], status="FAILED")
        for ref in ("PAY-ALL", "PAY-LEK", "PAY-NONE"):
            WebhookEvent.objects.create(
                provider="PAYSTACK", dedupe_key=f"wh-{ref}", provider_reference=f"WH-{ref}",
                payout=cls.payouts[ref], status="FAILED")

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

    def detail(self, path, pk):
        return f"/v1/payments/{path}/{pk}/?entity={self.books.code}"

    # -- lists ---------------------------------------------------------------- #

    def test_each_list_holds_only_rows_in_reach(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        every_payout = set(self.payouts)
        cases = (
            ("collections/", "reference", {"COL-IKJ"}, set(self.collections)),
            ("virtual-accounts/", "provider_reference", {"VA-IKJ"},
             {"VA-IKJ", "VA-LEK", "VA-ALL"}),
            ("payouts/", "reference", {"PAY-IKJ", "PAY-ALL"}, every_payout),
            ("payout-batches/", "reference", {"BAT-IKJ"}, set(self.batches)),
            ("movements/", "reference", {"COL-IKJ", "PAY-IKJ", "PAY-ALL"},
             set(self.collections) | every_payout),
            ("transactions/", "reference",
             {"COL-IKJ", "PAY-IKJ", "PAY-ALL", "BAT-IKJ", "REQ-IKJ", "VA-IKJ"},
             set(self.collections) | every_payout | set(self.batches)
             | {"REQ-IKJ", "REQ-LEK", "REQ-ALL", "VA-IKJ", "VA-LEK", "VA-ALL"}),
            ("webhooks/?status=ALL", "provider_reference", {"WH-COL-IKJ", "WH-PAY-ALL"},
             {"WH-COL-IKJ", "WH-COL-LEK", "WH-PAY-ALL", "WH-PAY-LEK", "WH-PAY-NONE"}),
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
        self.assertEqual(rows, {"COL-IKJ", "PAY-IKJ", "PAY-ALL"})
        self.assertEqual(lines, {"LINE-IKJ"})
        rows, lines = seen(self.reader(branch=None))
        self.assertIn("COL-LEK", rows)
        self.assertIn("PAY-LEK", rows)
        self.assertEqual(lines, {"LINE-IKJ", "LINE-LEK", "LINE-NONE"})

    # -- summaries ------------------------------------------------------------ #

    def test_each_summary_counts_only_rows_in_reach(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        cases = (
            ("collections/summary/", lambda d: (d["total"], d["collected"]["kobo"]),
             (1, 1_000), (5, 5_000)),
            ("virtual-accounts/", lambda d: d["kpis"]["total"], 1, 3),
            ("payouts/summary/", lambda d: (d["total"], d["settled7d"]["kobo"]),
             (2, 4_000), (4, 8_000)),
            ("payout-batches/summary/", lambda d: d["total"], 1, 3),
            ("movements/summary/", lambda d: (d["in7d"]["kobo"], d["out7d"]["kobo"]),
             (1_000, 4_000), (5_000, 8_000)),
            ("webhooks/summary/", lambda d: d["failed"], 2, 5),
        )
        for path, pick, clerk_counts, bursar_counts in cases:
            with self.subTest(path=path):
                clerk_data, bursar_data = self.get(clerk, path), self.get(bursar, path)
                if "kpis" not in clerk_data:
                    clerk_data, bursar_data = clerk_data["data"], bursar_data["data"]
                self.assertEqual(pick(clerk_data), clerk_counts)
                self.assertEqual(pick(bursar_data), bursar_counts)

    # -- the paying bank decides a payout ------------------------------------- #

    def test_a_payout_is_reached_by_the_bank_it_leaves_not_the_vendor_it_pays(self):
        """Lekki's bank paying the Ikeja vendor is Lekki's; Ikeja's bank paying anyone is Ikeja's.

        Ikeja's clerk does not see PAY-LEK, though it pays a vendor filed under
        Ikeja, and does see PAY-ALL, though it pays a vendor every branch shares.
        Lekki's clerk sees the reverse.
        """
        ikeja, lekki = self.reader(branch=self.ikeja), self.reader(branch=self.lekki)
        for path in ("payouts/", "movements/"):
            with self.subTest(path=path):
                ikeja_sees, lekki_sees = self.refs(ikeja, path), self.refs(lekki, path)
                self.assertIn("PAY-ALL", ikeja_sees)
                self.assertNotIn("PAY-LEK", ikeja_sees)
                self.assertIn("PAY-LEK", lekki_sees)
                self.assertNotIn("PAY-ALL", lekki_sees)
        self.assertEqual(self.get(lekki, "payouts/summary/")["data"]["total"], 1)

    # -- batches -------------------------------------------------------------- #

    def test_a_batch_is_reached_by_its_own_branch(self):
        """BAT-IKJ, paid from Ikeja's bank, opens with both its lines; BAT-LEK does not open."""
        clerk = self.reader(branch=self.ikeja)
        opened = clerk.get(self.detail("payout-batches", self.batches["BAT-IKJ"].pk))
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertEqual({line["reference"] for line in opened.data["data"]["instructions"]},
                         {"PAY-IKJ", "PAY-ALL"})
        refused = clerk.get(self.detail("payout-batches", self.batches["BAT-LEK"].pk))
        self.assertEqual(refused.status_code, 404, refused.data)
        lekki = self.reader(branch=self.lekki)
        self.assertEqual(
            lekki.get(self.detail("payout-batches", self.batches["BAT-LEK"].pk)).status_code, 200)

    # -- the log and webhooks ------------------------------------------------- #

    def test_the_log_and_webhooks_follow_their_records_own_branch(self):
        """An action or a provider event is reached with the record it names."""
        lekki = self.reader(branch=self.lekki)
        self.assertEqual(
            self.refs(lekki, "transactions/"),
            {"COL-LEK", "COL-LEKINV", "PAY-LEK", "BAT-LEK", "REQ-LEK", "VA-LEK"})
        self.assertEqual(self.refs(lekki, "webhooks/?status=ALL", "provider_reference"),
                         {"WH-COL-LEK", "WH-PAY-LEK"})
        self.assertEqual(self.get(lekki, "webhooks/summary/")["data"]["failed"], 2)

    # -- rows not yet given a branch ------------------------------------------ #

    def test_an_unbranched_gateway_row_is_whole_school_only(self):
        """A row whose own branch is blank is a 404 to a branch reader, whatever it hangs on.

        Every fact around these rows points at Ikeja (the Ikeja family, Ikeja's
        bank, an Ikeja vendor), but their own branch is blank, as a row the
        backfill leaves for an administrator is. Ikeja's clerk cannot open or
        list them; the bursar can.
        """
        from .models import PayoutInstruction

        e = self.books
        family = self.customer(e, "CIKJ2", self.ikeja)
        rows = {
            "collections": CollectionIntent.objects.create(
                entity=e, provider="PAYSTACK", reference="COL-BLANK", amount=1_000,
                customer=family),
            "virtual-accounts": VirtualAccount.objects.create(
                entity=e, provider="PAYSTACK", customer=family, account_number="909",
                provider_reference="VA-BLANK"),
            "payout-batches": PayoutBatch.objects.create(
                entity=e, provider="PAYSTACK", reference="BAT-BLANK",
                source_account=self.banks["IKJ"]),
        }
        vendor = self.vendor("VIKJ2", self.ikeja)
        PayoutInstruction.objects.create(
            entity=e, provider="PAYSTACK", reference="PAY-BLANK", amount=2_000,
            beneficiary_name=vendor.name, beneficiary_account_number="0123456789",
            vendor_source_type="vs_procurement.Vendor", vendor_source_id=str(vendor.pk),
            source_account=self.banks["IKJ"], batch=rows["payout-batches"])

        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        for path, row in rows.items():
            with self.subTest(path=path):
                self.assertEqual(clerk.get(self.detail(path, row.pk)).status_code, 404)
                self.assertEqual(bursar.get(self.detail(path, row.pk)).status_code, 200)
        for path, key, ref in (("collections/", "reference", "COL-BLANK"),
                               ("virtual-accounts/", "provider_reference", "VA-BLANK"),
                               ("payouts/", "reference", "PAY-BLANK"),
                               ("payout-batches/", "reference", "BAT-BLANK")):
            with self.subTest(list=path):
                self.assertNotIn(ref, self.refs(clerk, path, key))
                self.assertIn(ref, self.refs(bursar, path, key))

    def test_at_a_one_branch_school_an_unbranched_row_is_the_branchs(self):
        """A clerk pinned to a school's only branch is not narrowed, so the blank row is theirs."""
        from vs_rbac.scoping import transaction_branch_scope_for_user

        from .reach import PaymentsReach

        row = CollectionIntent.objects.create(
            entity=self.solo_books, provider="PAYSTACK", reference="COL-SOLO", amount=1_000)
        clerk = self.grant(self.user_for(self.solo_tenant, "solo-clerk@single.test"),
                           *self.KEYS, tenant=self.solo_tenant, role_key="solo-clerk",
                           branch=self.solo_main)
        scope = transaction_branch_scope_for_user(clerk, tenant=self.solo_tenant)
        self.assertFalse(scope.is_narrowed)
        self.assertIn(row, PaymentsReach(self.solo_books, scope).collections())

    # -- detail and status changes -------------------------------------------- #

    def test_another_branchs_rows_are_unknown_on_detail(self):
        clerk, bursar = self.reader(branch=self.ikeja), self.reader(branch=None)
        lekki = {
            "collections": self.collections["COL-LEK"].pk,
            "virtual-accounts": self.vas["LEK"].pk,
            "payout-batches": self.batches["BAT-LEK"].pk,
        }
        for path, pk in lekki.items():
            with self.subTest(path=path):
                self.assertEqual(clerk.get(self.detail(path, pk)).status_code, 404)
                self.assertEqual(bursar.get(self.detail(path, pk)).status_code, 200)
        own = self.detail("collections", self.collections["COL-IKJ"].pk)
        self.assertEqual(clerk.get(own).status_code, 200)

    def _okafor_collection(self):
        """The Okafors, filed under Ikeja, paid a Lekki invoice online: Lekki's money."""
        from django.utils import timezone

        from .constants import CollectionStatus, PaymentAuditAction
        from .models import PaymentEvent, WebhookEvent
        from .services import collection_branch_id

        okafor = self.customer(self.books, "COKAF", self.ikeja)
        invoice = self.invoice(self.books, okafor, self.lekki)
        row = CollectionIntent.objects.create(
            entity=self.books, provider="PAYSTACK", reference="COL-OKAF", amount=1_000,
            customer=okafor, invoice=invoice,
            branch_id=collection_branch_id(customer=okafor, invoice=invoice),
            status=CollectionStatus.SUCCEEDED, confirmed_at=timezone.now())
        PaymentEvent.objects.create(
            entity=self.books, action=PaymentAuditAction.COLLECTION_INITIATED,
            reference="COL-OKAF")
        WebhookEvent.objects.create(
            provider="PAYSTACK", dedupe_key="wh-COL-OKAF", provider_reference="WH-COL-OKAF",
            collection=row, status="FAILED")
        return row

    def test_a_collection_for_an_invoice_is_reached_by_the_invoices_branch(self):
        """Tola keeps Lekki's books, so they see and open the Okafors' Lekki payment."""
        okafor = self._okafor_collection()
        tola = self.reader(branch=self.lekki)
        detail = f"/v1/payments/collections/{okafor.pk}/?entity={self.books.code}"

        self.assertEqual(tola.get(detail).status_code, 200)
        for path, key in (("collections/", "reference"), ("movements/", "reference"),
                          ("transactions/", "reference"),
                          ("webhooks/?status=ALL", "provider_reference")):
            with self.subTest(path=path):
                self.assertIn("OKAF", " ".join(self.refs(tola, path, key)))
        summary = self.get(tola, "collections/summary/")["data"]
        self.assertEqual((summary["total"], summary["collected"]["kobo"]), (3, 3_000))

    def test_a_collection_for_another_branchs_invoice_is_unknown_to_the_familys_branch(self):
        """Ikeja files the Okafors, but the Lekki payment is not Ikeja's to see."""
        okafor = self._okafor_collection()
        clerk = self.reader(branch=self.ikeja)
        detail = f"/v1/payments/collections/{okafor.pk}/?entity={self.books.code}"

        self.assertEqual(clerk.get(detail).status_code, 404)
        for path, key in (("collections/", "reference"), ("movements/", "reference"),
                          ("transactions/", "reference"),
                          ("webhooks/?status=ALL", "provider_reference")):
            with self.subTest(path=path):
                self.assertNotIn("OKAF", " ".join(self.refs(clerk, path, key)))
        summary = self.get(clerk, "collections/summary/")["data"]
        self.assertEqual((summary["total"], summary["collected"]["kobo"]), (1, 1_000))
        self.assertEqual(self.reader(branch=None).get(detail).status_code, 200)

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
                         {"COL-IKJ"})
        self.assertEqual(set(_payouts(scope).values_list("reference", flat=True)),
                         {"PAY-IKJ", "PAY-ALL"})
        whole = ScopeContext(tenant=self.tenant, entity=self.books, user=None)
        self.assertEqual(_collections(whole).count(), 5)

    def test_another_branchs_batch_and_webhook_cannot_be_acted_on(self):
        from .models import WebhookEvent

        clerk = self.reader(branch=self.ikeja)
        batch = self.batches["BAT-LEK"]
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
    the approval route puts them on every batch. A batch is the branch of the bank
    it pays from. One pays a vendor every branch shares from Ikeja's bank, and is
    Ikeja's; one pays an Ikeja vendor from Lekki's bank, and is Lekki's; one pays
    the Ikeja vendor from a bank account not yet given a branch. Bello sees and
    decides only the first. The approval inbox, the instance behind it and its
    approve and reject answer the same way, while the head bursar, who covers the
    whole school, sees and decides all three.
    """

    def setUp(self):
        from vs_finance.models import BankAccount
        from vs_workflow.constants import GroupMemberKind
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        from .approvals import ensure_tenant_approval_templates
        from .constants import WF_DEFAULT_APPROVE_GROUP, PayoutStatus
        from .models import PayoutInstruction
        from .services import payout_branch_id, submit_payout_batch_for_approval

        super().setUp()
        e = self.books
        vendor = PaymentsNameOnlyWhatTheClerkReachesTests.vendor
        vendors = {"IKJ": vendor(self, "AVIKJ", self.ikeja), "ALL": vendor(self, "AVALL", None)}
        cash_type = Account.objects.get(entity=e, code="1000").account_type
        banks = {}
        for n, (tag, branch) in enumerate((("IKJ", self.ikeja), ("LEK", self.lekki),
                                           ("NONE", None))):
            banks[tag] = Account.objects.create(entity=e, code=f"115{n}", name=f"Bank {tag}",
                                                account_type=cash_type, is_postable=True)
            BankAccount.objects.create(entity=e, name=f"Bank {tag}", branch=branch,
                                       gl_account=banks[tag])

        def batch(ref, bank, code):
            source = banks[bank]
            row = PayoutBatch.objects.create(
                entity=e, provider="PAYSTACK", reference=ref, source_account=source,
                branch_id=payout_branch_id(e, source))
            PayoutInstruction.objects.create(
                entity=e, provider="PAYSTACK", reference=f"{ref}-0", amount=2_000,
                beneficiary_name=vendors[code].name, beneficiary_account_number="0123456789",
                vendor_source_type="vs_procurement.Vendor",
                vendor_source_id=str(vendors[code].pk), batch=row, source_account=source,
                branch_id=row.branch_id, status=PayoutStatus.PENDING)
            return row

        self.batches = {
            "ABAT-IKJ": batch("ABAT-IKJ", "IKJ", "ALL"),
            "ABAT-LEK": batch("ABAT-LEK", "LEK", "IKJ"),
            "ABAT-NONE": batch("ABAT-NONE", "NONE", "IKJ"),
        }

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
            ref: submit_payout_batch_for_approval(row, requested_by=clerk)
            for ref, row in self.batches.items()
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
        self.assertEqual(self.inbox(self.bello), {str(self.batches["ABAT-IKJ"].pk)})
        self.assertEqual(self.inbox(self.head), {str(b.pk) for b in self.batches.values()})

    def test_the_finance_dashboard_counts_only_the_batches_in_reach(self):
        from vs_finance.dashboard_blocks import approvals_waiting_on

        self.assertEqual(approvals_waiting_on(self.books, self.bello)["total"], 1)
        self.assertEqual(approvals_waiting_on(self.books, self.head)["total"], 3)

    def test_a_batch_out_of_reach_cannot_be_read_approved_or_rejected(self):
        client = TenantAPIClient(user=self.bello)
        for reference in ("ABAT-LEK", "ABAT-NONE"):
            instance = self.instances[reference]
            with self.subTest(reference=reference):
                self.assertEqual(
                    client.get(f"/v1/workflow/instances/{instance.pk}/").status_code, 404)
                for action in ("APPROVED", "REJECTED"):
                    self.assertEqual(self.act(self.bello, reference, action).status_code, 404)
                instance.refresh_from_db()
                self.assertEqual(instance.status, "IN_PROGRESS")

        self.assertEqual(self.act(self.bello, "ABAT-IKJ", "REJECTED").status_code, 200)
        self.assertEqual(self.act(self.head, "ABAT-LEK", "REJECTED").status_code, 200)


class HeldSettlementsStayWithinReachTests(_FinanceBranchFixture):
    """The platform's settlements of held money are read by the branch each one pays.

    The platform holds Corona's online money and pays Ikeja N100 and Lekki N200
    on the same day; Rival Group, which also has an Ikeja Branch, is paid N400.
    Lekki's bursar sees Lekki's settlement in the settlements list, the movements
    feed and its summary, and nothing of Ikeja's. Corona's bursar, who covers the
    whole school, sees both of Corona's and never Rival's.
    """

    KEYS = ("payments.report.view",)

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone

        from vs_finance.models import BankAccount

        from .constants import HeldSettlementStatus, PayoutPurpose
        from .models import HeldSettlement

        super().setUpTestData()
        now = timezone.now()

        def settlement(entity, branch, amount, code, *, batch=None):
            gl = Account.objects.create(entity=entity, code=code, name=f"Collections {code}",
                                        account_type="ASSET", is_postable=True)
            bank = BankAccount.objects.create(entity=entity, name=f"Collections {code}",
                                              branch=branch, gl_account=gl)
            return HeldSettlement.objects.create(
                entity=entity, tenant=entity.tenant, branch=branch,
                status=HeldSettlementStatus.PAID, run_on=tenant_today(entity.tenant),
                cutoff=now, gross=amount, amount=amount, bank_account=bank, batch=batch,
                paid_at=now)

        lekki_transfer = PayoutBatch.objects.create(
            entity=cls.books, provider="PAYSTACK", reference="SET-LEK",
            purpose=PayoutPurpose.SETTLEMENT)
        cls.ikeja_run = settlement(cls.books, cls.ikeja, 10_000, "1181")
        cls.lekki_run = settlement(cls.books, cls.lekki, 20_000, "1182", batch=lekki_transfer)
        cls.rival_run = settlement(cls.rival_books, cls.rival_branch, 40_000, "1181")

    def reader(self, tenant, branch):
        n = next(_clerks)
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"held-reader-{n}@corona.test"), *self.KEYS,
            tenant=tenant, role_key=f"held-reader-{n}", branch=branch,
        ))

    def seen(self, client, books=None):
        """What ``client`` reaches in the list, the feed and the summary of ``books``."""
        code = (books or self.books).code
        listed = client.get(f"/v1/payments/held-settlements/?entity={code}")
        feed = client.get(f"/v1/payments/movements/?entity={code}")
        summary = client.get(f"/v1/payments/movements/summary/?entity={code}")
        for response in (listed, feed, summary):
            self.assertEqual(response.status_code, 200, response.data)
        return (
            {row["id"] for row in listed.data["data"]},
            {row["gateway_id"] for row in feed.data["data"] if row["kind"] == "settlement"},
            summary.data["data"]["transfers7d"]["kobo"],
        )

    def test_a_branch_reader_sees_only_their_branchs_settlements(self):
        lekki = self.reader(self.tenant, self.lekki)
        self.assertEqual(self.seen(lekki), ({self.lekki_run.pk}, {self.lekki_run.pk}, 20_000))
        ikeja = self.reader(self.tenant, self.ikeja)
        self.assertEqual(self.seen(ikeja), ({self.ikeja_run.pk}, {self.ikeja_run.pk}, 10_000))
        yaba = self.reader(self.tenant, self.yaba)
        self.assertEqual(self.seen(yaba), (set(), set(), 0))

    def test_a_whole_school_reader_is_not_narrowed(self):
        """The bursar sees every settlement of the school, through the entity filter alone."""
        from vs_rbac.scoping import transaction_branch_scope_for_user

        from .models import HeldSettlement
        from .reach import PaymentsReach

        both = {self.ikeja_run.pk, self.lekki_run.pk}
        self.assertEqual(self.seen(self.reader(self.tenant, None)), (both, both, 30_000))

        bursar = self.grant(self.user_for(self.tenant, "held-bursar@corona.test"), *self.KEYS,
                            tenant=self.tenant, role_key="held-bursar", branch=None)
        reach = PaymentsReach(self.books, transaction_branch_scope_for_user(bursar))
        self.assertFalse(reach.is_narrowed)
        self.assertEqual(str(reach.held_settlements().query),
                         str(HeldSettlement.objects.filter(entity=self.books).query))

    def test_the_summary_and_the_feed_agree_on_a_provider(self):
        """Lekki's money went by Paystack and Ikeja's settlement sent nothing.

        Asked about Paystack alone, the summary counts what the feed lists.
        """
        bursar = self.reader(self.tenant, None)
        feed = bursar.get(f"/v1/payments/movements/?entity={self.books.code}&provider=PAYSTACK")
        self.assertEqual({row["gateway_id"] for row in feed.data["data"]
                          if row["kind"] == "settlement"}, {self.lekki_run.pk})
        summary = bursar.get(
            f"/v1/payments/movements/summary/?entity={self.books.code}&provider=PAYSTACK")
        self.assertEqual(summary.data["data"]["transfers7d"]["kobo"], 20_000)

    def test_another_schools_settlements_are_never_visible(self):
        """Rival's Ikeja Branch shares a name with Corona's and nothing else."""
        corona_ikeja = self.reader(self.tenant, self.ikeja)
        corona_bursar = self.reader(self.tenant, None)
        for client in (corona_ikeja, corona_bursar):
            listed, fed, _ = self.seen(client)
            self.assertNotIn(self.rival_run.pk, listed | fed)
            refused = client.get(f"/v1/payments/held-settlements/?entity={self.rival_books.code}")
            self.assertEqual(refused.status_code, 404, refused.data)

        rival = self.reader(self.rival_tenant, self.rival_branch)
        self.assertEqual(self.seen(rival, self.rival_books),
                         ({self.rival_run.pk}, {self.rival_run.pk}, 40_000))

    def test_a_held_settlement_always_names_its_branch(self):
        """No settlement is unbranched, so none can be whole-school only.

        A settlement is one branch's money on its way to that branch's bank, and
        the column refuses a blank branch outright. Reach reads it exclusively
        all the same, as it reads every transaction.
        """
        from django.db import IntegrityError, transaction

        from .models import HeldSettlement

        row = HeldSettlement.objects.get(pk=self.ikeja_run.pk)
        row.pk, row.branch = None, None
        with self.assertRaises(IntegrityError), transaction.atomic():
            row.save()


class PaymentsViewsStartFromTheReachTests(SimpleTestCase):
    """No payments view reaches a gateway table except through :class:`PaymentsReach`.

    A view that filtered ``CollectionIntent.objects`` or ``HeldSettlement.objects``
    itself would show Ikeja's clerk the whole school again, and nothing else would
    notice. The only exceptions read at platform scope, for platform staff only:
    webhook events matched to no tenant, and every tenant's held settlements for
    the operators who put them forward.
    """

    def test_views_name_no_gateway_manager(self):
        import inspect
        import re

        from . import views, views_custody

        platform_scope = {
            views: (views._unattributed_webhooks,),
            views_custody: (views_custody.PlatformHeldSettlementListView,
                            views_custody.PlatformHeldSettlementSubmitView),
        }
        for module, exempt in platform_scope.items():
            source = inspect.getsource(module)
            for reader in exempt:
                source = source.replace(inspect.getsource(reader), "")
            found = re.findall(
                r"\b(CollectionIntent|VirtualAccount|PayoutInstruction|PayoutBatch|PaymentEvent"
                r"|WebhookEvent|HeldSettlement)\.(?:objects|all_objects)\b",
                source)
            with self.subTest(module=module.__name__):
                self.assertEqual(found, [], "Start from vs_payments.reach.PaymentsReach instead.")
