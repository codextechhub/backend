"""Who may write a finance record that carries no branch: the key, and the reach behind it.

The fiscal calendar, dunning policies, tax obligations, currencies, FX rates,
tax codes, cost centres, dimensions and the chart of accounts carry no branch,
so each binds every branch posting to the books. So does a tax filing or a
journal filed with no branch, whether it is posted, submitted or reversed.
Holding the write key is not enough to change one: the caller's reach has to be
the whole tenant, and a refusal is a 403 ``SHARED_RECORD_READ_ONLY`` with
nothing written. The warning that the calendar is running out goes only to the
people who could open the next year.

A direct entry starts a chain, so it takes its branch from the person raising
it: a branch-bound bursar's entry is her branch's, and hers to reverse.

Lagoon View runs Ikeja and Lekki. Adaeze is the bursar for the whole school.
Ngozi is Lekki's bursar: her role carries the same keys, pinned to Lekki. She
reads all of it and changes none of the shared records, because closing
January from Lekki closes it for Ikeja too. Her own branch's rows stay hers.

Harbour Primary has one branch, Main, and Tolu's grant is pinned to it. With
one branch a shared record reaches nobody she does not cover, so she may
change it; the day Harbour opens a second branch, the same grant is refused.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

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

from .constants import DocumentStatus, PeriodStatus, TaxFilingStatus
from .models import (
    Account,
    BankAccount,
    CostCenter,
    Currency,
    Dimension,
    DunningPolicy,
    DunningStage,
    FiscalPeriod,
    FiscalYear,
    FxRate,
    JournalEntry,
    LedgerEntity,
    TaxCode,
    TaxFiling,
    TaxObligation,
)
from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

REFUSED = "SHARED_RECORD_READ_ONLY"
JAN_10 = datetime.date(2026, 1, 10)

KEYS = (
    "finance.period.view", "finance.period.close", "finance.period.reopen",
    "finance.period.lock", "finance.period.create", "finance.period.force_close",
    "finance.fiscalyear.reopen",
    "finance.dunning.view", "finance.dunning.create", "finance.dunning.update",
    "finance.tax.view", "finance.tax.create", "finance.tax.update",
    "finance.tax.file", "finance.tax.pay",
    "finance.currency.create", "finance.fxrate.view", "finance.fxrate.create",
    "finance.taxcode.create", "finance.dimension.create",
    "finance.account.view", "finance.account.create", "finance.account.update",
    "finance.journal.view", "finance.journal.reverse",
    "finance.journal.post", "finance.journal.submit", "finance.directentry.post",
    "finance.costcenter.create",
)


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    seed_fiscal_year(entity, year=2026, start_month=1)
    return entity


def _bursar_role(school):
    role = make_role(school, name="Bursar", key="bursar")
    for key in KEYS:
        make_role_permission(role, make_permission(key))
    return role


class _SharedWriteFixture(TestCase):
    """A two-branch school with a whole-school bursar and Lekki's, and a one-branch school."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()

        cls.lagoon = make_school(slug="lagoon-view-fin-shared", name="Lagoon View")
        cls.tenant = cls.lagoon.tenant
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGSHR", cls.tenant)
        role = _bursar_role(cls.lagoon)
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-shared.example.com")
        make_assignment(cls.lagoon, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-shared.example.com")
        make_assignment(cls.lagoon, cls.ngozi, role, branch=cls.lekki)

        cls.harbour = make_school(slug="harbour-fin-shared", name="Harbour Primary")
        cls.harbour_main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_books = _books("HBRSHR", cls.harbour.tenant)
        cls.tolu = make_school_admin(cls.harbour_main, email="tolu@harbour-shared.example.com")
        make_assignment(cls.harbour, cls.tolu, _bursar_role(cls.harbour), branch=cls.harbour_main)

    # -- calling -------------------------------------------------------------- #

    def send(self, user, method, path, books=None, body=None):
        books = books or self.books
        client = TenantAPIClient(user=user)
        url = f"/v1/finance/{path}?entity={books.code}"
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body or {}, format="json")

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)

    # -- rows ----------------------------------------------------------------- #

    def period(self, books=None, month=1):
        return FiscalPeriod.objects.get(
            entity=books or self.books, fiscal_year__year=2026, period_no=month,
        )

    def year(self, books=None):
        return FiscalYear.objects.get(entity=books or self.books, year=2026)


