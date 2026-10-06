"""A branch close blocks its own postings while another branch keeps working."""

from types import SimpleNamespace
from unittest.mock import patch

from .constants import PeriodStatus
from .exceptions import PeriodClosedError
from .models import (
    Account, BranchFiscalPeriod, BranchFiscalYear, FiscalYear, FixedAsset, LedgerSeal,
)
from .posting import ensure_period_open
from .tests_shared_write_reach import _SharedWriteFixture
from core.test_utils import TenantAPIClient


class BranchPostingGuardTests(_SharedWriteFixture):
    def test_closed_branch_refuses_while_another_branch_remains_open(self):
        period = self.period()
        BranchFiscalPeriod.objects.create(
            period=period, branch=self.ikeja, status=PeriodStatus.CLOSED,
        )
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(period, branch=self.ikeja)
        ensure_period_open(period, branch=self.lekki)
        self.assertEqual(period.status, PeriodStatus.OPEN)

    def test_a_single_branch_close_refuses_its_postings(self):
        period = self.period(self.harbour_books)
        BranchFiscalPeriod.objects.create(
            period=period, branch=self.harbour_main, status=PeriodStatus.SOFT_CLOSED,
        )
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(period, branch=self.harbour_main)
        ensure_period_open(period, branch=self.harbour_main, allow_restricted=True)

    def test_inter_branch_transfer_refuses_when_either_branch_is_closed(self):
        from .inter_branch import ensure_branches_open

        period = self.period()
        BranchFiscalPeriod.objects.create(
            period=period, branch=self.lekki, status=PeriodStatus.CLOSED,
        )
        with self.assertRaises(PeriodClosedError):
            ensure_branches_open(self.books, period.start_date, self.ikeja, self.lekki)


class BranchPeriodCloseTests(_SharedWriteFixture):
    def test_closing_one_branch_keeps_the_other_and_tenant_open(self):
        from .branch_close import close_branch_period

        period = self.period()
        state, checklist = close_branch_period(
            self.books, period, self.ikeja, actor_user=self.adaeze,
            run_depreciation=False, release_deferred=False,
        )
        self.assertTrue(checklist.passed)
        self.assertEqual(state.status, PeriodStatus.CLOSED)
        period.refresh_from_db()
        self.assertEqual(period.status, PeriodStatus.OPEN)
        with self.assertRaises(PeriodClosedError):
            ensure_period_open(period, branch=self.ikeja)
        ensure_period_open(period, branch=self.lekki)

    def test_final_branch_closes_tenant_period(self):
        from .branch_close import close_branch_period

        period = self.period()
        close_branch_period(
            self.books, period, self.ikeja, actor_user=self.adaeze,
            run_depreciation=False, release_deferred=False,
        )
        close_branch_period(
            self.books, period, self.lekki, actor_user=self.adaeze,
            run_depreciation=False, release_deferred=False,
        )
        period.refresh_from_db()
        self.assertEqual(period.status, PeriodStatus.CLOSED)

    def test_a_foreign_branch_cannot_close_these_books(self):
        from .branch_close import close_branch_period
        from .exceptions import PeriodCloseError

        with self.assertRaises(PeriodCloseError):
            close_branch_period(
                self.books, self.period(), self.harbour_main,
                run_depreciation=False, release_deferred=False,
            )

    def test_reopen_one_branch_does_not_reopen_another(self):
        from .branch_close import close_branch_period, reopen_branch_period

        period = self.period()
        for branch in (self.ikeja, self.lekki):
            close_branch_period(
                self.books, period, branch, actor_user=self.adaeze,
                run_depreciation=False, release_deferred=False,
            )
        reopened = reopen_branch_period(
            self.books, period, self.ikeja, actor_user=self.adaeze,
            reason="Correct the January books.",
        )
        self.assertEqual(reopened.status, PeriodStatus.OPEN)
        self.assertEqual(BranchFiscalPeriod.objects.get(
            period=period, branch=self.lekki,
        ).status, PeriodStatus.CLOSED)
        period.refresh_from_db()
        self.assertEqual(period.status, PeriodStatus.OPEN)

    def test_locked_branch_cannot_reopen(self):
        from .branch_close import lock_branch_period, reopen_branch_period
        from .exceptions import PeriodCloseError

        period = self.period()
        BranchFiscalPeriod.objects.create(
            period=period, branch=self.ikeja, status=PeriodStatus.CLOSED,
        )
        locked = lock_branch_period(self.books, period, self.ikeja, actor_user=self.adaeze)
        self.assertEqual(locked.status, PeriodStatus.LOCKED)
        with self.assertRaises(PeriodCloseError):
            reopen_branch_period(
                self.books, period, self.ikeja, reason="Correct January.",
            )

    def test_procurement_checks_reconcile_only_this_branch(self):
        from .close import close_checklist

        reconciliation = SimpleNamespace(
            is_reconciled=True, subledger_total=0, control_total=0,
        )
        with patch("vs_procurement.models.Vendor.objects.filter") as vendors, \
                patch("vs_procurement.reports.reconcile_ap", return_value=reconciliation) as ap, \
                patch("vs_procurement.reports.grir_balance", return_value=0) as grir:
            vendors.return_value.exists.return_value = True
            checklist = close_checklist(self.books, self.period(), branch=self.ikeja)
        self.assertIn("ap_reconciled", [item.name for item in checklist.items])
        self.assertIn("grir_explained", [item.name for item in checklist.items])
        self.assertEqual(ap.call_args.kwargs["branch_scope"].branch_ids, frozenset({self.ikeja.pk}))
        self.assertEqual(grir.call_args.kwargs["branch_scope"].branch_ids, frozenset({self.ikeja.pk}))


