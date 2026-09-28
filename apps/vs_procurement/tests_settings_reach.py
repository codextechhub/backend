"""Who may change a ledger entity's procurement settings: the key, and the reach behind it.

Payment terms, RFQ windows and invoice-matching tolerances carry no branch, so
they bind every branch buying against the books. Holding
``procurement.settings.update`` is not enough to change them: the caller's
reach has to be the whole tenant.

Lagoon View runs Ikeja and Lekki. Adaeze runs purchasing for the whole school.
Ngozi runs Lekki's: her role carries the same keys, pinned to Lekki. She reads
the settings and cannot change them, because widening the price tolerance from
Lekki would let Ikeja's vendor invoices through the match unchallenged too. A
single-branch school proves the gate does not stand in the way of the common
case, where the officer's grant is school-wide.
"""
from __future__ import annotations

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import FinanceAuditLog, LedgerEntity
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

from .models import ProcurementSettings

REFUSED = "SHARED_RECORD_READ_ONLY"

KEYS = ("procurement.settings.view", "procurement.settings.update")

BODY = {"default_payment_terms": "NET_60"}


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    return entity


class ProcurementSettingsReachTests(TestCase):
    """A two-branch school with a whole-school officer and Lekki's, and a one-branch school."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()

        cls.lagoon = make_school(slug="lagoon-view-proc-reach", name="Lagoon View")
        cls.tenant = cls.lagoon.tenant
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGPRC", cls.tenant)

        role = make_role(cls.lagoon, name="Procurement Officer", key="procurement-officer")
        for key in KEYS:
            make_role_permission(role, make_permission(key))
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-proc.example.com")
        make_assignment(cls.lagoon, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-proc.example.com")
        make_assignment(cls.lagoon, cls.ngozi, role, branch=cls.lekki)

        cls.solo = make_school(slug="solo-proc-reach", name="Harbour Primary")
        cls.solo_main = make_branch(cls.solo, name="Main Branch")
        cls.solo_books = _books("SOLPRC", cls.solo.tenant)
        solo_role = make_role(cls.solo, name="Procurement Officer", key="procurement-officer")
        for key in KEYS:
            make_role_permission(solo_role, make_permission(key))
        cls.tolu = make_school_admin(cls.solo_main, email="tolu@solo-proc.example.com")
        make_assignment(cls.solo, cls.tolu, solo_role, branch=None)

    def send(self, user, method, books, body=None):
        client = TenantAPIClient(user=user)
        url = f"/v1/procurement/settings/?entity={books.code}"
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body, format="json")

    def stored(self, books):
        return ProcurementSettings.objects.filter(entity=books).first()

    def test_a_branch_bound_holder_of_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", self.books, BODY)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(
            response.data["message"],
            "Only a school-wide administrator can change the procurement settings.",
        )
        self.assertIsNone(self.stored(self.books))
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED,
        ).exists())

    def test_a_branch_bound_holder_still_reads_them(self):
        response = self.send(self.ngozi, "get", self.books)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_changes_them(self):
        response = self.send(self.adaeze, "patch", self.books, BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.books).default_payment_terms, "NET_60")

    def test_a_single_branch_schools_officer_changes_them(self):
        response = self.send(self.tolu, "patch", self.solo_books, BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.solo_books).default_payment_terms, "NET_60")