class FiscalCalendarWriteTests(_SharedWriteFixture):
    MESSAGE = "Only a school-wide administrator can change the fiscal periods and years."

    def test_a_branch_bound_holder_cannot_close_or_soft_close_a_period(self):
        jan = self.period()
        for body in (
            {"force": True, "reason": "Close for the audit."},
            {"soft": True, "force": True, "reason": "Close for the audit."},
        ):
            with self.subTest(body=body):
                response = self.send(self.ngozi, "post", f"periods/{jan.pk}/close/", body=body)
                self.assert_refused(response, self.MESSAGE)
                jan.refresh_from_db()
                self.assertEqual(jan.status, PeriodStatus.OPEN)

    def test_a_branch_bound_holder_still_reads_the_close_checklist(self):
        response = self.send(self.ngozi, "get", f"periods/{self.period().pk}/checklist/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_branch_bound_holder_cannot_reopen_or_lock_a_period(self):
        jan = self.period()
        FiscalPeriod.objects.filter(pk=jan.pk).update(status=PeriodStatus.CLOSED)
        for action in ("reopen", "lock"):
            with self.subTest(action=action):
                response = self.send(
                    self.ngozi, "post", f"periods/{jan.pk}/{action}/",
                    body={"reason": "Correct January."},
                )
                self.assert_refused(response, self.MESSAGE)
                jan.refresh_from_db()
                self.assertEqual(jan.status, PeriodStatus.CLOSED)

    def test_a_branch_bound_holder_cannot_open_or_close_a_year(self):
        opened = self.send(self.ngozi, "post", "fiscal-years/", body={"year": 2027})
        self.assert_refused(opened, self.MESSAGE)
        self.assertFalse(FiscalYear.objects.filter(entity=self.books, year=2027).exists())

        closed = self.send(
            self.ngozi, "post", f"fiscal-years/{self.year().pk}/close/",
            body={"force": True, "reason": "Close for the audit."},
        )
        self.assert_refused(closed, self.MESSAGE)
        self.assertEqual(self.year().status, PeriodStatus.OPEN)

    def test_a_whole_tenant_holder_moves_the_calendar(self):
        jan = self.period()
        steps = (
            ("close", {"soft": True, "force": True, "reason": "Close for the audit."},
             PeriodStatus.SOFT_CLOSED),
            ("reopen", {"reason": "Correct January."}, PeriodStatus.OPEN),
            ("close", {"force": True, "reason": "Close for the audit."}, PeriodStatus.CLOSED),
            ("lock", {}, PeriodStatus.LOCKED),
        )
        for action, body, status in steps:
            with self.subTest(action=action, status=status):
                response = self.send(self.adaeze, "post", f"periods/{jan.pk}/{action}/", body=body)
                self.assertEqual(response.status_code, 200, response.data)
                jan.refresh_from_db()
                self.assertEqual(jan.status, status)

        opened = self.send(self.adaeze, "post", "fiscal-years/", body={"year": 2027})
        self.assertEqual(opened.status_code, 201, opened.data)
        self.assertTrue(FiscalYear.objects.filter(entity=self.books, year=2027).exists())

        closed = self.send(
            self.adaeze, "post", f"fiscal-years/{self.year().pk}/close/",
            body={"force": True, "reason": "Close for the audit."},
        )
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(self.year().status, PeriodStatus.CLOSED)


class OneBranchTenantTests(_SharedWriteFixture):
    """A grant pinned to the only branch reaches the whole tenant, until a second opens."""

    def test_a_bursar_pinned_to_the_only_branch_closes_a_period(self):
        jan = self.period(self.harbour_books)
        response = self.send(
            self.tolu, "post", f"periods/{jan.pk}/close/", self.harbour_books,
            {"force": True, "reason": "Close for the audit."},
        )
        self.assertEqual(response.status_code, 200, response.data)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.CLOSED)

    def test_a_second_branch_makes_the_same_grant_branch_bound(self):
        make_branch(self.harbour, name="Ajah Branch", is_main=False)
        jan = self.period(self.harbour_books)
        response = self.send(
            self.tolu, "post", f"periods/{jan.pk}/close/", self.harbour_books,
            {"force": True, "reason": "Close for the audit."},
        )
        self.assert_refused(response, FiscalCalendarWriteTests.MESSAGE)
        jan.refresh_from_db()
        self.assertEqual(jan.status, PeriodStatus.OPEN)

    def test_the_session_tells_her_the_same(self):
        me = TenantAPIClient(user=self.tolu).get("/v1/user/auth/me/")
        self.assertEqual(me.status_code, 200, me.data)
        self.assertEqual(me.data["data"]["branch_reach"], {"whole_tenant": True, "branch_ids": []})

        make_branch(self.harbour, name="Ajah Branch", is_main=False)
        again = TenantAPIClient(user=self.tolu).get("/v1/user/auth/me/")
        self.assertEqual(
            again.data["data"]["branch_reach"],
            {"whole_tenant": False, "branch_ids": [self.harbour_main.pk]},
        )


