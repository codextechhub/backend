"""Who may write a finance record that carries no branch: the key, and the reach behind it.

The fiscal calendar, dunning policies, tax obligations, currencies, FX rates,
tax codes, cost centres, dimensions and the chart of accounts carry no branch,
so each binds every branch posting to the books. Holding the write key is not
enough to change one: the caller's reach has to be the whole tenant, and a
refusal is a 403 ``SHARED_RECORD_READ_ONLY`` with nothing written. The warning
that the calendar is running out goes only to the people who could open the
next year.

A journal with no branch is a transaction not yet given its branch, not a
shared record: a branch-bound bursar cannot read it, so posting, submitting or
reversing it answers 404, as another branch's does. The school's tax return
names no branch because it is booked per branch: Ngozi reaches it through
Lekki's share, reads and pays only that share, and may not file, un-file or
reverse the whole return (403). A return with no Lekki share is a 404 to them.
A direct entry starts a chain, so it takes its branch from the person
raising it: a branch-bound bursar's entry is their branch's, and theirs to reverse,
and a whole-school bursar at a school with several branches names one.

Lagoon View runs Ikeja and Lekki. Adaeze is the bursar for the whole school.
Ngozi is Lekki's bursar: their role carries the same keys, pinned to Lekki. They
read all of it and change none of the shared records, because closing
January from Lekki closes it for Ikeja too. Their own branch's rows stay theirs.

Harbour Primary has one branch, Main, and Tolu's grant is pinned to it. With
one branch a shared record reaches nobody they do not cover, so they may
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.policy = DunningPolicy.objects.create(entity=cls.books, name="Standard")
        DunningStage.objects.create(
            policy=cls.policy, level=1, name="Nudge", min_days_overdue=3,
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.obligation = TaxObligation.objects.create(
            entity=cls.books, code="TPAYE", name="PAYE", obligation_type="PAYE",
            liability_account=Account.objects.get(entity=cls.books, code="2310"),
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.obligation = TaxObligation.objects.create(
            entity=cls.books, code="TWHT", name="WHT", obligation_type="WHT",
            liability_account=Account.objects.get(entity=cls.books, code="2300"),
        )
        gl = Account.objects.create(
            entity=cls.books, code="1150", name="GTBank Operations",
            account_type=Account.objects.get(entity=cls.books, code="1100").account_type,
            is_postable=True,
        )
        cls.bank = BankAccount.objects.create(
            entity=cls.books, name="GTBank Operations", branch=None, gl_account=gl,
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
            source="PURCHASE",
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

    def test_a_branch_bound_holder_cannot_reach_a_school_wide_filing_to_file_or_unfile_it(self):
        draft = self.filing(1)
        filed = self.filing(2, status=TaxFilingStatus.FILED)
        attempts = (
            (draft, "file", {"filed_date": "2026-02-05"}, TaxFilingStatus.DRAFT),
            (filed, "unfile", {}, TaxFilingStatus.FILED),
        )
        for filing, action, body, status in attempts:
            with self.subTest(action=action):
                response = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/{action}/", body=body)
                self.assertEqual(response.status_code, 404, response.data)
                filing.refresh_from_db()
                self.assertEqual(filing.filing_status, status)
                self.assertEqual(filing.amount_paid, 0)

    # -- a return split over Ikeja and Lekki ---------------------------------- #

    def branch_bank(self, code, branch):
        gl = Account.objects.create(
            entity=self.books, code=code, name=f"{branch.name} Collections",
            account_type=Account.objects.get(entity=self.books, code="1100").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(
            entity=self.books, name=f"{branch.name} Collections", branch=branch, gl_account=gl,
        )

    def split_return(self):
        """March WHT: N500 withheld at Ikeja and N300 at Lekki, filed as one return."""
        from .posting import post_journal, resolve_period
        from .tax_filing import file_filing, prepare_filing

        for branch, amount in ((self.ikeja, 50_000), (self.lekki, 30_000)):
            date = datetime.date(2026, 3, 10)
            entry = JournalEntry.objects.create(
                entity=self.books, branch=branch, date=date,
                period=resolve_period(self.books, date), narration="Vendor withholding",
                source="PURCHASE",
            )
            for line_no, (code, debit, credit) in enumerate(
                (("5300", amount, 0), ("2300", 0, amount)), start=1,
            ):
                entry.lines.create(
                    account=Account.objects.get(entity=self.books, code=code),
                    debit=debit, credit=credit, line_no=line_no,
                )
            post_journal(entry)
        filing = prepare_filing(self.obligation, period_start=datetime.date(2026, 3, 1),
                                period_end=datetime.date(2026, 3, 31))
        return file_filing(filing, filed_date=datetime.date(2026, 4, 5))

    def test_a_branch_bound_holder_pays_her_own_branchs_share_from_her_branchs_bank(self):
        filing = self.split_return()
        lekki_bank = self.branch_bank("1152", self.lekki)

        response = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/pay/", body={
            "pay_date": "2026-04-10", "bank_account": lekki_bank.pk,
        })

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()["data"]
        self.assertEqual([s["branch_id"] for s in data["branch_breakdown"]], [self.lekki.pk])
        self.assertEqual((data["amount_due"], data["balance_due"]), (30_000, 0))
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)
        self.assertEqual(filing.shares.get(branch=self.lekki).amount_paid, 30_000)
        self.assertEqual(filing.shares.get(branch=self.ikeja).amount_paid, 0)

    def test_a_branch_bound_holder_cannot_pay_another_branchs_share(self):
        filing = self.split_return()
        lekki_bank = self.branch_bank("1152", self.lekki)

        named = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/pay/", body={
            "pay_date": "2026-04-10", "bank_account": lekki_bank.pk, "branch": self.ikeja.pk,
        })
        self.assertEqual(named.status_code, 400, named.data)

        self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/pay/", body={
            "pay_date": "2026-04-10", "bank_account": lekki_bank.pk,
        })
        only_ikeja_left = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/pay/", body={
            "pay_date": "2026-04-11", "bank_account": lekki_bank.pk,
        })
        self.assertEqual(only_ikeja_left.status_code, 409, only_ikeja_left.data)
        self.assertEqual(filing.shares.get(branch=self.ikeja).amount_paid, 0)

    def test_a_tenant_wide_bank_pays_no_share_of_a_split_return(self):
        filing = self.split_return()

        response = self.send(self.adaeze, "post", f"tax-filings/{filing.pk}/pay/", body={
            "pay_date": "2026-04-10", "bank_account": self.bank.pk,
        })

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(filing.remittances.exists())

    def test_a_branch_bound_reader_sees_only_her_branchs_share_of_the_return(self):
        from .tax_filing import pay_filing

        filing = self.split_return()
        pay_filing(filing, bank_account=self.branch_bank("1151", self.ikeja),
                   pay_date=datetime.date(2026, 4, 10))

        whole = self.send(self.adaeze, "get", f"tax-filings/{filing.pk}/").json()["data"]
        lekki = self.send(self.ngozi, "get", f"tax-filings/{filing.pk}/").json()["data"]
        listed = self.send(self.ngozi, "get", "tax-filings/").json()["data"]
        summary = self.send(self.ngozi, "get", "tax-filings/summary/").json()["data"]

        self.assertEqual(len(whole["branch_breakdown"]), 2)
        self.assertEqual((whole["amount_due"], whole["amount_paid"]), (80_000, 50_000))
        self.assertEqual([s["branch_id"] for s in lekki["branch_breakdown"]], [self.lekki.pk])
        self.assertEqual(
            (lekki["gross_liability"], lekki["amount_due"], lekki["amount_paid"],
             lekki["payment_status"], lekki["remittances"]),
            (30_000, 30_000, 0, "UNPAID", []),
        )
        self.assertEqual([row["amount_due"] for row in listed if row["id"] == filing.pk], [30_000])
        self.assertEqual(summary["outstanding"], 30_000)

    def test_a_branch_bound_reader_sees_only_her_branchs_outstanding_tax(self):
        """Lekki's bursar sees Lekki's N300; the school-wide N70 with no branch is nobody's."""
        from .posting import post_journal, resolve_period

        self.split_return()
        date = datetime.date(2026, 3, 20)
        entry = JournalEntry.objects.create(
            entity=self.books, branch=None, date=date, source="PURCHASE",
            period=resolve_period(self.books, date), narration="Unbranched withholding",
        )
        for line_no, (code, debit, credit) in enumerate(
            (("5300", 7_000, 0), ("2300", 0, 7_000)), start=1,
        ):
            entry.lines.create(
                account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        post_journal(entry)

        def outstanding(user):
            rows = self.send(user, "get", "tax-obligations/outstanding/").json()["data"]["rows"]
            row = next(r for r in rows if r["code"] == "TWHT")
            return row["payable_balance"]["kobo"], row["net_outstanding"]["kobo"]

        self.assertEqual(outstanding(self.ngozi), (30_000, 30_000))
        self.assertEqual(outstanding(self.adaeze), (87_000, 87_000))

    def test_a_branch_bound_holder_reaches_a_split_return_but_cannot_unfile_it(self):
        """Ngozi opens the return through Lekki's share; un-filing it moves Ikeja's too."""
        filing = self.split_return()

        response = self.send(self.ngozi, "post", f"tax-filings/{filing.pk}/unfile/")

        self.assert_refused(response, self.MESSAGE)
        filing.refresh_from_db()
        self.assertEqual(filing.filing_status, TaxFilingStatus.FILED)

    def test_a_return_with_no_share_of_theirs_is_not_listed_for_them(self):
        """A return not yet given a branch or any share is the whole school's to place."""
        split = self.split_return()
        unshared = self.filing(1)

        def listed(user):
            return {row["id"] for row in self.send(user, "get", "tax-filings/").json()["data"]}

        self.assertEqual(listed(self.ngozi), {split.pk})
        self.assertEqual(listed(self.adaeze), {split.pk, unshared.pk})
        detail = self.send(self.ngozi, "get", f"tax-filings/{unshared.pk}/")
        self.assertEqual(detail.status_code, 404, detail.json())

    def test_a_branch_bound_holder_cannot_reverse_a_remittance(self):
        from .tax_filing import pay_filing

        filing = self.split_return()
        lekki_bank = self.branch_bank("1152", self.lekki)
        pay_filing(filing, bank_account=lekki_bank, pay_date=datetime.date(2026, 4, 10))
        remittance = filing.remittances.get()

        response = self.send(
            self.ngozi, "post", f"tax-filings/{filing.pk}/remittances/{remittance.pk}/reverse/",
            body={"reason": "Paid twice."},
        )

        self.assert_refused(response, self.MESSAGE)
        remittance.refresh_from_db()
        self.assertFalse(remittance.is_reversed)

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
            ("pay", {"pay_date": "2026-01-20",
                     "bank_account": self.branch_bank("1151", self.ikeja).pk},
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
        before = plain.name
        self.assert_refused(self.rename(self.ngozi, plain, "Renamed"), self.MESSAGE)
        plain.refresh_from_db()
        self.assertEqual(plain.name, before)

    def test_the_ledger_of_a_bank_not_yet_given_a_branch_is_not_theirs_to_name(self):
        """A bank account holds one branch's money; until it has a branch it is nobody's to them."""
        unbranched_bank = self.bank_ledger("1160", None)
        response = self.rename(self.ngozi, unbranched_bank, "Renamed")
        self.assertEqual(response.status_code, 404, response.data)
        unbranched_bank.refresh_from_db()
        self.assertEqual(unbranched_bank.name, "Bank 1160")

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
    """A journal not yet given a branch is reversed only by a whole-school bursar."""

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

    def test_a_branch_bound_holder_cannot_reach_an_unbranched_journal_to_reverse_it(self):
        entry = self.posted_journal()
        self.assertEqual(self.reverse(self.ngozi, entry).status_code, 404)
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin_cc = CostCenter.objects.create(entity=cls.books, code="ADMIN", name="Admin")

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
    """A draft not yet given a branch is posted only by a whole-school bursar."""

    def post(self, user, entry):
        return self.send(user, "post", f"journals/{entry.pk}/post/")

    def test_a_branch_bound_holder_cannot_reach_an_unbranched_draft_to_post_it(self):
        entry = _draft(self.books)
        self.assertEqual(self.post(self.ngozi, entry).status_code, 404)
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
    """A draft not yet given a branch is submitted only by a whole-school bursar."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_workflow.services.roles import ensure_approver_role
        from vs_workflow.services.templates import publish_template

        from vs_rbac.models import TenantRoleTemplate

        ensure_approver_role(cls.tenant, "journal-checker")
        checker = make_school_admin(cls.ikeja, email="checker@lagoon-shared.example.com")
        make_assignment(
            cls.lagoon, checker,
            TenantRoleTemplate.objects.get(tenant=cls.tenant, key="journal-checker"),
            branch=None,
        )
        publish_template(
            tenant=cls.tenant, branch=None,
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

    def test_a_branch_bound_holder_cannot_reach_an_unbranched_draft_to_submit_it(self):
        entry = _draft(self.books)
        self.assertEqual(self.submit(self.ngozi, entry).status_code, 404)
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

    def test_a_whole_tenant_caller_at_a_two_branch_school_names_a_branch(self):
        """No school-wide journal: Adaeze's unnamed entry is a 400 and nothing is written."""
        unnamed = self.enter(self.adaeze)
        self.assertEqual(unnamed.status_code, 400, unnamed.data)
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

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
