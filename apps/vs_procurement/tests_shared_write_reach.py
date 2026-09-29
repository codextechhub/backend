"""Who may write the item catalog and the vendor categories: the key, and the reach behind it.

Neither carries a branch. A catalog item's price, preferred vendor and expense
account are the defaults every branch's requisitions start from, and a
category is the taxonomy every branch files its vendors under. Holding the
write key is not enough to change either: the caller's reach has to be the
whole tenant, and a refusal is a 403 ``SHARED_RECORD_READ_ONLY`` with nothing
written.

Lagoon View runs Ikeja and Lekki. Adaeze runs purchasing for the whole school;
Ngozi runs Lekki's, with the same keys pinned to Lekki. Harbour Primary has one
branch, and Tolu's grant pinned to it reaches the whole tenant until a second
branch opens.
"""
from __future__ import annotations

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

from .models import CatalogItem, VendorCategory

REFUSED = "SHARED_RECORD_READ_ONLY"

KEYS = (
    "procurement.catalog_item.view", "procurement.catalog_item.create",
    "procurement.catalog_item.update",
    "procurement.category.view", "procurement.category.create",
    "procurement.category.update",
)


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    return entity


def _officer_role(school):
    role = make_role(school, name="Procurement Officer", key="procurement-officer")
    for key in KEYS:
        make_role_permission(role, make_permission(key))
    return role


class _SharedWriteFixture(TestCase):
    """A two-branch school with a whole-school officer and Lekki's, and a one-branch school."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()

        cls.lagoon = make_school(slug="lagoon-view-proc-shared", name="Lagoon View")
        cls.tenant = cls.lagoon.tenant
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGPSH", cls.tenant)
        role = _officer_role(cls.lagoon)
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-pshared.example.com")
        make_assignment(cls.lagoon, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-pshared.example.com")
        make_assignment(cls.lagoon, cls.ngozi, role, branch=cls.lekki)

        cls.harbour = make_school(slug="harbour-proc-shared", name="Harbour Primary")
        cls.harbour_main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_books = _books("HBRPSH", cls.harbour.tenant)
        cls.tolu = make_school_admin(cls.harbour_main, email="tolu@harbour-pshared.example.com")
        make_assignment(
            cls.harbour, cls.tolu, _officer_role(cls.harbour), branch=cls.harbour_main,
        )

    def send(self, user, method, path, books=None, body=None):
        books = books or self.books
        client = TenantAPIClient(user=user)
        url = f"/v1/procurement/{path}?entity={books.code}"
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body or {}, format="json")

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)


class CatalogItemWriteTests(_SharedWriteFixture):
    MESSAGE = "Only a school-wide administrator can change the item catalog."
    NEW = {"code": "PEN01", "name": "Ballpoint pens", "unit_of_measure": "box"}

    def setUp(self):
        super().setUp()
        self.item = CatalogItem.objects.create(
            entity=self.books, code="CHALK", name="Chalk", unit_of_measure="box",
        )

    def test_a_branch_bound_holder_cannot_create_or_change_an_item(self):
        created = self.send(self.ngozi, "post", "catalog-items/", body=self.NEW)
        self.assert_refused(created, self.MESSAGE)
        self.assertFalse(CatalogItem.objects.filter(entity=self.books, code="PEN01").exists())

        changed = self.send(
            self.ngozi, "patch", f"catalog-items/{self.item.pk}/",
            body={"standard_unit_price": 90_000},
        )
        self.assert_refused(changed, self.MESSAGE)
        self.item.refresh_from_db()
        self.assertEqual(self.item.standard_unit_price, 0)

    def test_a_branch_bound_holder_still_reads_the_catalog(self):
        response = self.send(self.ngozi, "get", "catalog-items/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_creates_and_changes_an_item(self):
        created = self.send(self.adaeze, "post", "catalog-items/", body=self.NEW)
        self.assertEqual(created.status_code, 201, created.data)
        changed = self.send(
            self.adaeze, "patch", f"catalog-items/{self.item.pk}/",
            body={"standard_unit_price": 90_000},
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.standard_unit_price, 90_000)

    def test_an_officer_pinned_to_the_only_branch_creates_an_item(self):
        response = self.send(self.tolu, "post", "catalog-items/", self.harbour_books, self.NEW)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_second_branch_makes_the_same_grant_branch_bound(self):
        make_branch(self.harbour, name="Ajah Branch", is_main=False)
        response = self.send(self.tolu, "post", "catalog-items/", self.harbour_books, self.NEW)
        self.assert_refused(response, self.MESSAGE)
        self.assertFalse(
            CatalogItem.objects.filter(entity=self.harbour_books, code="PEN01").exists(),
        )


class VendorCategoryWriteTests(_SharedWriteFixture):
    MESSAGE = "Only a school-wide administrator can change the vendor categories."
    NEW = {"code": "STAT", "name": "Stationery"}

    def setUp(self):
        super().setUp()
        self.category = VendorCategory.objects.create(
            entity=self.books, code="FOOD", name="Catering",
        )

    def test_a_branch_bound_holder_cannot_create_or_change_a_category(self):
        created = self.send(self.ngozi, "post", "categories/", body=self.NEW)
        self.assert_refused(created, self.MESSAGE)
        self.assertFalse(VendorCategory.objects.filter(entity=self.books, code="STAT").exists())

        changed = self.send(
            self.ngozi, "patch", f"categories/{self.category.pk}/", body={"name": "Food"},
        )
        self.assert_refused(changed, self.MESSAGE)
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "Catering")

    def test_a_branch_bound_holder_still_reads_the_categories(self):
        response = self.send(self.ngozi, "get", "categories/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_whole_tenant_holder_creates_and_changes_a_category(self):
        created = self.send(self.adaeze, "post", "categories/", body=self.NEW)
        self.assertEqual(created.status_code, 201, created.data)
        changed = self.send(
            self.adaeze, "patch", f"categories/{self.category.pk}/", body={"name": "Food"},
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "Food")