class DunningPolicyWriteTests(_SharedWriteFixture):
    MESSAGE = "Only a school-wide administrator can change the dunning policies."
    STAGES = [{"name": "First reminder", "min_days_overdue": 7}]

    def setUp(self):
        super().setUp()
        self.policy = DunningPolicy.objects.create(entity=self.books, name="Standard")
        DunningStage.objects.create(
            policy=self.policy, level=1, name="Nudge", min_days_overdue=3,
        )

    def test_a_branch_bound_holder_cannot_create_or_change_one(self):
        created = self.send(
            self.ngozi, "post", "dunning-policies/", body={"name": "Gentle", "stages": self.STAGES},
        )
        self.assert_refused(created, self.MESSAGE)
        self.assertFalse(DunningPolicy.objects.filter(entity=self.books, name="Gentle").exists())

        changed = self.send(
            self.ngozi, "patch", f"dunning-policies/{self.policy.pk}/",
            body={"name": "Firm", "stages": self.STAGES},
        )
        self.assert_refused(changed, self.MESSAGE)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.name, "Standard")
        self.assertEqual(list(self.policy.stages.values_list("name", flat=True)), ["Nudge"])

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", "dunning-policies/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_creates_and_changes_one(self):
        created = self.send(
            self.adaeze, "post", "dunning-policies/", body={"name": "Gentle", "stages": self.STAGES},
        )
        self.assertEqual(created.status_code, 201, created.data)
        changed = self.send(
            self.adaeze, "patch", f"dunning-policies/{self.policy.pk}/",
            body={"name": "Firm", "stages": self.STAGES},
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.name, "Firm")
        self.assertEqual(
            list(self.policy.stages.values_list("name", flat=True)), ["First reminder"],
        )


