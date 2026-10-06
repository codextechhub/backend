"""Under All branches, "Earlier months closed" answers branch by branch.

Lagoon View has closed August at Ikeja but not at Lekki, so the school's August
is still open. Mrs Adaeze looks at September's close checklist under All
branches. The check names Lekki's August as the month in Lekki's way and says
Ikeja can close September now; it is a warning, not a blocker, so the screen
still offers what Ikeja's own close offers, a forced close included. It blocks
only when no branch still to close September can close it.
"""
from __future__ import annotations

from .constants import PeriodStatus
from .models import BranchFiscalPeriod, FiscalPeriod
from .tests_period_order import CLOSE_ORDER, _OrderFixture


class WholeSchoolCloseOrderTests(_OrderFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for number in range(1, 9):
            period = FiscalPeriod.objects.get(
                entity=cls.books, fiscal_year__year=2026, period_no=number)
            BranchFiscalPeriod.objects.create(
                period=period, branch=cls.ikeja, status=PeriodStatus.CLOSED)
            if number < 8:
                BranchFiscalPeriod.objects.create(
                    period=period, branch=cls.lekki, status=PeriodStatus.CLOSED)

    def item(self, number=9):
        response = self.send(
            self.adaeze, "get", f"periods/{self.month(number, self.books).pk}/checklist/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["branch"])
        return next(i for i in response.data["data"]["items"] if i["name"] == CLOSE_ORDER)

    def test_it_names_the_branch_in_the_way_and_does_not_block_the_one_that_can_close(self):
        item = self.item()

        self.assertFalse(item["passed"])
        self.assertFalse(item["blocking"])
        self.assertEqual(
            item["detail"],
            "Lekki Branch has not closed August 2026 yet, so Lekki Branch can close "
            "September 2026 only after it. Ikeja Branch can close September 2026 now.",
        )

    def test_ikeja_still_closes_september_from_the_whole_school_view(self):
        response = self.send(
            self.adaeze, "post", f"periods/{self.month(9, self.books).pk}/close/",
            body={"branch": self.ikeja.pk, "run_depreciation": False, "release_deferred": False},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(BranchFiscalPeriod.objects.get(
            period=self.month(9, self.books), branch=self.ikeja).status, PeriodStatus.CLOSED)

    def test_it_blocks_once_no_branch_left_to_close_can_close(self):
        self.set_branch_months(self.ikeja, PeriodStatus.CLOSED, [9])

        item = self.item()

        self.assertFalse(item["passed"])
        self.assertTrue(item["blocking"])
        self.assertIn("so no branch can close September 2026 yet", item["detail"])

    def test_it_blocks_when_every_branch_has_an_earlier_month_open(self):
        self.set_branch_months(self.ikeja, PeriodStatus.OPEN, [8])

        item = self.item()

        self.assertTrue(item["blocking"])
        self.assertIn("Ikeja Branch has not closed August 2026 yet", item["detail"])
        self.assertIn("Lekki Branch has not closed August 2026 yet", item["detail"])

    def test_it_passes_once_every_branch_has_closed_the_earlier_months(self):
        self.set_branch_months(self.lekki, PeriodStatus.CLOSED, [8])

        item = self.item()

        self.assertTrue(item["passed"])
        self.assertEqual(item["detail"], "Every earlier month is closed at every branch.")
