"""Tenant-wide spend, read by a buyer pinned to a tenant's only branch.

Procurement reads a blank branch on a spend document exclusively: a requisition
raised for the school as a whole is not a branch's, so a buyer pinned to one
branch of several does not see it. Harbour Primary has one branch, Main, and
Tolu's grant is pinned to it. The pin carries no meaning there, so she reads
Harbour's tenant-wide requisitions exactly as an unpinned buyer would. The day
Harbour opens Ajah, the same grant narrows her to Main's requisitions again.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .models import PurchaseRequisition


class OneBranchSpendReadingTests(TestCase):
    """The exclusive reading of spend recedes where a tenant has one branch."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.harbour = make_school(slug="harbour-proc-read", name="Harbour Primary")
        cls.main = make_branch(cls.harbour, name="Main Branch")
        cls.books = LedgerEntity.objects.create(
            name="HBRPRD Books", code="HBRPRD", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.harbour.tenant,
        )
        seed_chart_of_accounts(cls.books)

        role = make_role(cls.harbour, name="Buyer", key="buyer")
        make_role_permission(role, make_permission("procurement.requisition.view"))
        cls.tolu = make_school_admin(cls.main, email="tolu@harbour-proc-read.example.com")
        make_assignment(cls.harbour, cls.tolu, role, branch=cls.main)

        cls.at_main = cls.requisition(cls.main, "Chairs for Main")
        cls.tenant_wide = cls.requisition(None, "Exam papers for the school")

    @classmethod
    def requisition(cls, branch, title):
        return PurchaseRequisition.objects.create(
            entity=cls.books, branch=branch, title=title,
            request_date=datetime.date(2026, 1, 3),
        )

    def listed(self):
        response = TenantAPIClient(user=self.tolu).get(
            f"/v1/procurement/requisitions/?entity={self.books.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_a_buyer_pinned_to_the_only_branch_reads_tenant_wide_spend(self):
        self.assertEqual(self.listed(), {self.at_main.pk, self.tenant_wide.pk})

    def test_a_second_branch_hides_tenant_wide_spend_from_her_again(self):
        self.assertIn(self.tenant_wide.pk, self.listed())

        make_branch(self.harbour, name="Ajah Branch", is_main=False)

        self.assertEqual(self.listed(), {self.at_main.pk})