class TaxObligationWriteTests(_SharedWriteFixture):
    MESSAGE = "Only a school-wide administrator can change the tax obligations."
    BODY = {"code": "LEVY", "obligation_type": "OTHER", "liability_account": "2300"}

    def setUp(self):
        super().setUp()
        self.obligation = TaxObligation.objects.create(
            entity=self.books, code="TPAYE", name="PAYE", obligation_type="PAYE",
            liability_account=Account.objects.get(entity=self.books, code="2310"),
            filing_day=10,
        )

    def test_a_branch_bound_holder_cannot_create_or_change_one(self):
        created = self.send(self.ngozi, "post", "tax-obligations/", body=self.BODY)
        self.assert_refused(created, self.MESSAGE)
        self.assertFalse(TaxObligation.objects.filter(entity=self.books, code="LEVY").exists())

        changed = self.send(
            self.ngozi, "patch", f"tax-obligations/{self.obligation.pk}/", body={"filing_day": 21},
        )
        self.assert_refused(changed, self.MESSAGE)
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.filing_day, 10)

    def test_a_whole_tenant_holder_creates_and_changes_one(self):
        created = self.send(self.adaeze, "post", "tax-obligations/", body=self.BODY)
        self.assertEqual(created.status_code, 201, created.data)
        changed = self.send(
            self.adaeze, "patch", f"tax-obligations/{self.obligation.pk}/", body={"filing_day": 21},
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.obligation.refresh_from_db()
        self.assertEqual(self.obligation.filing_day, 21)


class TaxFilingWriteTests(_SharedWriteFixture):
    """A filing with no branch is the school's return; a branch's filing stays the branch's."""

    MESSAGE = "Only a school-wide administrator can change a school-wide tax filing."

    def setUp(self):
        super().setUp()
        self.obligation = TaxObligation.objects.create(
            entity=self.books, code="TWHT", name="WHT", obligation_type="WHT",
            liability_account=Account.objects.get(entity=self.books, code="2300"),
        )
        gl = Account.objects.create(
            entity=self.books, code="1150", name="GTBank Operations",
            account_type=Account.objects.get(entity=self.books, code="1100").account_type,
            is_postable=True,
        )
        self.bank = BankAccount.objects.create(
            entity=self.books, name="GTBank Operations", branch=None, gl_account=gl,
        )

    def filing(self, month, *, branch=None, status=TaxFilingStatus.DRAFT):
        """A return for ``month`` whose figures match the N500 withheld that month.

        The withholding is posted for the return's own branch, or Ikeja's for a
        return of the whole tenant, so filing finds the lines the draft shows.
        """
        from .posting import post_journal, resolve_period

        date = datetime.date(2026, month, 10)
        entry = JournalEntry.objects.create(
            entity=self.books, branch=branch or self.ikeja, date=date,
            period=resolve_period(self.books, date), narration="Vendor withholding",
        )
        for line_no, (code, debit, credit) in enumerate(
            (("5300", 50_000, 0), ("2300", 0, 50_000)), start=1,
        ):
            entry.lines.create(
                account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        post_journal(entry)
        return TaxFiling.objects.create(
            entity=self.books, obligation=self.obligation, branch=branch,
            period_start=datetime.date(2026, month, 1),
            period_end=datetime.date(2026, month, 28),
            gross_liability=50_000, amount_due=50_000, filing_status=status,
        )

    def test_a_branch_bound_holder_cannot_prepare_a_school_wide_filing(self):
        response = self.send(self.ngozi, "post", "tax-filings/", body={
            "obligation": self.obligation.pk,
            "period_start": "2026-03-01", "period_end": "2026-03-31",
        })
        self.assert_refused(response, self.MESSAGE)
        self.assertFalse(TaxFiling.objects.filter(obligation=self.obligation).exists())

    def test_a_branch_bound_holder_cannot_file_unfile_or_pay_a_school_wide_filing(self):
        draft = self.filing(1)
        filed = self.filing(2, status=TaxFilingStatus.FILED)
        attempts = (
            (draft, "file", {"filed_date": "2026-02-05"}, TaxFilingStatus.DRAFT),
            (filed, "unfile", {}, TaxFilingStatus.FILED),
            (filed, "pay", {"pay_date": "2026-01-20", "bank_account": self.bank.pk},
             TaxFilingStatus.FILED),
        )
        for filing, action, body, status in attempts:
            with self.subTest(action=action):
                response = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/{action}/", body=body)
                self.assert_refused(response, self.MESSAGE)
                filing.refresh_from_db()
                self.assertEqual(filing.filing_status, status)
                self.assertEqual(filing.amount_paid, 0)

    def test_a_branch_bound_holder_still_files_her_own_branchs_return(self):
        own = self.filing(3, branch=self.lekki)
        response = self.send(
            self.ngozi, "post", f"tax-filings/{own.pk}/file/", body={"filed_date": "2026-04-05"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        own.refresh_from_db()
        self.assertEqual(own.filing_status, TaxFilingStatus.FILED)

    def test_a_whole_tenant_holder_prepares_files_unfiles_and_pays(self):
        prepared = self.send(self.adaeze, "post", "tax-filings/", body={
            "obligation": self.obligation.pk,
            "period_start": "2026-03-01", "period_end": "2026-03-31",
        })
        self.assertEqual(prepared.status_code, 201, prepared.data)

        filing = self.filing(1)
        steps = (
            ("file", {"filed_date": "2026-01-05"}, TaxFilingStatus.FILED),
            ("unfile", {}, TaxFilingStatus.DRAFT),
            ("file", {"filed_date": "2026-01-06"}, TaxFilingStatus.FILED),
            ("pay", {"pay_date": "2026-01-20", "bank_account": self.bank.pk},
             TaxFilingStatus.PAID),
        )
        for action, body, status in steps:
            with self.subTest(action=action, status=status):
                response = self.send(
                    self.adaeze, "post", f"tax-filings/{filing.pk}/{action}/", body=body,
                )
                self.assertEqual(response.status_code, 200, response.data)
                filing.refresh_from_db()
                self.assertEqual(filing.filing_status, status)


class MasterDataWriteTests(_SharedWriteFixture):
    """Currencies, FX rates, tax codes and dimensions."""

    def writes(self):
        return (
            ("currencies/", {"code": "XAF", "name": "CFA Franc"},
             "the currencies", lambda: Currency.objects.filter(code="XAF").exists()),
            ("fx-rates/", {"base": "USD", "quote": "NGN", "rate": "1500", "as_of": "2026-01-10"},
             "the exchange rates",
             lambda: FxRate.objects.filter(base_id="USD", quote_id="NGN", as_of=JAN_10).exists()),
            ("tax-codes/", {"code": "VAT75", "name": "VAT", "rate_bps": 750},
             "the tax codes",
             lambda: TaxCode.objects.filter(entity=self.books, code="VAT75").exists()),
            ("dimensions/", {"code": "FUND", "name": "Fund"},
             "the dimensions",
             lambda: Dimension.objects.filter(entity=self.books, code="FUND").exists()),
        )

    def test_a_branch_bound_holder_writes_none_of_them(self):
        for path, body, subject, exists in self.writes():
            with self.subTest(path=path):
                response = self.send(self.ngozi, "post", path, body=body)
                self.assert_refused(
                    response, f"Only a school-wide administrator can change {subject}.",
                )
                self.assertFalse(exists())

    def test_a_whole_tenant_holder_writes_each(self):
        for path, body, _subject, exists in self.writes():
            with self.subTest(path=path):
                response = self.send(self.adaeze, "post", path, body=body)
                self.assertIn(response.status_code, (200, 201), response.data)
                self.assertTrue(exists())


class ChartOfAccountsWriteTests(_SharedWriteFixture):
    """The chart is shared; a branch's own bank ledger stays that branch's to edit."""

    MESSAGE = "Only a school-wide administrator can change the chart of accounts."

    def bank_ledger(self, code, branch):
        gl = Account.objects.create(
            entity=self.books, code=code, name=f"Bank {code}",
            account_type=Account.objects.get(entity=self.books, code="1100").account_type,
            is_postable=True,
        )
        BankAccount.objects.create(entity=self.books, name=f"Bank {code}", branch=branch, gl_account=gl)
        return gl

    def rename(self, user, account, name):
        return self.send(user, "patch", f"accounts/{account.pk}/", body={"name": name})

    def test_a_branch_bound_holder_cannot_create_an_account(self):
        response = self.send(self.ngozi, "post", "accounts/", body={"code": "5310", "name": "Printing"})
        self.assert_refused(response, self.MESSAGE)
        self.assertFalse(Account.objects.filter(entity=self.books, code="5310").exists())

    def test_a_branch_bound_holder_cannot_edit_a_shared_account(self):
        plain = Account.objects.get(entity=self.books, code="5300")
        shared_bank = self.bank_ledger("1160", None)
        for account in (plain, shared_bank):
            with self.subTest(code=account.code):
                before = account.name
                self.assert_refused(self.rename(self.ngozi, account, "Renamed"), self.MESSAGE)
                account.refresh_from_db()
                self.assertEqual(account.name, before)

    def test_a_branch_bound_holder_edits_her_own_branchs_bank_ledger(self):
        own = self.bank_ledger("1170", self.lekki)
        response = self.rename(self.ngozi, own, "Lekki Collections Ledger")
        self.assertEqual(response.status_code, 200, response.data)
        own.refresh_from_db()
        self.assertEqual(own.name, "Lekki Collections Ledger")

    def test_a_whole_tenant_holder_creates_and_edits_accounts(self):
        created = self.send(self.adaeze, "post", "accounts/", body={"code": "5310", "name": "Printing"})
        self.assertEqual(created.status_code, 201, created.data)
        plain = Account.objects.get(entity=self.books, code="5300")
        response = self.rename(self.adaeze, plain, "Admin Costs")
        self.assertEqual(response.status_code, 200, response.data)
        plain.refresh_from_db()
        self.assertEqual(plain.name, "Admin Costs")


class JournalReversalTests(_SharedWriteFixture):
    """Reversing a journal with no branch moves every branch's statements."""

    MESSAGE = "Only a school-wide administrator can reverse a school-wide journal."

    def posted_journal(self, branch=None):
        from .posting import create_direct_entry, post_journal

        entry = create_direct_entry(
            self.books, lines=[("1100", 10_000, 0), ("3100", 0, 10_000)], date=JAN_10,
            narration="Capital injection",
        )
        if branch is not None:
            JournalEntry.objects.filter(pk=entry.pk).update(branch=branch)
            entry.refresh_from_db()
        post_journal(entry)
        entry.refresh_from_db()
        return entry

    def reverse(self, user, entry):
        return self.send(user, "post", f"journals/{entry.pk}/reverse/")

    def test_a_branch_bound_holder_cannot_reverse_a_school_wide_journal(self):
        entry = self.posted_journal()
        self.assert_refused(self.reverse(self.ngozi, entry), self.MESSAGE)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), 1)

    def test_a_branch_bound_holder_reverses_her_own_branchs_journal(self):
        entry = self.posted_journal(branch=self.lekki)
        response = self.reverse(self.ngozi, entry)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_whole_tenant_holder_reverses_a_school_wide_journal(self):
        entry = self.posted_journal()
        response = self.reverse(self.adaeze, entry)
        self.assertEqual(response.status_code, 201, response.data)
        entry.refresh_from_db()
        self.assertNotEqual(entry.status, DocumentStatus.POSTED)


class CostCentreWriteTests(_SharedWriteFixture):
    """A cost centre is shared, and the POST that creates one also renames one by code."""

    MESSAGE = "Only a school-wide administrator can change the cost centres."

    def setUp(self):
        super().setUp()
        self.admin_cc = CostCenter.objects.create(entity=self.books, code="ADMIN", name="Admin")

    def test_a_branch_bound_holder_cannot_create_one(self):
        response = self.send(self.ngozi, "post", "cost-centers/", body={"code": "SCI", "name": "Science"})
        self.assert_refused(response, self.MESSAGE)
        self.assertFalse(CostCenter.objects.filter(entity=self.books, code="SCI").exists())

    def test_a_branch_bound_holder_cannot_rename_one_by_code(self):
        response = self.send(
            self.ngozi, "post", "cost-centers/", body={"code": "ADMIN", "name": "Lekki Admin"},
        )
        self.assert_refused(response, self.MESSAGE)
        self.admin_cc.refresh_from_db()
        self.assertEqual(self.admin_cc.name, "Admin")

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", "cost-centers/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_creates_and_renames_one(self):
        created = self.send(self.adaeze, "post", "cost-centers/", body={"code": "SCI", "name": "Science"})
        self.assertEqual(created.status_code, 201, created.data)
        renamed = self.send(
            self.adaeze, "post", "cost-centers/", body={"code": "ADMIN", "name": "Administration"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.admin_cc.refresh_from_db()
        self.assertEqual(self.admin_cc.name, "Administration")


def _draft(books, branch=None):
    from .posting import create_direct_entry

    return create_direct_entry(
        books, lines=[("1100", 10_000, 0), ("3100", 0, 10_000)], date=JAN_10,
        narration="Capital injection", branch=branch,
    )


class DraftJournalPostTests(_SharedWriteFixture):
    """Posting a draft with no branch moves every branch's statements."""

    MESSAGE = "Only a school-wide administrator can post a school-wide journal."

    def post(self, user, entry):
        return self.send(user, "post", f"journals/{entry.pk}/post/")

    def test_a_branch_bound_holder_cannot_post_a_school_wide_draft(self):
        entry = _draft(self.books)
        self.assert_refused(self.post(self.ngozi, entry), self.MESSAGE)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)

    def test_a_branch_bound_holder_posts_her_own_branchs_draft(self):
        entry = _draft(self.books, branch=self.lekki)
        response = self.post(self.ngozi, entry)
        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)

    def test_a_whole_tenant_holder_posts_a_school_wide_draft(self):
        entry = _draft(self.books)
        response = self.post(self.adaeze, entry)
        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)


class DraftJournalSubmitTests(_SharedWriteFixture):
    """Submitting a draft with no branch sends the school's journal for posting."""

    MESSAGE = "Only a school-wide administrator can submit a school-wide journal."

    def setUp(self):
        super().setUp()
        from vs_workflow.services.roles import ensure_approver_role
        from vs_workflow.services.templates import publish_template

        from vs_rbac.models import TenantRoleTemplate

        ensure_approver_role(self.tenant, "journal-checker")
        checker = make_school_admin(self.ikeja, email="checker@lagoon-shared.example.com")
        make_assignment(
            self.lagoon, checker,
            TenantRoleTemplate.objects.get(tenant=self.tenant, key="journal-checker"),
            branch=None,
        )
        publish_template(
            tenant=self.tenant, branch=None,
            document_type="finance.journal", code="standard",
            name="Standard journal approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_source": "ROLE",
                "approver_role_key": "journal-checker",
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,
            }],
        )

    def submit(self, user, entry):
        return self.send(user, "post", f"journals/{entry.pk}/submit/")

    def instances(self, entry):
        from vs_workflow.models import WorkflowInstance

        return WorkflowInstance.objects.for_document(entry)

    def test_a_branch_bound_holder_cannot_submit_a_school_wide_draft(self):
        entry = _draft(self.books)
        self.assert_refused(self.submit(self.ngozi, entry), self.MESSAGE)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)
        self.assertFalse(self.instances(entry).exists())

    def test_a_branch_bound_holder_submits_her_own_branchs_draft(self):
        entry = _draft(self.books, branch=self.lekki)
        response = self.submit(self.ngozi, entry)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.instances(entry).exists())

    def test_a_whole_tenant_holder_submits_a_school_wide_draft(self):
        entry = _draft(self.books)
        response = self.submit(self.adaeze, entry)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.instances(entry).exists())


