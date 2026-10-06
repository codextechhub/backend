"""A closed fiscal year stays closed until somebody reopens it, on the record.

Lagoon View runs Ikeja and Lekki. In 2026 Ikeja earns N1,000 of fees; Lekki earns
N700 and spends N400. The year close works each branch's result out on its own
and closes them together: Ikeja's closing journal moves N1,000 into Retained
Earnings, Lekki's moves N300, and the year reads CLOSED.

After that nothing posts into 2026, whatever a month says: not an ordinary
entry, not a privileged depreciation run into a month left soft-closed, not a
raw reversal of a closing journal from the journal screen. A January that is
CLOSED cannot be reopened while its year is. The only way back in is to reopen
the year itself: Adaeze, the whole-school bursar, gives her reason, both
closing journals are reversed on 31 December inside 2026, the year reads OPEN,
and she can correct January and close the year again. Ngozi, Lekki's bursar,
holds the same key and is refused, because a year binds every branch.

Forcing a close over its checks and reopening a period each need a reason, and
forcing needs ``finance.period.force_close`` rather than the ordinary close key.

Harbour Primary has one branch, Main. Its entries written before branches were
recorded carry none, and its year closes them onto Main. At Lagoon View an
income entry with no branch belongs to neither branch's result, so the close is
refused until it is given one. A tenant that owns no branch at all is a broken
invariant, so the close raises rather than write a closing journal with none.

The close also refuses while a depreciation charge dated in the year is unposted,
because nothing can post it afterwards, and a posting in flight holds a year or
month close back until it commits.
"""
from __future__ import annotations

import datetime
import threading

from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, tag
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .branch_ledger import branches_in_year, ledger_lines
from .close import (
    close_fiscal_year,
    close_period,
    reopen_fiscal_year,
    reopen_period,
    run_period_depreciation,
)
from .constants import (
    AccountType,
    DocumentStatus,
    FinanceAuditAction,
    PeriodStatus,
)
from .exceptions import BranchlessTenantError, PeriodCloseError, PeriodClosedError, PostingError
from .models import (
    Account,
    BranchFiscalPeriod,
    BranchFiscalYear,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    FixedAsset,
    JournalEntry,
    JournalLine,
    LedgerEntity,
)
from .posting import (
    journal_reversal_action,
    post_journal,
    posting_window,
    resolve_period,
    reverse_journal,
)
from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

REFUSED = "SHARED_RECORD_READ_ONLY"
CALENDAR_MESSAGE = "Only a school-wide administrator can change the fiscal periods and years."
REASON = "Auditors found a January fee receipt booked to the wrong account."
PL_TYPES = (AccountType.INCOME, AccountType.EXPENSE)

BURSAR_KEYS = (
    "finance.period.view", "finance.period.close", "finance.period.reopen",
    "finance.period.force_close", "finance.fiscalyear.reopen",
    "finance.journal.view", "finance.journal.reverse", "finance.report.view",
)
CLOSER_KEYS = (
    "finance.period.view", "finance.period.close", "finance.period.reopen",
    "finance.journal.view", "finance.journal.reverse",
)
FORCER_KEYS = ("finance.period.view", "finance.period.force_close")


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    seed_fiscal_year(entity, year=2026, start_month=1)
    return entity


def _role(school, name, keys):
    role = make_role(school, name=name)
    for key in keys:
        make_role_permission(role, make_permission(key))
    return role


