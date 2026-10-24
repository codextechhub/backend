"""Adjustment documents enforce their fields across direct and batch routes."""

from core.test_utils import TenantAPIClient
from vs_finance.models import Concession, CustomerCreditTransfer, PaymentPlan, Refund, WriteOffRequest
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_bank_document_rework import _ReworkFixture


class AdjustmentFieldAccessTests(_ReworkFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for resource in ("concession", "writeoff", "credittransfer", "paymentplan", "refund"):
            install_declared_fields(f"finance.{resource}")
        cls.customer_row = cls.customer(cls.books, "FLS-ADJ", cls.ikeja)
        cls.other_customer = cls.customer(cls.books, "FLS-ADJ-2", cls.ikeja)
        cls.invoice_row = cls.invoice(cls.books, cls.customer_row, cls.ikeja)
        cls.clerk = cls.grant(
            cls.user_for(cls.tenant, "adjustments@field.test"),
            "finance.concession.view", "finance.concession.create",
            "finance.writeoff.view", "finance.writeoff.create",
            "finance.credittransfer.view", "finance.credittransfer.create",
            "finance.paymentplan.view", "finance.paymentplan.create",
            "finance.refund.view", "finance.refund.create",
            "finance.invoice.writeoff",
            tenant=cls.tenant, role_key="adjustment-field-clerk",
        )
        role = TenantRoleTemplate.objects.get(
            tenant=cls.tenant, key="adjustment-field-clerk")
        for key in (
            "finance.concession.amount", "finance.writeoff.amount",
            "finance.credittransfer.amount", "finance.paymentplan.total_amount",
            "finance.refund.amount",
        ):
            set_field_access(role, key, read=False, write=False)
        cls.bank_reader = cls.grant(
            cls.user_for(cls.tenant, "refund-bank-reader@field.test"),
            "finance.refund.view", tenant=cls.tenant, role_key="refund-bank-reader",
        )
        cls.bank_locked = cls.grant(
            cls.user_for(cls.tenant, "refund-bank-locked@field.test"),
            "finance.refund.view", "finance.refund.create",
            tenant=cls.tenant, role_key="refund-bank-locked",
        )
        bank_role = TenantRoleTemplate.objects.get(
            tenant=cls.tenant, key="refund-bank-locked")
        set_field_access(bank_role, "finance.refund.bank_account",
                         read=False, write=False)

    def test_hidden_amounts_leave_document_reads(self):
        concession = Concession.objects.create(
            entity=self.books, branch=self.ikeja, customer=self.customer_row,
            invoice=self.invoice_row, concession_date="2026-01-15", amount=100,
        )
        writeoff = WriteOffRequest.objects.create(
            entity=self.books, branch=self.ikeja, invoice=self.invoice_row, amount=100,
        )
        transfer = CustomerCreditTransfer.objects.create(
            entity=self.books, branch=self.ikeja, from_customer=self.customer_row,
            to_customer=self.other_customer, transfer_date="2026-01-15", amount=100,
        )
        plan = PaymentPlan.objects.create(
            entity=self.books, branch=self.ikeja, customer=self.customer_row,
            invoice=self.invoice_row, start_date="2026-01-15", total_amount=100,
        )
        refund = Refund.objects.create(
            entity=self.books, branch=self.ikeja, customer=self.customer_row,
            refund_date="2026-01-15", amount=100,
        )
        client = TenantAPIClient(user=self.clerk)
        for route, row in (
            ("concessions", concession), ("write-offs", writeoff),
            ("credit-transfers", transfer), ("payment-plans", plan),
            ("refunds", refund),
        ):
            with self.subTest(route=route):
                response = client.get(self.url(f"{route}/{row.pk}/"))
                self.assertEqual(response.status_code, 200, response.data)
                self.assertNotIn("amount", response.data["data"])
                self.assertNotIn("amount_naira", response.data["data"])
                self.assertNotIn("total_amount", response.data["data"])

    def test_create_routes_refuse_amounts_without_write_access(self):
        client = TenantAPIClient(user=self.clerk)
        cases = (
            ("concessions/", {
                "customer": self.customer_row.pk, "invoice": self.invoice_row.pk,
                "concession_date": "2026-01-15", "amount": 100,
            }),
            ("write-offs/", {"invoice": self.invoice_row.pk, "amount": 100}),
            ("credit-transfers/", {
                "from_customer": self.customer_row.pk,
                "to_customer": self.other_customer.pk,
                "transfer_date": "2026-01-15", "amount": 100,
            }),
            ("payment-plans/", {
                "customer": self.customer_row.pk, "start_date": "2026-01-15",
                "total_amount": 100,
            }),
            ("refunds/", {
                "customer": self.customer_row.pk, "refund_date": "2026-01-15",
                "amount": 100,
            }),
        )
        for route, body in cases:
            with self.subTest(route=route):
                response = client.post(self.url(route), body, format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_batch_refund_refuses_hidden_amount_before_creating_documents(self):
        before = Refund.objects.filter(entity=self.books).count()
        response = TenantAPIClient(user=self.clerk).post(
            self.url("ar-adjustments/batch/"), {
                "kind": "REFUND", "action": "DRAFT", "date": "2026-01-15",
                "bank_account": self.ikeja_bank.pk,
                "items": [{"customer": self.customer_row.pk, "amount": 100}],
            }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(Refund.objects.filter(entity=self.books).count(), before)

    def test_refund_bank_account_follows_read_switch(self):
        refund = Refund.objects.create(
            entity=self.books, branch=self.ikeja, customer=self.customer_row,
            refund_date="2026-01-15", amount=100, bank_account=self.ikeja_bank,
        )
        route = self.url(f"refunds/{refund.pk}/")
        visible = TenantAPIClient(user=self.bank_reader).get(route)
        hidden = TenantAPIClient(user=self.bank_locked).get(route)
        self.assertEqual(visible.status_code, 200, visible.data)
        self.assertEqual(hidden.status_code, 200, hidden.data)
        self.assertEqual(visible.data["data"]["bank_account_id"], self.ikeja_bank.pk)
        self.assertNotIn("bank_account_id", hidden.data["data"])

    def test_direct_and_batch_refunds_refuse_locked_bank_account(self):
        client = TenantAPIClient(user=self.bank_locked)
        before = Refund.objects.filter(entity=self.books).count()
        for route, body in (
            ("refunds/", {
                "customer": self.customer_row.pk, "refund_date": "2026-01-15",
                "amount": 100, "bank_account": self.ikeja_bank.pk,
            }),
            ("ar-adjustments/batch/", {
                "kind": "REFUND", "action": "DRAFT", "date": "2026-01-15",
                "bank_account": self.ikeja_bank.pk,
                "items": [{"customer": self.customer_row.pk, "amount": 100}],
            }),
        ):
            with self.subTest(route=route):
                response = client.post(self.url(route), body, format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(Refund.objects.filter(entity=self.books).count(), before)