class DirectEntryBranchTests(_SharedWriteFixture):
    """A direct entry takes the branch of the person raising it.

    Emeka covers Ikeja and Lekki under one role, pinned to each.
    """

    LINES = [{"account": "1100", "debit": 10_000}, {"account": "3100", "credit": 10_000}]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_rbac.models import TenantRoleTemplate

        role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="bursar")
        cls.emeka = make_school_admin(cls.ikeja, email="emeka@lagoon-shared.example.com")
        make_assignment(cls.lagoon, cls.emeka, role, branch=cls.ikeja)
        make_assignment(cls.lagoon, cls.emeka, role, branch=cls.lekki)

    def enter(self, user, **extra):
        body = {"date": "2026-01-10", "narration": "Grant received", "lines": self.LINES}
        body.update(extra)
        return self.send(user, "post", "direct-entries/", body=body)

    def entry(self, response):
        return JournalEntry.objects.get(pk=response.data["data"]["id"])

    def test_a_branch_bound_bursar_files_at_her_branch_and_can_reverse_it(self):
        response = self.enter(self.ngozi)
        self.assertEqual(response.status_code, 201, response.data)
        entry = self.entry(response)
        self.assertEqual(entry.branch_id, self.lekki.pk)
        self.assertEqual(entry.status, DocumentStatus.POSTED)

        reversed_ = self.send(self.ngozi, "post", f"journals/{entry.pk}/reverse/")
        self.assertEqual(reversed_.status_code, 201, reversed_.data)

    def test_a_branch_bound_bursar_cannot_file_at_another_branch(self):
        response = self.enter(self.ngozi, branch=self.ikeja.pk)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

    def test_a_caller_with_several_branches_must_name_one(self):
        unnamed = self.enter(self.emeka)
        self.assertEqual(unnamed.status_code, 400, unnamed.data)
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

        named = self.enter(self.emeka, branch=self.ikeja.pk)
        self.assertEqual(named.status_code, 201, named.data)
        self.assertEqual(self.entry(named).branch_id, self.ikeja.pk)

    def test_a_whole_tenant_caller_may_leave_it_school_wide_or_name_a_branch(self):
        shared = self.enter(self.adaeze)
        self.assertEqual(shared.status_code, 201, shared.data)
        self.assertIsNone(self.entry(shared).branch_id)

        at_ikeja = self.enter(self.adaeze, branch=self.ikeja.pk)
        self.assertEqual(at_ikeja.status_code, 201, at_ikeja.data)
        self.assertEqual(self.entry(at_ikeja).branch_id, self.ikeja.pk)