class _YearFixture(TestCase):
    """Lagoon View (Ikeja, Lekki) and Harbour Primary (Main only), each with 2026 books."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()

        cls.lagoon = make_school(slug="lagoon-year-guard", name="Lagoon View")
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGYRG", cls.lagoon.tenant)

        bursar = _role(cls.lagoon, "Bursar", BURSAR_KEYS)
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-year.example.com")
        make_assignment(cls.lagoon, cls.adaeze, bursar, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-year.example.com")
        make_assignment(cls.lagoon, cls.ngozi, bursar, branch=cls.lekki)
        cls.kemi = make_school_admin(cls.ikeja, email="kemi@lagoon-year.example.com")
        make_assignment(cls.lagoon, cls.kemi, _role(cls.lagoon, "Closer", CLOSER_KEYS), branch=None)
        cls.femi = make_school_admin(cls.ikeja, email="femi@lagoon-year.example.com")
        make_assignment(cls.lagoon, cls.femi, _role(cls.lagoon, "Forcer", FORCER_KEYS), branch=None)

        cls.harbour = make_school(slug="harbour-year-guard", name="Harbour Primary")
        cls.harbour_main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_books = _books("HBRYRG", cls.harbour.tenant)

    # -- rows ----------------------------------------------------------------- #

    def year(self, books=None):
        return FiscalYear.objects.get(entity=books or self.books, year=2026)

    def month(self, number, books=None):
        return FiscalPeriod.objects.get(
            entity=books or self.books, fiscal_year__year=2026, period_no=number,
        )

    def entry(self, branch, date, pairs, books=None):
        """A draft journal on ``date`` for ``branch``; pairs are (code, debit, credit)."""
        books = books or self.books
        entry = JournalEntry.objects.create(
            entity=books, branch=branch, date=date,
            period=resolve_period(books, date), narration="test",
        )
        for i, (code, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=books, code=code),
                debit=debit, credit=credit, line_no=i,
            )
        return entry

    def post(self, branch, date, pairs, books=None):
        entry = self.entry(branch, date, pairs, books)
        post_journal(entry)
        return entry

    def asset(self, acquired, *, months, name="Generator", cost=1200000):
        """An acquired asset at Ikeja, paid from capital, depreciating monthly."""
        from .assets import acquire_asset

        asset = FixedAsset.objects.create(
            entity=self.books, branch=self.ikeja, name=name, acquisition_date=acquired,
            cost=cost, salvage_value=0, useful_life_months=months,
        )
        acquire_asset(asset, credit_account=Account.objects.get(entity=self.books, code="3100"))
        asset.refresh_from_db()
        return asset

    def trade(self):
        """Ikeja earns N1,000; Lekki earns N700 and spends N400 (amounts in kobo)."""
        self.post(self.ikeja, datetime.date(2026, 1, 15), [("1100", 100000, 0), ("4100", 0, 100000)])
        self.post(self.lekki, datetime.date(2026, 3, 10), [("1100", 70000, 0), ("4100", 0, 70000)])
        self.post(self.lekki, datetime.date(2026, 3, 20), [("5200", 40000, 0), ("1100", 0, 40000)])

    def seal_months(self, status=PeriodStatus.CLOSED, books=None):
        FiscalPeriod.objects.filter(
            fiscal_year=self.year(books), is_closing=False,
        ).update(status=status)

    def closing_period(self, books=None):
        return FiscalPeriod.objects.get(fiscal_year=self.year(books), is_closing=True)

    def close_year(self, books=None):
        self.seal_months(books=books)
        return close_fiscal_year(books or self.books, self.year(books))

    def force_close_year(self, books=None):
        return close_fiscal_year(
            books or self.books, self.year(books),
            require_periods_closed=False, reason="Year sealed for the audit.",
        )

    def retained(self, books=None, branch=None):
        """Credit minus debit on Retained Earnings (3200), optionally for one branch."""
        lines = ledger_lines(books or self.books).filter(account__code="3200")
        if branch is not None:
            lines = lines.filter(entry__branch=branch)
        sums = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        return int(sums["c"] or 0) - int(sums["d"] or 0)

    def pl_movement(self, branch, books=None):
        """Each income/expense account's net for ``branch`` over 2026, non-zero only."""
        rows = (
            ledger_lines(books or self.books)
            .filter(entry__branch=branch, entry__period__fiscal_year__year=2026,
                    account__account_type__in=PL_TYPES)
            .values("account__code").annotate(d=Sum("debit"), c=Sum("credit"))
        )
        return {r["account__code"]: r["d"] - r["c"] for r in rows if r["d"] != r["c"]}

    # -- calling -------------------------------------------------------------- #

    def send(self, user, path, body=None, books=None):
        books = books or self.books
        body = dict(body or {})
        if any(action in path for action in ("/close/", "/reopen/", "/lock/")):
            body.setdefault(
                "branch", self.ikeja.pk if books == self.books else self.harbour_main.pk,
            )
        return TenantAPIClient(user=user).post(
            f"/v1/finance/{path}?entity={books.code}", body, format="json",
        )


class PostingIntoAClosedYearTests(_YearFixture):
    """Nothing posts into a closed year, whatever its months say."""

    def test_an_ordinary_posting_into_a_closed_year_is_refused(self):
        self.trade()
        self.close_year()
        FiscalPeriod.objects.filter(pk=self.month(6).pk).update(status=PeriodStatus.OPEN)

        with self.assertRaises(PeriodClosedError) as caught:
            self.post(self.ikeja, datetime.date(2026, 6, 5), [("1100", 5000, 0), ("4100", 0, 5000)])
        self.assertIn("FY2026", caught.exception.message)
        self.assertIn("Reopen the year first", caught.exception.message)

    def test_a_forced_close_leaves_open_months_that_still_refuse(self):
        self.trade()
        self.force_close_year()
        self.assertEqual(self.month(6).status, PeriodStatus.OPEN)

        with self.assertRaises(PeriodClosedError):
            self.post(self.ikeja, datetime.date(2026, 6, 5), [("1100", 5000, 0), ("4100", 0, 5000)])

    def test_privileged_flags_do_not_reach_a_closed_year(self):
        self.trade()
        self.close_year()
        FiscalPeriod.objects.filter(pk=self.month(6).pk).update(status=PeriodStatus.SOFT_CLOSED)

        for flags in ({"allow_restricted": True}, {"allow_restricted": True, "allow_closed": True}):
            with self.subTest(flags=flags):
                entry = self.entry(
                    self.ikeja, datetime.date(2026, 6, 5), [("5200", 5000, 0), ("1100", 0, 5000)],
                )
                with self.assertRaises(PeriodClosedError):
                    post_journal(entry, **flags)
                entry.refresh_from_db()
                self.assertEqual(entry.status, DocumentStatus.DRAFT)

    def test_a_depreciation_run_posts_nothing_into_a_closed_year(self):
        from .assets import post_depreciation

        asset = self.asset(datetime.date(2026, 1, 1), months=12)
        self.force_close_year()
        FiscalPeriod.objects.filter(pk=self.month(6).pk).update(status=PeriodStatus.SOFT_CLOSED)

        posted = post_depreciation(
            asset, up_to_date=datetime.date(2026, 6, 30), allow_restricted=True,
        )
        self.assertEqual(list(posted), [])
        self.assertEqual(len(posted.skipped), 5)
        self.assertEqual(run_period_depreciation(self.books, self.month(6)), 0)
        self.assertFalse(asset.schedule.filter(is_posted=True).exists())

    def test_the_posting_window_offers_no_day_of_a_closed_year(self):
        self.force_close_year()
        window = posting_window(self.books, today=datetime.date(2026, 6, 15))
        self.assertEqual(window["open"], [])
        self.assertIsNone(window["default_date"])

    def test_a_month_of_a_closed_year_cannot_be_reopened(self):
        self.trade()
        self.close_year()

        with self.assertRaises(PeriodCloseError) as caught:
            reopen_period(self.books, self.month(1), reason=REASON)
        self.assertIn("Reopen the fiscal year first", caught.exception.message)
        self.assertEqual(self.month(1).status, PeriodStatus.CLOSED)

    def test_a_month_of_a_locked_year_cannot_be_reopened(self):
        self.close_year()
        FiscalYear.objects.filter(pk=self.year().pk).update(status=PeriodStatus.LOCKED)

        with self.assertRaises(PeriodCloseError) as caught:
            reopen_period(self.books, self.month(1), reason=REASON)
        self.assertIn("locked", caught.exception.message)

    def test_the_year_close_hard_closes_every_soft_closed_month(self):
        self.trade()
        self.seal_months(PeriodStatus.SOFT_CLOSED)
        close_fiscal_year(self.books, self.year())

        statuses = set(
            FiscalPeriod.objects.filter(fiscal_year=self.year()).values_list("status", flat=True)
        )
        self.assertEqual(statuses, {PeriodStatus.CLOSED})
        self.assertEqual(FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.PERIOD_CLOSED,
            metadata__fiscal_year=2026,
        ).count(), 12)