class BranchCloseAPITests(_SharedWriteFixture):
    def test_branch_bursar_closes_only_the_branch_she_reaches(self):
        response = self.send(
            self.ngozi, "post", f"periods/{self.period().pk}/close/",
            body={"branch": self.lekki.pk, "run_depreciation": False,
                  "release_deferred": False},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["branch_period"]["branch"], self.lekki.pk)
        self.assertEqual(response.data["data"]["branch_period"]["status"], PeriodStatus.CLOSED)
        self.assertEqual(self.period().status, PeriodStatus.OPEN)

    def test_branch_bursar_cannot_close_another_branch(self):
        response = self.send(
            self.ngozi, "post", f"periods/{self.period().pk}/close/",
            body={"branch": self.ikeja.pk, "run_depreciation": False,
                  "release_deferred": False},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(BranchFiscalPeriod.objects.filter(
            period=self.period(), branch=self.ikeja,
        ).exists())

    def test_multi_branch_write_requires_a_selected_branch(self):
        response = self.send(
            self.adaeze, "post", f"periods/{self.period().pk}/close/",
            body={"run_depreciation": False, "release_deferred": False},
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("branch", str(response.data))

    def test_period_list_and_posting_window_use_the_reached_branch_state(self):
        from .posting import posting_window

        period = self.period()
        BranchFiscalPeriod.objects.create(
            period=period, branch=self.lekki, status=PeriodStatus.CLOSED,
        )
        listed = TenantAPIClient(user=self.ngozi).get(
            f"/v1/finance/periods/?entity={self.books.code}&all=true&year=2026",
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        january = next(row for row in listed.data["data"] if row["id"] == period.pk)
        self.assertEqual(january["status"], PeriodStatus.CLOSED)
        window = posting_window(self.books, today=period.start_date, branch=self.lekki)
        self.assertFalse(window["today_is_open"])

    def test_one_branch_api_infers_the_branch_and_keeps_the_compact_response(self):
        response = self.send(
            self.tolu, "post", f"periods/{self.period(self.harbour_books).pk}/close/",
            self.harbour_books,
            {"run_depreciation": False, "release_deferred": False},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("branch_period", response.data["data"])


class CalendarWholeSchoolRuleTests(_SharedWriteFixture):
    """Which calendar writes a branch bursar may make, and which need the whole school.

    Ngozi keeps only Lekki's books. She closes and reopens Lekki's months herself,
    which is the point of a per-branch close. Opening a new fiscal year changes
    every branch's calendar, and reopening a closed year moves a whole year's result
    back out of Retained Earnings, so both need somebody who reaches every branch.
    Naming a branch in the body never turns a whole-school write into a branch one.
    """

    def test_naming_a_branch_does_not_let_a_branch_bursar_open_a_year(self):
        response = self.send(
            self.ngozi, "post", "fiscal-years/",
            body={"year": 2027, "branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(FiscalYear.objects.filter(entity=self.books, year=2027).exists())

    def test_a_branch_bursar_cannot_reopen_her_own_branch_year(self):
        from .close import close_fiscal_year

        fiscal_year = self.year()
        close_fiscal_year(
            self.books, fiscal_year, actor_user=self.adaeze, branch=self.lekki,
            require_periods_closed=False, reason="Close Lekki for the audit.",
        )

        response = self.send(
            self.ngozi, "post", f"fiscal-years/{fiscal_year.pk}/reopen/",
            body={"branch": self.lekki.pk, "reason": "Fix a December entry."},
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(BranchFiscalYear.objects.get(
            fiscal_year=fiscal_year, branch=self.lekki,
        ).status, PeriodStatus.CLOSED)

    def test_a_whole_school_bursar_reopens_one_branch_year(self):
        from .close import close_fiscal_year

        fiscal_year = self.year()
        close_fiscal_year(
            self.books, fiscal_year, actor_user=self.adaeze, branch=self.lekki,
            require_periods_closed=False, reason="Close Lekki for the audit.",
        )

        response = self.send(
            self.adaeze, "post", f"fiscal-years/{fiscal_year.pk}/reopen/",
            body={"branch": self.lekki.pk, "reason": "Fix a December entry."},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(BranchFiscalYear.objects.get(
            fiscal_year=fiscal_year, branch=self.lekki,
        ).status, PeriodStatus.OPEN)


class BranchFiscalYearCloseTests(_SharedWriteFixture):
    def test_year_closes_only_after_every_branch_year_closes(self):
        from .close import close_fiscal_year

        fiscal_year = self.year()
        close_fiscal_year(
            self.books, fiscal_year, actor_user=self.adaeze, branch=self.ikeja,
            require_periods_closed=False, reason="Close Ikeja for the audit.",
        )
        fiscal_year.refresh_from_db()
        self.assertEqual(fiscal_year.status, PeriodStatus.OPEN)
        self.assertEqual(BranchFiscalYear.objects.get(
            fiscal_year=fiscal_year, branch=self.ikeja,
        ).status, PeriodStatus.CLOSED)
        self.assertFalse(LedgerSeal.objects.filter(
            fiscal_year=fiscal_year, kind=LedgerSeal.Kind.YEAR_CLOSED,
        ).exists())
        close_fiscal_year(
            self.books, fiscal_year, actor_user=self.adaeze, branch=self.lekki,
            require_periods_closed=False, reason="Close Lekki for the audit.",
        )
        fiscal_year.refresh_from_db()
        self.assertEqual(fiscal_year.status, PeriodStatus.CLOSED)
        self.assertEqual(LedgerSeal.objects.filter(
            fiscal_year=fiscal_year, kind=LedgerSeal.Kind.YEAR_CLOSED,
        ).count(), 1)

    def test_reopening_one_branch_year_leaves_other_branch_closed(self):
        from .close import close_fiscal_year, reopen_fiscal_year

        fiscal_year = self.year()
        for branch in (self.ikeja, self.lekki):
            close_fiscal_year(
                self.books, fiscal_year, actor_user=self.adaeze, branch=branch,
                require_periods_closed=False,
                reason=f"Close {branch.name} for the audit.",
            )

        reopen_fiscal_year(
            self.books, fiscal_year, actor_user=self.adaeze, branch=self.ikeja,
            reason="Correct Ikeja before signing the audit.",
        )

        fiscal_year.refresh_from_db()
        self.assertEqual(fiscal_year.status, PeriodStatus.OPEN)
        statuses = dict(BranchFiscalYear.objects.filter(
            fiscal_year=fiscal_year,
        ).values_list("branch_id", "status"))
        self.assertEqual(statuses[self.ikeja.pk], PeriodStatus.OPEN)
        self.assertEqual(statuses[self.lekki.pk], PeriodStatus.CLOSED)

    def test_year_checklist_reads_only_the_branch_assets(self):
        from .assets import acquire_asset
        from .close import year_close_checklist

        asset = FixedAsset.objects.create(
            entity=self.books, branch=self.lekki, name="Lekki Generator",
            acquisition_date=self.period().start_date, cost=120000,
            salvage_value=0, useful_life_months=12,
        )
        acquire_asset(asset, credit_account=Account.objects.get(entity=self.books, code="3100"))
        ikeja = year_close_checklist(self.books, self.year(), branch=self.ikeja)
        lekki = year_close_checklist(self.books, self.year(), branch=self.lekki)
        self.assertNotIn("depreciation_posted_for_year", [item.name for item in ikeja.failures])
        self.assertIn("depreciation_posted_for_year", [item.name for item in lekki.failures])

    def test_closed_branch_year_allows_its_final_period_to_lock(self):
        from .branch_close import lock_branch_period

        fiscal_year = self.year()
        final = self.period(month=12)
        BranchFiscalYear.objects.create(
            fiscal_year=fiscal_year, branch=self.ikeja, status=PeriodStatus.CLOSED,
        )
        BranchFiscalYear.objects.create(
            fiscal_year=fiscal_year, branch=self.lekki, status=PeriodStatus.OPEN,
        )
        # Ikeja has closed every month of the year, so the order lets December lock.
        for month in range(1, 13):
            BranchFiscalPeriod.objects.create(
                period=self.period(month=month), branch=self.ikeja, status=PeriodStatus.CLOSED,
            )
        state = lock_branch_period(self.books, final, self.ikeja, actor_user=self.adaeze)
        self.assertEqual(state.status, PeriodStatus.LOCKED)
        fiscal_year.refresh_from_db()
        self.assertEqual(fiscal_year.status, PeriodStatus.OPEN)