class CalendarWarningRecipientTests(_SharedWriteFixture):
    """The calendar warning reaches the people who could open the next year, and nobody else."""

    def recipients(self, tenant):
        from .fiscal_calendar import calendar_alert_recipients

        return {user.pk for user in calendar_alert_recipients(tenant)}

    def test_only_the_whole_school_bursar_is_told_at_a_two_branch_school(self):
        self.assertEqual(self.recipients(self.tenant), {self.adaeze.pk})

    def test_a_bursar_pinned_to_the_only_branch_is_told_until_a_second_opens(self):
        self.assertEqual(self.recipients(self.harbour.tenant), {self.tolu.pk})

        make_branch(self.harbour, name="Ajah Branch", is_main=False)
        self.assertEqual(self.recipients(self.harbour.tenant), set())

    def test_the_rollover_sends_the_warning_to_them(self):
        from unittest.mock import patch

        from .fiscal_calendar import CALENDAR_ALERT_EVENT, roll_fiscal_calendar
        from .models import FinanceCalendarSettings

        FinanceCalendarSettings.objects.create(
            entity=self.books,
            next_year_mode=FinanceCalendarSettings.NextYearMode.WARN_ONLY,
        )
        with patch("vs_notifications.notify.send_notification", return_value=["n1"]) as send:
            outcome = roll_fiscal_calendar(self.books, today=datetime.date(2026, 12, 1))

        self.assertTrue(outcome["warned"])
        self.assertEqual(send.call_args.args[0], CALENDAR_ALERT_EVENT)
        self.assertEqual(
            {user.pk for user in send.call_args.kwargs["recipients"]}, {self.adaeze.pk},
        )
