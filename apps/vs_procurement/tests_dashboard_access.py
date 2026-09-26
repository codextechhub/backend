"""The Procurement dashboard answers each reader with what they may see.

A storekeeper at Lekki raises requisitions and holds nothing else; the head of
procurement reads the analytics school-wide; a Lekki analyst reads them for Lekki
only. All three open the dashboard, since it is the console's landing page. The
storekeeper gets their own approval queue and no spend figures; the head gets
every block; the Lekki analyst gets spend for Lekki but not the activity feed,
which cannot be narrowed by branch.
"""
from __future__ import annotations

from django.test import TestCase

from .tests import _BranchTenantsFixture


class ProcurementDashboardAccessTests(_BranchTenantsFixture, TestCase):
    """Which blocks each reader receives."""

    def reader(self, email, *keys, branch=None):
        client = self.client_for(self.multi_tenant, email)
        for key in keys:
            self.grant(client.test_user, key, tenant=self.multi_tenant,
                       role_key=f"role-{email}", branch=branch)
        return client

    def dashboard(self, client):
        response = client.get(
            f"/v1/procurement/reports/dashboard/?entity={self.multi.entity.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]

    def test_a_requisition_raiser_gets_their_queue_and_no_spend(self):
        data = self.dashboard(
            self.reader("storekeeper@t.com", "procurement.requisition.view", branch=self.lekki),
        )

        self.assertIsNotNone(data["kpis"]["pending_approvals"])
        for kpi in ("spend", "open_purchase_orders", "overdue_invoices", "active_vendors"):
            self.assertIsNone(data["kpis"][kpi], kpi)
        for block in ("spend_by_category", "committed_vs_spent", "top_vendors",
                      "exceptions", "bills_due", "contracts_ending", "recent_activity"):
            self.assertIsNone(data[block], block)

    def test_a_school_wide_analyst_gets_every_analytics_block(self):
        data = self.dashboard(self.reader("head@t.com", "procurement.analytics.view"))

        self.assertFalse(data["narrowed"])
        self.assertIsNotNone(data["kpis"]["spend"])
        self.assertIsNotNone(data["committed_vs_spent"])
        self.assertIsNotNone(data["recent_activity"])

    def test_a_branch_analyst_does_not_get_the_unnarrowed_activity_feed(self):
        data = self.dashboard(
            self.reader("analyst-lekki@t.com", "procurement.analytics.view", branch=self.lekki),
        )

        self.assertTrue(data["narrowed"])
        self.assertIsNotNone(data["kpis"]["spend"])
        self.assertIsNone(data["recent_activity"])

    def test_a_reader_with_no_procurement_key_is_refused(self):
        client = self.reader("finance-only@t.com", "finance.invoice.view")
        response = client.get(
            f"/v1/procurement/reports/dashboard/?entity={self.multi.entity.code}",
        )
        self.assertEqual(response.status_code, 403)
