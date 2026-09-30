"""A stock movement's finance audit entry is its store's branch's.

The entry is about the stock item, which no branch owns, so the store the
movement happened at decides whose entry it is. Paper received into the Lekki
store is Lekki's to read; paper received into a central store not yet given a
branch is read by whole-school readers only. An entry written before entries
carried a branch names its store in its details, and the branch backfill reads
it there.
"""
from __future__ import annotations

import datetime
import io

from django.core.management import call_command

from vs_finance.constants import FinanceAuditAction
from vs_finance.exceptions import FinanceError
from vs_finance.models import FinanceAuditLog

from .models import StockLocation
from .stock import issue_stock
from .tests_dashboard_stock import _StockFixture


class StockEntriesTakeTheirStoresBranchTests(_StockFixture):

    def entries(self, action):
        return FinanceAuditLog.objects.filter(entity=self.multi.entity, action=action)

    def test_a_receipt_is_the_branch_of_the_store_it_went_into(self):
        received = {
            e.metadata["location_id"]: e.branch_id
            for e in self.entries(FinanceAuditAction.STOCK_RECEIVED)
        }

        self.assertEqual(received, {self.central.pk: self.central.branch_id, self.lekki_store.pk: self.lekki.pk})

    def test_a_refusal_naming_another_books_store_has_no_branch(self):
        from vs_rbac.tests.helpers import make_branch

        elsewhere = make_branch(self.foreign_school, name="Elsewhere Branch", is_main=False)
        other = StockLocation.objects.create(
            entity=self.foreign.entity, code="ELSE", name="Elsewhere", branch=elsewhere,
        )

        with self.assertRaises(FinanceError):
            issue_stock(self.paper, quantity=1, movement_date=datetime.date(2026, 1, 31), location=other)

        refused = self.entries(FinanceAuditAction.STOCK_ISSUE_REJECTED).get()
        self.assertIsNone(refused.branch_id)

    def test_an_old_stock_entry_takes_its_stores_branch_from_the_backfill(self):
        old = FinanceAuditLog.objects.create(
            entity=self.multi.entity, action=FinanceAuditAction.STOCK_RECEIVED,
            target_type="StockItem", target_id=str(self.paper.pk), message="old",
            metadata={"location_id": self.lekki_store.pk},
        )

        call_command("branch_backfill", "--tenant", self.multi_tenant.slug, "--apply", stdout=io.StringIO())

        old.refresh_from_db()
        self.assertEqual(old.branch_id, self.lekki.pk)