class ClosingJournalOwnershipTests(_YearFixture):
    """A closing journal belongs to its year; the journal screen cannot reverse it."""

    def test_the_closing_journal_offers_no_raw_reverse(self):
        self.trade()
        journals, _ = self.close_year()

        for journal in journals:
            with self.subTest(branch=journal.branch.name):
                self.assertEqual(journal_reversal_action(journal), {
                    "kind": "SOURCE_DOCUMENT_ACTION",
                    "document_type": "FiscalYear",
                    "document_number": "FY2026",
                })
                with self.assertRaises(PostingError) as caught:
                    reverse_journal(journal)
                self.assertIn("Reopen fiscal year FY2026", caught.exception.message)
                journal.refresh_from_db()
                self.assertEqual(journal.status, DocumentStatus.POSTED)

    def test_the_reverse_endpoint_refuses_the_closing_journal(self):
        self.trade()
        journals, _ = self.close_year()

        response = self.send(self.adaeze, f"journals/{journals[0].pk}/reverse/")

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Reopen fiscal year FY2026", str(response.data))
        journals[0].refresh_from_db()
        self.assertEqual(journals[0].status, DocumentStatus.POSTED)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)
        self.assertEqual(self.retained(), 130000)


class YearCloseByBranchTests(_YearFixture):
    """Each branch's result is closed on its own journal, all in one act."""

    def test_two_branches_get_one_closing_journal_each(self):
        self.trade()
        journals, net = self.close_year()

        self.assertEqual(net, 130000)
        self.assertEqual(
            sorted(j.branch_id for j in journals), sorted([self.ikeja.pk, self.lekki.pk]),
        )
        for journal in journals:
            self.assertEqual(journal.closes_fiscal_year_id, self.year().pk)
            self.assertEqual(journal.status, DocumentStatus.POSTED)
            self.assertEqual(journal.date, datetime.date(2026, 12, 31))
        for branch in (self.ikeja, self.lekki):
            with self.subTest(branch=branch.name):
                self.assertEqual(self.pl_movement(branch), {})
        self.assertEqual(self.retained(branch=self.ikeja), 100000)
        self.assertEqual(self.retained(branch=self.lekki), 30000)

    def test_a_one_branch_tenant_closes_unbranched_entries_onto_its_branch(self):
        books = self.harbour_books
        self.post(None, datetime.date(2026, 2, 3), [("1100", 50000, 0), ("4100", 0, 50000)], books)
        self.post(self.harbour_main, datetime.date(2026, 4, 3),
                  [("1100", 20000, 0), ("4100", 0, 20000)], books)

        journals, net = self.close_year(books)

        self.assertEqual(net, 70000)
        self.assertEqual([j.branch_id for j in journals], [self.harbour_main.pk])
        self.assertEqual(self.retained(books, branch=self.harbour_main), 70000)
        closed_revenue = journals[0].lines.get(account__code="4100").debit
        self.assertEqual(closed_revenue, 70000)

    def test_a_multi_branch_tenant_refuses_unbranched_income(self):
        self.trade()
        self.post(None, datetime.date(2026, 5, 5), [("1100", 9000, 0), ("4100", 0, 9000)])
        self.seal_months(PeriodStatus.SOFT_CLOSED)

        with self.assertRaises(PeriodCloseError) as caught:
            close_fiscal_year(self.books, self.year())

        self.assertIn("1 journal entry carries income or expense with no branch",
                      caught.exception.message)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)
        self.assertFalse(JournalEntry.objects.filter(closes_fiscal_year=self.year()).exists())
        self.assertEqual(self.month(1).status, PeriodStatus.SOFT_CLOSED)

    def test_a_tenant_with_no_branch_is_a_fault_not_an_unbranched_close(self):
        bare = make_school(slug="bare-year-guard", name="Bare Academy")
        books = _books("BAREYG", bare.tenant)
        self.seal_months(books=books)

        with self.assertRaises(BranchlessTenantError):
            close_fiscal_year(books, self.year(books))

        self.assertEqual(self.year(books).status, PeriodStatus.OPEN)
        self.assertFalse(JournalEntry.objects.filter(closes_fiscal_year=self.year(books)).exists())

    def test_unbranched_balance_sheet_entries_do_not_block_the_close(self):
        self.trade()
        self.post(None, datetime.date(2026, 2, 1), [("1100", 500000, 0), ("3100", 0, 500000)])

        journals, net = self.close_year()

        self.assertEqual(len(journals), 2)
        self.assertEqual(net, 130000)

    def test_branches_in_year_names_the_branches_and_counts_the_rest(self):
        self.trade()
        self.post(None, datetime.date(2026, 2, 1), [("1100", 500000, 0), ("3100", 0, 500000)])

        every = branches_in_year(self.books, self.year())
        self.assertEqual(every.branch_ids, tuple(sorted([self.ikeja.pk, self.lekki.pk])))
        self.assertEqual(every.unbranched_entries, 1)
        self.assertTrue(every.has_unbranched)

        pl_only = branches_in_year(self.books, self.year(), account_types=PL_TYPES)
        self.assertEqual(pl_only.unbranched_entries, 0)
        self.assertFalse(pl_only.has_unbranched)


