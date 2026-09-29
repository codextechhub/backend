"""The transactions log shows named figures of each gateway action, never its raw metadata.

Every gateway action writes free-form JSON beside its row: internal record ids,
per-line breakdowns, and whatever any writer, now or later, chooses to keep there.
Corona's payments officer reads the log to see what happened to a payout; she is
shown the payout's gross, WHT and amount sent, and the code a refusal carried,
and nothing else that row happens to hold. A virtual account's number appears in
its log messages only for a reader whose roles let them read that number.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.tests.deep_payload import closed_request

from .constants import PaymentAuditAction
from .models import PaymentEvent
from .serializers import PaymentEventSerializer

#: What a payout's initiation row stores, as a writer might fill it.
STORED = {
    "gross_amount": 10_000, "wht_amount": 500, "transfer_amount": 9_500,
    "error_code": "PROVIDER_DOWN",
    "journal_entry_id": 71, "vendor_payment_id": 12, "virtual_account_id": 4,
    "beneficiary_account_number": "0123456789",
    "provider_payload": {"authorization_code": "AUTH_secret", "account_number": "0123456789"},
    "wht": [{"reference": "PAY-1", "wht_amount": 500}],
    "channel": {"not": "a scalar"},
}


class TransactionsLogTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        self.payout_event = PaymentEvent.objects.create(
            entity=self.books, action=PaymentAuditAction.PAYOUT_INITIATED,
            reference="PAY-1", metadata=STORED)
        self.va_event = PaymentEvent.objects.create(
            entity=self.books, action=PaymentAuditAction.VIRTUAL_ACCOUNT_CREATED,
            reference="REQ-1", message="Virtual account 9012345678 for CALL.")

    def rows(self, *, reads_account_numbers=False):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import set_field_access

        user = self.grant(self.user_for(self.tenant, "log-reader@corona.test"),
                          "payments.report.view", tenant=self.tenant, role_key="log-reader")
        if reads_account_numbers:
            set_field_access(TenantRoleTemplate.objects.get(tenant=self.tenant, key="log-reader"),
                             "payments.virtual_account.account_number", read=True, write=False)
        response = TenantAPIClient(user=user).get(
            f"/v1/payments/transactions/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["reference"]: row for row in response.data["data"]}

    def test_a_row_carries_only_the_named_figures(self):
        row = self.rows()["PAY-1"]
        self.assertEqual(row["metadata"], {
            "gross_amount": 10_000, "wht_amount": 500, "transfer_amount": 9_500,
            "error_code": "PROVIDER_DOWN",
        })

    def test_a_row_with_nothing_named_carries_an_empty_object(self):
        self.assertEqual(self.rows()["REQ-1"]["metadata"], {})

    def test_the_account_number_leaves_the_message_for_a_reader_who_may_not_read_it(self):
        request, _keys = closed_request(self.tenant, "payments.virtual_account")
        closed = PaymentEventSerializer(self.va_event, context={"request": request}).data
        self.assertEqual(closed["message"], "Virtual account for CALL.")

        opened = self.rows(reads_account_numbers=True)["REQ-1"]
        self.assertEqual(opened["message"], "Virtual account 9012345678 for CALL.")
