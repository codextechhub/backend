"""The Finance dashboard for a bursar pinned to a tenant's only branch.

Single Site has one branch, Main. A bursar whose grant is pinned to Main gets the
dashboard an unpinned bursar gets: the tenant-level blocks (the period close,
the budget, the tenant's own ledger) are sent, and nothing is marked narrowed,
because the pin carries no meaning where there is nothing else to be pinned
against. The day Single Site opens a second branch, the same grant narrows her
again and the tenant-level blocks are withheld.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient

from .tests_branch_scope import _FinanceBranchFixture

KEYS = ("finance.report.view", "finance.invoice.view", "finance.period.view")


class OneBranchDashboardTests(_FinanceBranchFixture):
    """A pin to the only branch withholds nothing, until a second branch opens."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.solo_books
        cls.posted(cls.invoice(e, cls.customer(e, "SMAIN", cls.solo_main), cls.solo_main))
        cls.posted(cls.invoice(e, cls.customer(e, "SALL", None), None))

    @classmethod
    def posted(cls, invoice):
        """Post ``invoice`` through the real service, onto a heading that takes postings."""
        from vs_finance.models import Account, InvoiceLine
        from vs_finance.receivables import post_invoice

        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=invoice.entity, code="4100"),
        )
        post_invoice(invoice)
        return invoice

    def client_for(self, email, *, branch=None):
        user = self.grant(
            self.user_for(self.solo_tenant, email), *KEYS,
            tenant=self.solo_tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def dashboard(self, client):
        response = client.get(f"/v1/finance/reports/dashboard/?entity={self.solo_books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_a_bursar_pinned_to_the_only_branch_gets_the_tenant_level_blocks(self):
        pinned = self.dashboard(self.client_for("solo-pinned@fin.test", branch=self.solo_main))
        unbound = self.dashboard(self.client_for("solo-unbound@fin.test"))

        self.assertFalse(pinned["narrowed"])
        self.assertIsNotNone(pinned["close_progress"])
        for block in ("kpis", "ar_aging", "close_progress", "revenue_vs_budget", "branches"):
            self.assertEqual(pinned[block], unbound[block], block)

    def test_a_second_branch_withholds_them_again(self):
        from vs_rbac.tests.helpers import make_branch

        client = self.client_for("solo-later@fin.test", branch=self.solo_main)
        self.assertFalse(self.dashboard(client)["narrowed"])

        make_branch(self.solo_school, name="Ajah Branch", is_main=False)

        data = self.dashboard(client)
        self.assertTrue(data["narrowed"])
        self.assertIsNone(data["close_progress"])
        self.assertIsNone(data["revenue_vs_budget"])