class ReopenFiscalYearTests(_YearFixture):
    """Reopening a year reverses its close inside the year, on the record."""

    def test_reopen_reverses_every_closing_journal_inside_the_year(self):
        self.trade()
        journals, _ = self.close_year()

        year, reversals = reopen_fiscal_year(
            self.books, self.year(), actor_user=self.adaeze, reason=REASON,
        )

        self.assertEqual(year.status, PeriodStatus.OPEN)
        self.assertEqual(len(reversals), 2)
        closing = self.closing_period()
        for reversal in reversals:
            self.assertEqual(reversal.date, datetime.date(2026, 12, 31))
            self.assertEqual(reversal.period_id, closing.pk)
            self.assertEqual(reversal.status, DocumentStatus.POSTED)
        for journal in journals:
            journal.refresh_from_db()
            self.assertEqual(journal.status, DocumentStatus.REVERSED)
        self.assertEqual(self.retained(), 0)
        self.assertEqual(self.pl_movement(self.ikeja), {"4100": -100000})

        audit = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.FISCAL_YEAR_REOPENED,
        )
        self.assertEqual(audit.metadata["reason"], REASON)
        self.assertEqual(audit.actor_id, self.adaeze.pk)
        self.assertEqual(sorted(audit.metadata["reversal_ids"]), sorted(r.pk for r in reversals))

    def test_the_reopening_reversal_is_not_reversible_by_hand(self):
        self.trade()
        self.close_year()
        _, reversals = reopen_fiscal_year(self.books, self.year(), reason=REASON)

        self.assertEqual(journal_reversal_action(reversals[0])["document_type"], "FiscalYear")
        with self.assertRaises(PostingError):
            reverse_journal(reversals[0])

    def test_a_reopened_year_is_corrected_and_closed_again(self):
        self.trade()
        self.close_year()
        reopen_fiscal_year(self.books, self.year(), reason=REASON)

        # December is the latest month, so it reopens without reopening any other.
        reopen_period(self.books, self.month(12), reason=REASON)
        self.post(self.ikeja, datetime.date(2026, 12, 20), [("1100", 5000, 0), ("4100", 0, 5000)])
        FiscalPeriod.objects.filter(pk=self.month(12).pk).update(status=PeriodStatus.CLOSED)
        journals, net = close_fiscal_year(self.books, self.year())

        self.assertEqual(net, 135000)
        self.assertEqual(len(journals), 2)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)
        self.assertEqual(self.retained(), 135000)
        self.assertEqual(self.retained(branch=self.ikeja), 105000)
        for branch in (self.ikeja, self.lekki):
            self.assertEqual(self.pl_movement(branch), {})

    def test_reopen_refuses_a_locked_year_an_open_year_and_a_blank_reason(self):
        with self.assertRaises(PeriodCloseError):
            reopen_fiscal_year(self.books, self.year(), reason=REASON)

        self.close_year()
        with self.assertRaises(ValidationError):
            reopen_fiscal_year(self.books, self.year(), reason="   ")
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)

        FiscalYear.objects.filter(pk=self.year().pk).update(status=PeriodStatus.LOCKED)
        with self.assertRaises(PeriodCloseError):
            reopen_fiscal_year(self.books, self.year(), reason=REASON)

    def test_reopen_refuses_when_the_closing_period_is_locked(self):
        self.trade()
        journals, _ = self.close_year()
        FiscalPeriod.objects.filter(pk=self.closing_period().pk).update(status=PeriodStatus.LOCKED)

        with self.assertRaises(PeriodCloseError):
            reopen_fiscal_year(self.books, self.year(), reason=REASON)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)
        journals[0].refresh_from_db()
        self.assertEqual(journals[0].status, DocumentStatus.POSTED)

    def test_the_endpoint_reopens_for_a_whole_tenant_bursar(self):
        self.trade()
        self.close_year()

        response = self.send(
            self.adaeze, f"fiscal-years/{self.year().pk}/reopen/", {"reason": REASON},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["fiscal_year"]["status"], PeriodStatus.OPEN)
        self.assertEqual(len(response.data["data"]["reversals"]), 1)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)

    def test_the_endpoint_needs_a_reason(self):
        self.close_year()
        response = self.send(self.adaeze, f"fiscal-years/{self.year().pk}/reopen/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("reason", str(response.data))
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)

    def test_the_endpoint_needs_its_own_key(self):
        self.close_year()
        response = self.send(
            self.kemi, f"fiscal-years/{self.year().pk}/reopen/", {"reason": REASON},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)

    def test_a_branch_bound_holder_cannot_reopen_a_year(self):
        self.trade()
        self.close_year()
        response = self.send(
            self.ngozi, f"fiscal-years/{self.year().pk}/reopen/", {"reason": REASON},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], CALENDAR_MESSAGE)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)
        self.assertEqual(self.retained(), 130000)


