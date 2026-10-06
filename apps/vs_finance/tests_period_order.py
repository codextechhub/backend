"""Months close in order and reopen from the latest back, unless a school turns it off.

Lagoon View runs Ikeja and Lekki; Harbour Primary runs one branch. Walking a set
of books' months in date order, across fiscal years, a month is never more open
than a later one: September closes once August is closed, August reopens once
September is open again, and January 2027 waits for December 2026. The year's
closing period is not a month and takes no part, and the fiscal year itself may
stay open for the auditors. A forced close overrides the checklist, never the
order; only the school's ``periods_close_in_order`` setting does.

The close preview describes what the close will do. Depreciation and deferred
income falling due are work the close does itself, so the preview shows them as
done by the close rather than as blockers, while a close that is told not to do
that work is still refused by them.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from .constants import PeriodStatus
from .exceptions import PeriodCloseError
from .models import (
    Account,
    BranchFiscalPeriod,
    FinanceCalendarSettings,
    FiscalPeriod,
    FixedAsset,
)
from .seed import seed_fiscal_year
from .tests_shared_write_reach import _SharedWriteFixture

CLOSE_ORDER = "earlier_periods_closed"
REOPEN_ORDER = "later_periods_open"


class _OrderFixture(_SharedWriteFixture):
    """The shared two-branch and one-branch schools, with helpers to set month states."""

    def month(self, number, books=None, year=2026):
        return FiscalPeriod.objects.get(
            entity=books or self.harbour_books, fiscal_year__year=year, period_no=number,
        )

    def set_months(self, status, numbers, books=None, year=2026):
        FiscalPeriod.objects.filter(
            entity=books or self.harbour_books, fiscal_year__year=year,
            period_no__in=list(numbers), is_closing=False,
        ).update(status=status)

    def set_branch_months(self, branch, status, numbers, books=None):
        for number in numbers:
            BranchFiscalPeriod.objects.update_or_create(
                period=self.month(number, books or self.books), branch=branch,
                defaults={"status": status},
            )

    def order_off(self, books=None):
        FinanceCalendarSettings.objects.update_or_create(
            entity=books or self.harbour_books, defaults={"periods_close_in_order": False},
        )

    def close(self, period, **kwargs):
        from .close import close_period

        kwargs.setdefault("run_depreciation", False)
        kwargs.setdefault("release_deferred", False)
        return close_period(self.harbour_books, period, actor_user=self.tolu, **kwargs)

    def reopen(self, period):
        from .close import reopen_period

        return reopen_period(
            self.harbour_books, period, actor_user=self.tolu, reason="Correct the books.",
        )


class PeriodCloseOrderTests(_OrderFixture):
    def test_a_month_cannot_close_while_an_earlier_month_is_open(self):
        february = self.month(2)
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(february)
        self.assertEqual(
            refused.exception.message,
            "Close January 2026 first. Months close in order, so February 2026 can "
            "close once every earlier month is closed.",
        )
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])
        february.refresh_from_db()
        self.assertEqual(february.status, PeriodStatus.OPEN)

    def test_the_earliest_month_in_the_way_is_named(self):
        self.set_months(PeriodStatus.CLOSED, [1])
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(self.month(5))
        self.assertTrue(refused.exception.message.startswith("Close February 2026 first."))

    def test_a_hard_close_needs_earlier_months_closed_not_soft_closed(self):
        self.set_months(PeriodStatus.SOFT_CLOSED, [1])
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(self.month(2))
        self.assertIn("January 2026", refused.exception.message)
        self.assertIn("soft-closed", refused.exception.message)

        period, _ = self.close(self.month(2), soft=True)
        self.assertEqual(period.status, PeriodStatus.SOFT_CLOSED)

    def test_a_soft_close_needs_earlier_months_at_least_soft_closed(self):
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(self.month(2), soft=True)
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])
        self.assertIn("January 2026", refused.exception.message)

    def test_months_close_one_after_another(self):
        for number in (1, 2, 3):
            period, _ = self.close(self.month(number))
            self.assertEqual(period.status, PeriodStatus.CLOSED)

    def test_a_locked_earlier_month_counts_as_closed(self):
        self.set_months(PeriodStatus.LOCKED, [1])
        period, _ = self.close(self.month(2))
        self.assertEqual(period.status, PeriodStatus.CLOSED)

    def test_the_order_runs_across_the_year_boundary_but_the_year_may_stay_open(self):
        seed_fiscal_year(self.harbour_books, year=2027, start_month=1)
        self.set_months(PeriodStatus.CLOSED, range(1, 12))
        january = self.month(1, year=2027)
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(january)
        self.assertTrue(refused.exception.message.startswith("Close December 2026 first."))

        self.set_months(PeriodStatus.CLOSED, [12])
        period, _ = self.close(january)
        self.assertEqual(period.status, PeriodStatus.CLOSED)
        self.assertEqual(self.year(self.harbour_books).status, PeriodStatus.OPEN)

    def test_the_year_close_hard_closes_its_months_only_in_order(self):
        from .close import close_fiscal_year
        from .models import FiscalYear

        seed_fiscal_year(self.harbour_books, year=2027, start_month=1)
        self.set_months(PeriodStatus.CLOSED, range(1, 12))
        self.set_months(PeriodStatus.SOFT_CLOSED, [12])
        self.set_months(PeriodStatus.SOFT_CLOSED, range(1, 13), year=2027)
        fy2027 = FiscalYear.objects.get(entity=self.harbour_books, year=2027)
        with self.assertRaises(PeriodCloseError) as refused:
            close_fiscal_year(self.harbour_books, fy2027, actor_user=self.tolu)
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])
        self.assertTrue(refused.exception.message.startswith("Close December 2026 first."))
        self.assertEqual(self.month(1, year=2027).status, PeriodStatus.SOFT_CLOSED)

        self.set_months(PeriodStatus.CLOSED, [12])
        close_fiscal_year(self.harbour_books, fy2027, actor_user=self.tolu)
        self.assertEqual(self.month(1, year=2027).status, PeriodStatus.CLOSED)

    def test_a_forced_close_does_not_bypass_the_order(self):
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(self.month(2), force=True, reason="The auditors asked for it.")
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])

    def test_with_the_setting_off_months_close_and_reopen_in_any_order(self):
        self.order_off()
        march, _ = self.close(self.month(3))
        self.assertEqual(march.status, PeriodStatus.CLOSED)
        february, _ = self.close(self.month(2))
        self.reopen(february)
        february.refresh_from_db()
        self.assertEqual(february.status, PeriodStatus.OPEN)

    def test_the_single_branch_close_the_screen_uses_keeps_the_order(self):
        from .branch_close import close_branch_period

        with self.assertRaises(PeriodCloseError) as refused:
            close_branch_period(
                self.harbour_books, self.month(2), self.harbour_main,
                run_depreciation=False, release_deferred=False,
            )
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])
        self.assertFalse(BranchFiscalPeriod.objects.filter(
            period=self.month(2), status=PeriodStatus.CLOSED,
        ).exists())

    def test_a_month_locks_only_once_earlier_months_are_closed(self):
        from .close import lock_period

        self.set_months(PeriodStatus.CLOSED, [2])
        with self.assertRaises(PeriodCloseError) as refused:
            lock_period(self.harbour_books, self.month(2), actor_user=self.tolu)
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])

        self.set_months(PeriodStatus.CLOSED, [1])
        locked = lock_period(self.harbour_books, self.month(2), actor_user=self.tolu)
        self.assertEqual(locked.status, PeriodStatus.LOCKED)


class PeriodReopenOrderTests(_OrderFixture):
    def test_a_month_cannot_reopen_while_a_later_month_is_closed(self):
        self.set_months(PeriodStatus.CLOSED, [1, 2, 3])
        with self.assertRaises(PeriodCloseError) as refused:
            self.reopen(self.month(1))
        self.assertEqual(
            refused.exception.message,
            "Reopen March 2026 first. Months reopen from the latest back, so January "
            "2026 can reopen once every later month is open.",
        )
        self.assertEqual(refused.exception.failures, [REOPEN_ORDER])

    def test_the_latest_month_reopens_and_the_closing_period_is_not_in_the_way(self):
        self.set_months(PeriodStatus.CLOSED, [1, 2])
        self.reopen(self.month(2))
        self.reopen(self.month(1))
        self.assertEqual(self.month(1).status, PeriodStatus.OPEN)

    def test_a_locked_later_month_says_the_month_can_no_longer_reopen(self):
        self.set_months(PeriodStatus.CLOSED, [1])
        self.set_months(PeriodStatus.LOCKED, [2])
        with self.assertRaises(PeriodCloseError) as refused:
            self.reopen(self.month(1))
        self.assertTrue(refused.exception.message.startswith(
            "February 2026 is locked, so January 2026 can no longer be reopened."))


class BranchPeriodOrderTests(_OrderFixture):
    """Ikeja has closed August; Lekki has closed only up to July."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for number in range(1, 9):
            period = FiscalPeriod.objects.get(
                entity=cls.books, fiscal_year__year=2026, period_no=number,
            )
            BranchFiscalPeriod.objects.create(
                period=period, branch=cls.ikeja, status=PeriodStatus.CLOSED,
            )
            if number < 8:
                BranchFiscalPeriod.objects.create(
                    period=period, branch=cls.lekki, status=PeriodStatus.CLOSED,
                )

    def close_branch(self, branch, number, **kwargs):
        from .branch_close import close_branch_period

        return close_branch_period(
            self.books, self.month(number, self.books), branch, actor_user=self.adaeze,
            run_depreciation=False, release_deferred=False, **kwargs,
        )

    def test_lekki_cannot_close_september_before_its_own_august(self):
        with self.assertRaises(PeriodCloseError) as refused:
            self.close_branch(self.lekki, 9)
        self.assertEqual(
            refused.exception.message,
            "Close August 2026 at Lekki Branch first. Months close in order, so Lekki "
            "Branch can close September 2026 once every earlier month is closed.",
        )
        self.assertEqual(refused.exception.failures, [CLOSE_ORDER])

    def test_ikeja_closes_september_whatever_lekki_has_done(self):
        state, _ = self.close_branch(self.ikeja, 9)
        self.assertEqual(state.status, PeriodStatus.CLOSED)

    def test_a_branch_reopens_from_its_own_latest_month_back(self):
        from .branch_close import reopen_branch_period

        with self.assertRaises(PeriodCloseError) as refused:
            reopen_branch_period(
                self.books, self.month(7, self.books), self.ikeja,
                actor_user=self.adaeze, reason="Correct July.",
            )
        self.assertEqual(refused.exception.failures, [REOPEN_ORDER])
        self.assertTrue(refused.exception.message.startswith(
            "Reopen August 2026 at Ikeja Branch first."))

        state = reopen_branch_period(
            self.books, self.month(7, self.books), self.lekki,
            actor_user=self.adaeze, reason="Correct July.",
        )
        self.assertEqual(state.status, PeriodStatus.OPEN)

    def test_the_preview_reads_the_branch_it_is_for(self):
        response = self.send(
            self.ngozi, "get", f"periods/{self.month(9, self.books).pk}/checklist/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        item = next(i for i in response.data["data"]["items"] if i["name"] == CLOSE_ORDER)
        self.assertFalse(item["passed"])
        self.assertTrue(item["blocking"])
        self.assertIn("August 2026", item["detail"])
        self.assertFalse(response.data["data"]["passed"])


class PeriodOrderPreviewTests(_OrderFixture):
    def preview(self, number):
        response = self.send(
            self.tolu, "get", f"periods/{self.month(number).pk}/checklist/", self.harbour_books,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def item(self, data, name):
        return next((i for i in data["items"] if i["name"] == name), None)

    def test_the_preview_shows_the_month_in_the_way_as_a_blocker(self):
        data = self.preview(2)
        item = self.item(data, CLOSE_ORDER)
        self.assertEqual(item, {
            "name": CLOSE_ORDER, "passed": False, "blocking": True, "done_by_close": False,
            "detail": "Close January 2026 first. Months close in order, so February 2026 "
                      "can close once every earlier month is closed.",
        })
        self.assertFalse(data["passed"])
        self.assertEqual(data["done"], sum(1 for i in data["items"] if i["passed"]))

    def test_the_preview_passes_the_order_once_earlier_months_are_closed(self):
        self.set_months(PeriodStatus.CLOSED, [1])
        item = self.item(self.preview(2), CLOSE_ORDER)
        self.assertTrue(item["passed"])
        self.assertEqual(item["detail"], "Every earlier month is closed.")

    def test_the_preview_says_a_soft_closed_month_in_the_way_still_allows_a_soft_close(self):
        self.set_months(PeriodStatus.SOFT_CLOSED, [1])
        item = self.item(self.preview(2), CLOSE_ORDER)
        self.assertFalse(item["passed"])
        self.assertIn("February 2026 can be soft-closed now", item["detail"])

    def test_the_preview_leaves_the_order_out_when_the_setting_is_off(self):
        self.order_off()
        self.assertIsNone(self.item(self.preview(2), CLOSE_ORDER))

    def test_the_close_endpoint_refuses_out_of_order_with_a_409(self):
        response = self.send(
            self.tolu, "post", f"periods/{self.month(2).pk}/close/", self.harbour_books,
            {"run_depreciation": False, "release_deferred": False},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Close January 2026 first.", response.data["message"])


class DoneByClosePreviewTests(_OrderFixture):
    """Harbour bought a generator on 1 January; its first charge falls due in February.

    January is closed, so February is next in order.
    """

    @classmethod
    def setUpTestData(cls):
        from .assets import acquire_asset

        super().setUpTestData()
        cls.generator = FixedAsset.objects.create(
            entity=cls.harbour_books, branch=cls.harbour_main, name="Harbour Generator",
            acquisition_date=datetime.date(2026, 1, 1), cost=1_200_000,
            salvage_value=0, useful_life_months=12,
        )
        acquire_asset(
            cls.generator,
            credit_account=Account.objects.get(entity=cls.harbour_books, code="3100"),
        )
        FiscalPeriod.objects.filter(
            entity=cls.harbour_books, fiscal_year__year=2026, period_no=1,
        ).update(status=PeriodStatus.CLOSED)

    def test_due_depreciation_is_shown_as_done_by_the_close(self):
        from .close import close_checklist

        checklist = close_checklist(self.harbour_books, self.month(2), preview=True)
        item = next(i for i in checklist.items if i.name == "depreciation_posted")
        self.assertTrue(item.passed)
        self.assertTrue(item.done_by_close)
        self.assertEqual(
            item.detail, "1 depreciation charge is due; closing the period posts it.",
        )
        self.assertTrue(checklist.passed)

    def test_the_preview_endpoint_serializes_done_by_close(self):
        response = self.send(
            self.tolu, "get", f"periods/{self.month(2).pk}/checklist/", self.harbour_books,
        )
        self.assertEqual(response.status_code, 200, response.data)
        item = next(
            i for i in response.data["data"]["items"] if i["name"] == "depreciation_posted"
        )
        self.assertTrue(item["passed"])
        self.assertTrue(item["done_by_close"])
        self.assertTrue(response.data["data"]["passed"])

    def test_the_dashboard_counts_it_as_done_by_the_close(self):
        from .dashboard import _close_progress

        progress = _close_progress(self.harbour_books, self.month(2))
        check = next(c for c in progress["checks"] if c["name"] == "depreciation_posted")
        self.assertTrue(check["passed"])
        self.assertTrue(check["done_by_close"])
        self.assertEqual(progress["done"], progress["total"])

    def test_the_close_posts_it_and_passes(self):
        period, checklist = self.close(self.month(2), run_depreciation=True)
        self.assertEqual(period.status, PeriodStatus.CLOSED)
        item = next(i for i in checklist.items if i.name == "depreciation_posted")
        self.assertTrue(item.passed)
        self.assertFalse(item.done_by_close)

    def test_a_close_told_not_to_post_it_is_still_blocked(self):
        with self.assertRaises(PeriodCloseError) as refused:
            self.close(self.month(2), run_depreciation=False)
        self.assertIn("depreciation_posted", refused.exception.failures)
