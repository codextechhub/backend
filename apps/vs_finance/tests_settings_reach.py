"""Who may change a ledger entity's finance settings: the key, and the reach behind it.

Account mappings, document defaults, banking defaults and the fiscal calendar
rule all carry no branch, so each binds every branch posting to the books.
Holding ``finance.settings.update`` is not enough to change one: the caller's
reach has to be the whole tenant.

Lagoon View runs Ikeja and Lekki. Adaeze is the bursar for the whole school.
Ngozi is Lekki's bursar: her role carries the same keys, pinned to Lekki. She
reads every settings screen and changes none of them, because re-pointing the
school fees income mapping from Lekki would send Ikeja's receipts to her
choice of account too. A single-branch school proves the gate does not stand
in the way of the common case, where the bursar's grant is school-wide.
"""
from __future__ import annotations

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

from .account_mappings import resolve_mapped_account
from .constants import AccountMappingKey, FinanceAuditAction
from .models import (
    Account,
    FinanceAuditLog,
    FinanceBankingSettings,
    FinanceCalendarSettings,
    FinanceDocumentSettings,
    LedgerEntity,
)
from .seed import seed_chart_of_accounts, seed_currencies

REFUSED = "SHARED_RECORD_READ_ONLY"

KEYS = ("finance.settings.view", "finance.settings.update")


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    return entity


class _ReachFixture(TestCase):
    """A two-branch school with a whole-school bursar and Lekki's, and a one-branch school."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()

        cls.lagoon = make_school(slug="lagoon-view-fin-reach", name="Lagoon View")
        cls.tenant = cls.lagoon.tenant
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGFIN", cls.tenant)

        role = make_role(cls.lagoon, name="Bursar", key="bursar")
        for key in KEYS:
            make_role_permission(role, make_permission(key))
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-fin.example.com")
        make_assignment(cls.lagoon, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-fin.example.com")
        make_assignment(cls.lagoon, cls.ngozi, role, branch=cls.lekki)

        cls.solo = make_school(slug="solo-fin-reach", name="Harbour Primary")
        cls.solo_main = make_branch(cls.solo, name="Main Branch")
        cls.solo_books = _books("SOLFIN", cls.solo.tenant)
        solo_role = make_role(cls.solo, name="Bursar", key="bursar")
        for key in KEYS:
            make_role_permission(solo_role, make_permission(key))
        cls.tolu = make_school_admin(cls.solo_main, email="tolu@solo-fin.example.com")
        make_assignment(cls.solo, cls.tolu, solo_role, branch=None)

    def send(self, user, method, path, books, body=None):
        client = TenantAPIClient(user=user)
        url = f"/v1/finance/settings/{path}/?entity={books.code}"
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body, format="json")

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)

    def audits(self, books, action):
        return FinanceAuditLog.objects.filter(entity=books, action=action).count()


class AccountMappingReachTests(_ReachFixture):
    PATH = "account-mappings"

    def body(self, books):
        return {"mappings": {"CASH_BANK": Account.objects.get(entity=books, code="1300").pk}}

    def cash(self, books):
        return resolve_mapped_account(books, AccountMappingKey.CASH_BANK).code

    def test_a_branch_bound_holder_of_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", self.PATH, self.books, self.body(self.books))
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the finance account mappings.",
        )
        self.assertEqual(self.cash(self.books), "1100")
        self.assertEqual(self.audits(self.books, FinanceAuditAction.FINANCE_SETTINGS_UPDATED), 0)

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", self.PATH, self.books)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_changes_them(self):
        response = self.send(self.adaeze, "patch", self.PATH, self.books, self.body(self.books))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.cash(self.books), "1300")

    def test_a_single_branch_schools_bursar_changes_them(self):
        response = self.send(
            self.tolu, "patch", self.PATH, self.solo_books, self.body(self.solo_books),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.cash(self.solo_books), "1300")


class DocumentSettingsReachTests(_ReachFixture):
    PATH = "documents"
    BODY = {"default_invoice_due_days": 14}

    def stored(self, books):
        return FinanceDocumentSettings.objects.filter(entity=books).first()

    def test_a_branch_bound_holder_of_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", self.PATH, self.books, self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the finance document settings.",
        )
        self.assertIsNone(self.stored(self.books))
        self.assertEqual(self.audits(
            self.books, FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
        ), 0)

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", self.PATH, self.books)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_changes_them(self):
        response = self.send(self.adaeze, "patch", self.PATH, self.books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.books).default_invoice_due_days, 14)

    def test_a_single_branch_schools_bursar_changes_them(self):
        response = self.send(self.tolu, "patch", self.PATH, self.solo_books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.solo_books).default_invoice_due_days, 14)


class BankingSettingsReachTests(_ReachFixture):
    PATH = "banking"
    BODY = {"default_bank_reconciliation_tolerance_days": 5}

    def stored(self, books):
        return FinanceBankingSettings.objects.filter(entity=books).first()

    def test_a_branch_bound_holder_of_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", self.PATH, self.books, self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the finance banking settings.",
        )
        self.assertIsNone(self.stored(self.books))
        self.assertEqual(self.audits(
            self.books, FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
        ), 0)

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", self.PATH, self.books)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_changes_them(self):
        response = self.send(self.adaeze, "patch", self.PATH, self.books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.books).default_bank_reconciliation_tolerance_days, 5)

    def test_a_single_branch_schools_bursar_changes_them(self):
        response = self.send(self.tolu, "patch", self.PATH, self.solo_books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.stored(self.solo_books).default_bank_reconciliation_tolerance_days, 5,
        )


class CalendarSettingsReachTests(_ReachFixture):
    PATH = "calendar"
    BODY = {"next_year_mode": "WARN_ONLY"}

    def stored(self, books):
        return FinanceCalendarSettings.objects.filter(entity=books).first()

    def test_a_branch_bound_holder_of_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", self.PATH, self.books, self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the fiscal calendar settings.",
        )
        self.assertIsNone(self.stored(self.books))
        self.assertEqual(self.audits(
            self.books, FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
        ), 0)

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", self.PATH, self.books)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_changes_them(self):
        response = self.send(self.adaeze, "patch", self.PATH, self.books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.books).next_year_mode, "WARN_ONLY")

    def test_a_single_branch_schools_bursar_changes_them(self):
        response = self.send(self.tolu, "patch", self.PATH, self.solo_books, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.solo_books).next_year_mode, "WARN_ONLY")

    def test_a_branch_bound_holder_cannot_turn_the_close_order_off(self):
        response = self.send(
            self.ngozi, "patch", self.PATH, self.books, {"periods_close_in_order": False},
        )
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the fiscal calendar settings.",
        )
        self.assertIsNone(self.stored(self.books))

    def test_another_schools_books_are_not_found_and_nothing_moves(self):
        response = self.send(
            self.adaeze, "patch", self.PATH, self.solo_books, {"periods_close_in_order": False},
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.stored(self.solo_books))
        self.assertEqual(self.audits(
            self.solo_books, FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
        ), 0)