class ForceCloseAndReasonTests(_YearFixture):
    """Forcing a close and reopening a period are their own acts, with a reason."""

    def test_force_close_without_a_reason_is_refused(self):
        january = self.month(1)
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError):
                    close_period(self.books, january, force=True, reason=reason)
        self.assertEqual(self.month(1).status, PeriodStatus.OPEN)

        response = self.send(self.adaeze, f"periods/{january.pk}/close/", {"force": True})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("reason", str(response.data))
        self.assertEqual(self.month(1).status, PeriodStatus.OPEN)

    def test_force_close_needs_the_force_close_key(self):
        january = self.month(1)
        body = {"force": True, "reason": REASON}

        refused = self.send(self.kemi, f"periods/{january.pk}/close/", body)
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.month(1).status, PeriodStatus.OPEN)

        closed = self.send(self.femi, f"periods/{january.pk}/close/", body)
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(BranchFiscalPeriod.objects.get(
            period=january, branch=self.ikeja,
        ).status, PeriodStatus.CLOSED)
        audit = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.PERIOD_CLOSED, target_id=str(january.pk),
        )
        self.assertEqual(audit.metadata["reason"], REASON)
        self.assertTrue(audit.metadata["forced"])

    def test_an_ordinary_close_keeps_the_close_key(self):
        january = self.month(1)
        response = self.send(self.femi, f"periods/{january.pk}/close/", {})
        self.assertEqual(response.status_code, 403, response.data)
        response = self.send(self.kemi, f"periods/{january.pk}/close/", {})
        self.assertEqual(response.status_code, 200, response.data)

    def test_reopening_a_period_needs_a_reason_and_records_it(self):
        january = self.month(1)
        FiscalPeriod.objects.filter(pk=january.pk).update(status=PeriodStatus.CLOSED)

        with self.assertRaises(ValidationError):
            reopen_period(self.books, self.month(1))
        response = self.send(self.adaeze, f"periods/{january.pk}/reopen/", {})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.month(1).status, PeriodStatus.CLOSED)

        response = self.send(self.adaeze, f"periods/{january.pk}/reopen/", {"reason": REASON})
        self.assertEqual(response.status_code, 200, response.data)
        audit = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.PERIOD_REOPENED,
        )
        self.assertEqual(audit.metadata["reason"], REASON)

    def test_a_forced_year_close_needs_a_reason_and_the_force_close_key(self):
        with self.assertRaises(ValidationError):
            close_fiscal_year(self.books, self.year(), require_periods_closed=False)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)

        path = f"fiscal-years/{self.year().pk}/close/"
        refused = self.send(self.kemi, path, {"force": True, "reason": REASON})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)

        closed = self.send(self.femi, path, {"force": True, "reason": REASON})
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(BranchFiscalYear.objects.get(
            fiscal_year=self.year(), branch=self.ikeja,
        ).status, PeriodStatus.CLOSED)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)
        audit = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        )
        self.assertEqual(audit.metadata["reason"], REASON)


class DepreciationAcrossAClosedYearTests(_YearFixture):
    """A charge dated in a closed year is caught before the close, and skipped after it.

    Lagoon View bought a generator at Ikeja on 1 January 2026. The year close
    refuses while its 2026 charges are unposted and names them. If the year is
    forced closed anyway, the depreciation run leaves those charges behind as dated
    in a closed year and still posts 2027's.
    """

    def test_the_year_close_names_the_unposted_charges(self):
        self.asset(datetime.date(2026, 1, 1), months=12)
        self.seal_months()

        with self.assertRaises(PeriodCloseError) as caught:
            close_fiscal_year(self.books, self.year())

        message = caught.exception.message
        self.assertIn("11 depreciation charge(s) dated in FY2026 are not posted", message)
        self.assertIn("Generator", message)
        self.assertIn("1 Feb 2026, 1 Mar 2026, 1 Apr 2026, 1 May 2026 and 7 more", message)
        self.assertIn("Run depreciation up to 31 Dec 2026 first", message)
        self.assertEqual(caught.exception.failures, ["depreciation_posted_for_year"])
        self.assertEqual(self.year().status, PeriodStatus.OPEN)

    def test_the_year_closes_once_depreciation_has_run(self):
        from .assets import post_depreciation

        asset = self.asset(datetime.date(2026, 1, 1), months=12)
        self.assertEqual(len(post_depreciation(asset, up_to_date=datetime.date(2026, 12, 31))), 11)

        journals, net = self.close_year()

        self.assertEqual(net, -1100000)
        self.assertEqual([j.branch_id for j in journals], [self.ikeja.pk])
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)

    def test_a_forced_close_records_the_check_it_overrode(self):
        self.asset(datetime.date(2026, 1, 1), months=12)
        self.force_close_year()

        audit = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        )
        self.assertEqual(audit.metadata["overridden_checks"], ["depreciation_posted_for_year"])

    def _asset_across_the_year_end(self):
        """Bought 1 October 2026 over six months: two charges in 2026, four in 2027."""
        seed_fiscal_year(self.books, year=2027, start_month=1)
        asset = self.asset(datetime.date(2026, 10, 1), months=6)
        self.force_close_year()
        return asset

    def test_the_asset_run_skips_the_closed_year_and_carries_on(self):
        from .assets import SKIPPED_CLOSED_YEAR, post_depreciation

        asset = self._asset_across_the_year_end()

        posted = post_depreciation(asset, up_to_date=datetime.date(2027, 2, 28))

        self.assertEqual([row.seq for row in posted], [3, 4])
        self.assertEqual(
            [(s["date"], s["fiscal_year"], s["reason"]) for s in posted.skipped],
            [("2026-11-01", "FY2026", SKIPPED_CLOSED_YEAR),
             ("2026-12-01", "FY2026", SKIPPED_CLOSED_YEAR)],
        )
        asset.refresh_from_db()
        self.assertEqual(asset.accumulated_depreciation, 400000)
        self.assertEqual(
            sorted(asset.schedule.filter(is_posted=False).values_list("seq", flat=True)),
            [1, 2, 5, 6],
        )

    def test_the_period_run_skips_the_closed_year_and_carries_on(self):
        from .assets import preview_period_depreciation
        from .assets import run_period_depreciation as run_depreciation

        self._asset_across_the_year_end()
        up_to = datetime.date(2027, 2, 28)

        preview = preview_period_depreciation(self.books, up_to_date=up_to)
        self.assertEqual(preview["total"], 400000)
        self.assertEqual([s["date"] for s in preview["skipped"]], ["2026-11-01", "2026-12-01"])

        result = run_depreciation(self.books, up_to_date=up_to)
        self.assertEqual(result["charge_count"], 2)
        self.assertEqual(result["period_count"], 2)
        self.assertEqual(result["total"], 400000)
        self.assertEqual([s["date"] for s in result["skipped"]], ["2026-11-01", "2026-12-01"])

    def test_a_period_run_with_only_closed_year_charges_is_refused(self):
        from .assets import run_period_depreciation as run_depreciation
        from .exceptions import DepreciationError

        self._asset_across_the_year_end()

        with self.assertRaises(DepreciationError) as caught:
            run_depreciation(self.books, up_to_date=datetime.date(2026, 12, 31))
        self.assertIn("dated in a closed year (FY2026)", caught.exception.message)


class CloseLockTests(_YearFixture):
    """A posting and a close of its month or year take locks that serialise them.

    A posting holds KEY SHARE on its fiscal year and then its month; a year close
    and a month close hold FOR UPDATE on theirs. These tests read the SQL each one
    issues. :class:`YearCloseRaceTests` shows the locks actually make the second
    party wait.
    """

    YEAR = f'"{FiscalYear._meta.db_table}"'
    MONTH = f'"{FiscalPeriod._meta.db_table}"'

    def sql(self, action):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            action()
        return [query["sql"] for query in captured.captured_queries]

    def first(self, queries, table, lock):
        return next(
            (i for i, sql in enumerate(queries) if table in sql and sql.endswith(lock)), None,
        )

    def test_a_posting_key_shares_the_year_then_the_month(self):
        entry = self.entry(self.ikeja, datetime.date(2026, 1, 15), [("1100", 100, 0), ("4100", 0, 100)])

        queries = self.sql(lambda: post_journal(entry))

        year = self.first(queries, self.YEAR, "FOR KEY SHARE")
        month = self.first(queries, self.MONTH, "FOR KEY SHARE")
        self.assertIsNotNone(year, queries)
        self.assertIsNotNone(month, queries)
        self.assertLess(year, month)
        for table in (self.YEAR, self.MONTH):
            self.assertIsNone(self.first(queries, table, "FOR UPDATE"), queries)

    def test_a_year_close_locks_the_year_for_update_before_reading_it(self):
        self.seal_months()
        year = self.year()
        queries = self.sql(lambda: close_fiscal_year(self.books, year))

        year_queries = [sql for sql in queries if self.YEAR in sql]
        self.assertTrue(year_queries[0].endswith("FOR UPDATE"), year_queries)

    def test_reopening_a_year_locks_it_for_update_before_reading_it(self):
        self.close_year()
        year = self.year()
        queries = self.sql(lambda: reopen_fiscal_year(self.books, year, reason=REASON))

        year_queries = [sql for sql in queries if self.YEAR in sql]
        self.assertTrue(year_queries[0].endswith("FOR UPDATE"), year_queries)

    def test_a_month_close_share_locks_the_year_then_locks_the_month(self):
        january = self.month(1)
        queries = self.sql(lambda: close_period(self.books, january, run_depreciation=False))

        year = self.first(queries, self.YEAR, "FOR KEY SHARE")
        month = self.first(queries, self.MONTH, "FOR UPDATE")
        self.assertIsNotNone(year, queries)
        self.assertIsNotNone(month, queries)
        self.assertLess(year, month)


class ClosingPeriodTests(_YearFixture):
    """The close lives in its own period, so every month and the year keep their figures.

    Lagoon View earns N1,000 at Ikeja in January and N100 more in December, and
    Lekki earns N700 and spends N400 in March. After the 2026 close the year's
    income statement still reads a profit of N1,400, December still reads its N100,
    and the balance sheet carries the N1,400 in Retained Earnings.
    """

    def december_sale(self):
        self.post(self.ikeja, datetime.date(2026, 12, 15), [("1100", 10000, 0), ("4100", 0, 10000)])

    def test_every_year_is_created_with_its_closing_period(self):
        from .fiscal_calendar import open_fiscal_year

        closing = self.closing_period()
        self.assertEqual(closing.period_no, 13)
        self.assertEqual((closing.start_date, closing.end_date),
                         (datetime.date(2026, 12, 31), datetime.date(2026, 12, 31)))
        self.assertEqual(closing.status, PeriodStatus.CLOSED)

        year, periods = open_fiscal_year(
            self.books, year=2027, start_month=1, start_day=1, frequency="MONTHLY",
        )
        self.assertEqual(len(periods), 12)
        self.assertTrue(FiscalPeriod.objects.filter(
            fiscal_year=year, is_closing=True, start_date=datetime.date(2027, 12, 31),
        ).exists())

    def test_the_year_end_date_resolves_to_the_last_month(self):
        self.assertEqual(resolve_period(self.books, datetime.date(2026, 12, 31)), self.month(12))

        window = posting_window(self.books, today=datetime.date(2026, 6, 15))
        listed = {p["id"] for p in window["open"] + window["blocked"]}
        self.assertNotIn(self.closing_period().pk, listed)

    def test_the_closing_period_rejects_ordinary_postings(self):
        entry = self.entry(self.ikeja, datetime.date(2026, 12, 31), [("1100", 5000, 0), ("4100", 0, 5000)])
        entry.period = self.closing_period()
        entry.save(update_fields=["period"])

        for flags in ({}, {"allow_restricted": True}):
            with self.subTest(flags=flags):
                with self.assertRaises(PeriodClosedError) as caught:
                    post_journal(entry, **flags)
                self.assertIn("closing period", caught.exception.message)
        with self.assertRaises(PeriodCloseError):
            close_period(self.books, self.closing_period())
        with self.assertRaises(PeriodCloseError):
            reopen_period(self.books, self.closing_period(), reason=REASON)

    def test_the_closing_journals_post_into_the_closing_period(self):
        self.trade()
        journals, _ = self.close_year()
        closing = self.closing_period()
        self.assertEqual({j.period_id for j in journals}, {closing.pk})

    def test_a_closed_year_still_shows_its_real_profit(self):
        from .reports import income_statement, income_statement_compare

        self.trade()
        self.december_sale()
        self.close_year()

        year = income_statement(self.books, fiscal_year=self.year())
        self.assertEqual(year.total_income, 180000)
        self.assertEqual(year.net_income, 140000)
        december = income_statement(self.books, period=self.month(12))
        self.assertEqual(december.total_income, 10000)
        self.assertEqual(december.net_income, 10000)
        compare = income_statement_compare(self.books, fiscal_year=self.year())
        self.assertEqual(compare.fiscal_year, 2026)
        self.assertEqual(compare.net_totals.amount, 140000)

    def test_the_balance_sheet_and_trial_balance_show_the_moved_profit(self):
        from .reports import balance_sheet, trial_balance

        self.trade()
        self.december_sale()
        self.close_year()

        sheet = balance_sheet(self.books, as_of=datetime.date(2026, 12, 31))
        retained = {row.code: row.amount for row in sheet.equity_rows}
        self.assertEqual(retained.get("3200"), 140000)
        self.assertEqual(sheet.retained_earnings, 0)
        self.assertEqual(sheet.current_year_earnings, 0)
        self.assertTrue(sheet.is_balanced)

        balances = {row.code: (row.debit, row.credit) for row in trial_balance(self.books).rows}
        self.assertEqual(balances.get("3200"), (0, 140000))
        self.assertNotIn("4100", balances)
        self.assertNotIn("5200", balances)

    def test_the_equity_statement_shows_the_close_as_a_transfer(self):
        from .reports import RETAINED_EARNINGS_COLUMN, statement_of_changes_in_equity

        self.trade()
        self.close_year()

        soce = statement_of_changes_in_equity(self.books, period=self.closing_period())
        columns = {c.key: c for c in soce.columns}
        self.assertEqual(columns["3200"].transfers, 130000)
        self.assertEqual(columns["3200"].contributions, 0)
        self.assertEqual(columns[RETAINED_EARNINGS_COLUMN].transfers, -130000)
        self.assertEqual(soce.total_profit, 0)
        self.assertEqual(soce.total_transfers, 0)
        self.assertTrue(soce.is_reconciled)

    def test_the_next_years_prior_year_column_reads_the_real_figures(self):
        from .reports import income_statement_compare

        self.trade()
        self.close_year()
        seed_fiscal_year(self.books, year=2027, start_month=1)
        self.post(self.ikeja, datetime.date(2027, 2, 1), [("1100", 5000, 0), ("4100", 0, 5000)])

        compare = income_statement_compare(
            self.books, fiscal_year=FiscalYear.objects.get(entity=self.books, year=2027),
        )
        self.assertEqual(compare.prior_fiscal_year, 2026)
        self.assertEqual(compare.income_totals.prior_year, 170000)
        self.assertEqual(compare.net_totals.prior_year, 130000)
        self.assertEqual(compare.net_totals.amount, 5000)

    def test_budget_against_actual_and_the_dashboard_read_the_closed_years_actuals(self):
        from .dashboard import _revenue_vs_budget
        from .models import Budget
        from .reports import budget_vs_actual

        self.trade()
        self.close_year()
        plan = Budget.objects.create(entity=self.books, fiscal_year=self.year(), name="2026 plan")

        report = budget_vs_actual(plan)
        actuals = {row.code: row.actual for row in report.rows}
        self.assertEqual(actuals.get("4100"), 170000)
        self.assertEqual(actuals.get("5200"), 40000)
        card = _revenue_vs_budget(self.books, self.year())
        self.assertEqual(card["revenue"]["actual"]["kobo"], 170000)
        self.assertEqual(card["expense"]["actual"]["kobo"], 40000)

    def test_current_year_earnings_are_only_the_open_years(self):
        from .constants import IFRSLine
        from .reports import balance_sheet_sections

        self.trade()
        seed_fiscal_year(self.books, year=2027, start_month=1)
        self.post(self.ikeja, datetime.date(2027, 2, 1), [("1100", 5000, 0), ("4100", 0, 5000)])

        sections = balance_sheet_sections(self.books, as_of=datetime.date(2027, 6, 30))

        self.assertEqual(sections.current_year_earnings, 5000)
        equity = next(s for s in sections.sections if s.key == "equity")
        lines = {g.line: g.amount for g in equity.groups}
        self.assertEqual(lines.get(IFRSLine.RETAINED_EARNINGS), 130000)
        self.assertTrue(sections.is_balanced)

    def test_the_income_statement_endpoint_takes_a_fiscal_year(self):
        self.trade()
        self.december_sale()
        self.close_year()
        client = TenantAPIClient(user=self.adaeze)
        base = f"/v1/finance/reports/income-statement/?entity={self.books.code}"

        year = client.get(f"{base}&fiscal_year=2026")
        self.assertEqual(year.status_code, 200, year.data)
        self.assertEqual(year.data["data"]["fiscal_year"], 2026)
        self.assertEqual(year.data["data"]["totals"]["net"]["amount"]["kobo"], 140000)

        december = client.get(f"{base}&fiscal_year=2026&period=12")
        self.assertEqual(december.status_code, 200, december.data)
        self.assertEqual(december.data["data"]["period"], self.month(12).name)
        self.assertEqual(december.data["data"]["totals"]["net"]["amount"]["kobo"], 10000)

        missing = client.get(f"{base}&fiscal_year=1999")
        self.assertEqual(missing.status_code, 404, missing.data)


@tag("slow")
class YearCloseRaceTests(TransactionTestCase):
    """A posting in flight holds the year close back, and never holds another posting.

    Each test keeps one posting's transaction open in a thread, past the guard and
    before commit, and then acts against it from the main thread with a short
    ``lock_timeout``: a year close must time out waiting for it, a second posting
    into the same month must not.

    The fixture creates its own school and tenant rather than lean on the platform
    tenant the migrations seed, because a flush by another TransactionTestCase
    under ``--keepdb`` can remove that row.
    """

    serialized_rollback = True

    def setUp(self):
        seed_currencies()
        school = make_school(slug="race-year-close", name="Race School")
        make_branch(school, name="Main Branch")
        self.books = LedgerEntity.objects.create(
            name="Race Books", code="RACEYR", kind=LedgerEntity.Kind.TENANT,
            tenant=school.tenant,
        )
        seed_chart_of_accounts(self.books)
        seed_fiscal_year(self.books, year=2026, start_month=1)

    def draft(self, pairs):
        date = datetime.date(2026, 1, 15)
        entry = JournalEntry.objects.create(
            entity=self.books, date=date, period=resolve_period(self.books, date), narration="race",
        )
        for i, (code, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=i,
            )
        return entry

    def hold_posting(self, entry):
        """Post ``entry`` in a thread and keep its transaction open until released."""
        from django.db import close_old_connections, connection, transaction

        holding, release, outcome = threading.Event(), threading.Event(), {}

        def worker():
            close_old_connections()
            try:
                with transaction.atomic():
                    post_journal(JournalEntry.objects.get(pk=entry.pk))
                    holding.set()
                    if not release.wait(10):
                        raise TimeoutError("the held posting was never released")
                outcome["posted"] = True
            except Exception as exc:  # recorded, then asserted on by the test
                outcome["error"] = exc
            finally:
                holding.set()
                connection.close()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(holding.wait(10))
        self.assertNotIn("error", outcome)
        return thread, release, outcome

    def with_lock_timeout(self, action):
        from django.db import connection, transaction

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
            return action()

    def test_a_year_close_waits_for_a_posting_in_flight(self):
        from django.db import OperationalError

        thread, release, outcome = self.hold_posting(self.draft([("1100", 5000, 0), ("4100", 0, 5000)]))
        try:
            with self.assertRaises(OperationalError):
                self.with_lock_timeout(lambda: close_fiscal_year(
                    self.books, FiscalYear.objects.get(entity=self.books),
                    require_periods_closed=False, reason="Race test.",
                ))
        finally:
            release.set()
            thread.join(10)

        self.assertEqual(outcome, {"posted": True})
        self.assertEqual(FiscalYear.objects.get(entity=self.books).status, PeriodStatus.OPEN)

    def test_two_postings_into_one_month_do_not_wait_for_each_other(self):
        thread, release, outcome = self.hold_posting(self.draft([("1100", 5000, 0), ("4100", 0, 5000)]))
        second = self.draft([("5200", 3000, 0), ("3100", 0, 3000)])
        try:
            self.with_lock_timeout(lambda: post_journal(second))
        finally:
            release.set()
            thread.join(10)

        second.refresh_from_db()
        self.assertEqual(second.status, DocumentStatus.POSTED)
        self.assertEqual(outcome, {"posted": True})
