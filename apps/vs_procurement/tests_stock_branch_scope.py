"""Stock reads answer for the stores the caller works in, not for the entity.

A balance, a movement, an item's figures and the KPI strip above them all describe
where stock physically stands, and a store belongs to a branch. Asked with no store
named, each of these used to answer for every store the entity holds: a storekeeper
who cannot open another branch's store by id could still read its balances and every
movement through it simply by leaving the filter off.

The narrowing is the one the rest of procurement already uses, in its catalogue
reading: a store with no branch belongs to the whole school and stays visible, which
is what keeps the central store on a branch storekeeper's screen.

Two shapes of school run through this. The two-branch school is where the narrowing
has to bite; the single-branch school is where it must change nothing at all.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, FiscalPeriod, FiscalYear, LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import StockItem, StockLocation, StockMovement
from vs_procurement.stock import receive_stock
from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_role, make_school,
)


RECEIVED = datetime.date(2026, 1, 5)


class _StockFixture:
    """One entity, its stores, and one item received into each of them."""

    def build_entity(self, code, tenant):
        """An entity with a seeded chart and an open period.

        The period is what an issue or an adjustment needs: both post a journal, so
        a fixture without one would fail for a reason that has nothing to do with
        which store the stock came from.
        """
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT,
            tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        return entity

    @staticmethod
    def acc(entity, code):
        return Account.objects.get(entity=entity, code=code)

    def store(self, entity, code, name, *, branch=None, is_default=False):
        return StockLocation.objects.create(
            entity=entity, code=code, name=name, branch=branch, is_default=is_default,
        )

    def item(self, entity, *, code="BOOK", reorder_level=0):
        return StockItem.objects.create(
            entity=entity, code=code, name="Exercise book",
            inventory_account=self.acc(entity, "1400"),
            default_expense_account=self.acc(entity, "5300"),
            reorder_level=reorder_level,
        )

    @staticmethod
    def receive(item, location, quantity, value):
        return receive_stock(
            item, quantity=quantity, value=value, movement_date=RECEIVED,
            location=location,
        )

    def client_for(self, tenant, email, *, branch=None):
        """A real-JWT client for somebody who is, or is not, pinned to a branch."""
        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=tenant, branch=branch,
            status="ACTIVE", first_name="Stock", last_name="Keeper",
        )
        client = TenantAPIClient(user=user)
        client.test_user = user
        return client

    @staticmethod
    def rows(response):
        return response.data["data"]


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class StockBranchScopeTests(_StockFixture, TestCase):
    """Ikeja holds 300, Lekki 700, and the central store 100 of the same book."""

    def setUp(self):
        seed_currencies()
        self.school = make_school(slug="stock-multi", name="Multi Branch Group")
        self.tenant = self.school.tenant
        self.lekki = make_branch(self.school, name="Lekki Branch")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)

        self.entity = self.build_entity("STKMULTI", self.tenant)
        self.central = self.store(
            self.entity, "CENTRAL", "Central store", is_default=True)
        self.lekki_store = self.store(
            self.entity, "LEKKI", "Lekki store", branch=self.lekki)
        self.ikeja_store = self.store(
            self.entity, "IKEJA", "Ikeja store", branch=self.ikeja)

        self.book = self.item(self.entity, reorder_level=500)
        self.receive(self.book, self.central, 100, 50_000)
        self.receive(self.book, self.lekki_store, 700, 350_000)
        self.receive(self.book, self.ikeja_store, 300, 150_000)

        self.storekeeper = self.client_for(
            self.tenant, "ikeja-store@test.com", branch=self.ikeja)
        self.head_office = self.client_for(self.tenant, "hq-store@test.com")

    def url(self, path):
        return f"/v1/procurement/{path}?entity={self.entity.code}"

    # -- balances ------------------------------------------------------------ #

    def test_a_storekeeper_reads_their_own_store_and_the_shared_one(self, _perm):
        response = self.storekeeper.get(self.url("stock-balances/"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            {row["location_code"] for row in self.rows(response)},
            {"IKEJA", "CENTRAL"},
        )

    def test_head_office_still_reads_every_store(self, _perm):
        response = self.head_office.get(self.url("stock-balances/"))

        self.assertEqual(
            {row["location_code"] for row in self.rows(response)},
            {"IKEJA", "LEKKI", "CENTRAL"},
        )

    def test_another_branchs_store_cannot_be_named_in_the_filter(self, _perm):
        """The filter is not a way round the narrowing it was left off to avoid."""
        response = self.storekeeper.get(
            self.url("stock-balances/") + "&location=LEKKI")

        self.assertEqual(response.status_code, 400, response.data)

    # -- movements ----------------------------------------------------------- #

    def test_a_storekeeper_never_reads_another_branchs_movements(self, _perm):
        """The case the change exists for: Lekki's receipts, read from Ikeja."""
        response = self.storekeeper.get(self.url("stock-movements/"))

        self.assertEqual(response.status_code, 200, response.data)
        codes = {row["location_code"] for row in self.rows(response)}
        self.assertEqual(codes, {"IKEJA", "CENTRAL"})
        self.assertNotIn("LEKKI", codes)

    # -- item list ----------------------------------------------------------- #

    def test_the_item_list_reports_what_this_callers_stores_hold(self, _perm):
        response = self.storekeeper.get(self.url("stock-items/"))
        row = self.rows(response)[0]

        self.assertEqual(Decimal(str(row["on_hand_qty"])), Decimal(400))
        self.assertEqual(row["stock_value"], 200_000)
        # 400 is at or below the reorder level of 500; the school's 1,100 is not.
        self.assertTrue(row["needs_reorder"])

    def test_head_offices_item_list_is_the_school_total(self, _perm):
        response = self.head_office.get(self.url("stock-items/"))
        row = self.rows(response)[0]

        self.assertEqual(Decimal(str(row["on_hand_qty"])), Decimal(1100))
        self.assertEqual(row["stock_value"], 550_000)
        self.assertFalse(row["needs_reorder"])

    def test_an_item_held_only_at_another_branch_reports_nothing(self, _perm):
        """Never the entity roll-up, which is what leaked the other branch's stock."""
        lekki_only = self.item(self.entity, code="CHALK")
        self.receive(lekki_only, self.lekki_store, 40, 20_000)

        rows = {
            row["code"]: row
            for row in self.rows(self.storekeeper.get(self.url("stock-items/")))
        }

        self.assertEqual(Decimal(str(rows["CHALK"]["on_hand_qty"])), Decimal(0))
        self.assertEqual(rows["CHALK"]["stock_value"], 0)

    # -- summary ------------------------------------------------------------- #

    def test_the_summary_values_only_the_stores_in_scope(self, _perm):
        data = self.storekeeper.get(self.url("stock-items/summary/")).data["data"]

        self.assertEqual(data["total_value"], 200_000)
        self.assertEqual(data["tracked"], 1)
        self.assertEqual(data["low_stock"], 1)
        self.assertEqual(data["out_of_stock"], 0)

    def test_head_offices_summary_is_the_school_total(self, _perm):
        data = self.head_office.get(self.url("stock-items/summary/")).data["data"]

        self.assertEqual(data["total_value"], 550_000)
        self.assertEqual(data["tracked"], 1)
        self.assertEqual(data["low_stock"], 0)

    # -- the item drawer ----------------------------------------------------- #

    def test_the_item_drawer_shows_this_callers_stores_only(self, _perm):
        data = self.storekeeper.get(
            self.url(f"stock-items/{self.book.pk}/")).data["data"]

        self.assertEqual(
            {movement["location_code"] for movement in data["movements"]},
            {"IKEJA", "CENTRAL"},
        )
        self.assertEqual(Decimal(str(data["on_hand_qty"])), Decimal(400))
        self.assertEqual(data["stock_value"], 200_000)


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class SingleBranchStockIsUnchangedTests(_StockFixture, TestCase):
    """One branch, one store, and a storekeeper pinned to it: nothing recedes.

    Every store in the school is in this caller's scope, so the figures they read
    are the school's figures, which is what they were before stock reads narrowed
    at all.
    """

    def setUp(self):
        seed_currencies()
        self.school = make_school(slug="stock-flat", name="Single Site School")
        self.tenant = self.school.tenant
        self.main = make_branch(self.school, name="Main Branch")

        self.entity = self.build_entity("STKFLAT", self.tenant)
        self.main_store = self.store(
            self.entity, "MAIN", "Main store", branch=self.main, is_default=True)
        self.book = self.item(self.entity, reorder_level=10)
        self.receive(self.book, self.main_store, 50, 25_000)

        self.storekeeper = self.client_for(
            self.tenant, "main-store@test.com", branch=self.main)

    def url(self, path):
        return f"/v1/procurement/{path}?entity={self.entity.code}"

    def test_the_item_list_still_reports_the_whole_school(self, _perm):
        row = self.rows(self.storekeeper.get(self.url("stock-items/")))[0]

        self.assertEqual(Decimal(str(row["on_hand_qty"])), Decimal(50))
        self.assertEqual(row["stock_value"], 25_000)
        self.assertFalse(row["needs_reorder"])

    def test_the_summary_still_reports_the_whole_school(self, _perm):
        data = self.storekeeper.get(self.url("stock-items/summary/")).data["data"]

        self.assertEqual(data["tracked"], 1)
        self.assertEqual(data["active"], 1)
        self.assertEqual(data["total_value"], 25_000)
        self.assertEqual(data["low_stock"], 0)
        self.assertEqual(data["out_of_stock"], 0)

    def test_an_item_with_no_stock_anywhere_is_still_counted(self, _perm):
        """The count is of the catalogue, so a new item does not vanish from it."""
        self.item(self.entity, code="CHALK")

        data = self.storekeeper.get(self.url("stock-items/summary/")).data["data"]

        self.assertEqual(data["tracked"], 2)
        self.assertEqual(data["out_of_stock"], 1)

    def test_the_balances_and_the_ledger_are_still_whole(self, _perm):
        balances = self.storekeeper.get(self.url("stock-balances/"))
        movements = self.storekeeper.get(self.url("stock-movements/"))

        self.assertEqual(
            {row["location_code"] for row in self.rows(balances)}, {"MAIN"})
        self.assertEqual(
            {row["location_code"] for row in self.rows(movements)}, {"MAIN"})

    def test_stock_still_issues_without_anybody_naming_a_store(self, _perm):
        """One store and one branch: the dimension recedes, as it always did."""
        response = self.storekeeper.post(
            self.url(f"stock-items/{self.book.pk}/issue/"),
            {"quantity": 10, "movement_date": "2026-01-20"}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["movement"]["location_code"], "MAIN")


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class StockMovementComesFromTheCallersStoreTests(_StockFixture, TestCase):
    """A movement that names no store draws from one the person actually works in.

    This school keeps its only book store at Lekki. An Ikeja storekeeper issuing
    without naming a store used to draw from it: the stock service picks the
    entity's only active location, and the entity is not the thing the caller is
    pinned to. Reading Lekki's shelf was the first half of the same fault; taking
    stock off it is the half that changes the books.
    """

    def setUp(self):
        seed_currencies()
        self.school = make_school(slug="stock-write", name="Two Branch Group")
        self.tenant = self.school.tenant
        self.lekki = make_branch(self.school, name="Lekki Branch")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)

        self.entity = self.build_entity("STKWRITE", self.tenant)
        self.lekki_store = self.store(
            self.entity, "LEKKI", "Lekki store", branch=self.lekki, is_default=True)
        self.book = self.item(self.entity)
        self.receive(self.book, self.lekki_store, 100, 50_000)

        self.storekeeper = self.client_for(
            self.tenant, "ikeja-write@test.com", branch=self.ikeja)
        self.head_office = self.client_for(self.tenant, "hq-write@test.com")

    def url(self, path):
        return f"/v1/procurement/{path}?entity={self.entity.code}"

    def issue(self, client, **body):
        payload = {"quantity": 10, "movement_date": "2026-01-20"}
        payload.update(body)
        return client.post(
            self.url(f"stock-items/{self.book.pk}/issue/"), payload, format="json")

    def adjust(self, client, **body):
        payload = {"quantity_delta": -5, "movement_date": "2026-01-20"}
        payload.update(body)
        return client.post(
            self.url(f"stock-items/{self.book.pk}/adjust/"), payload, format="json")

    def test_issuing_without_a_store_cannot_draw_from_another_branchs(self, _perm):
        before = StockMovement.objects.count()

        response = self.issue(self.storekeeper)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_adjusting_without_a_store_cannot_touch_another_branchs(self, _perm):
        before = StockMovement.objects.count()

        response = self.adjust(self.storekeeper)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_naming_the_other_branchs_store_is_refused_as_well(self, _perm):
        response = self.issue(self.storekeeper, location="LEKKI")

        self.assertEqual(response.status_code, 400, response.data)

    def test_head_office_still_issues_from_the_only_store_unprompted(self, _perm):
        """Nobody entitled to every store is asked anything new."""
        response = self.issue(self.head_office)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["movement"]["location_code"], "LEKKI")

    def test_a_storekeeper_with_one_store_of_their_own_need_not_name_it(self, _perm):
        ikeja_store = self.store(
            self.entity, "IKEJA", "Ikeja store", branch=self.ikeja)
        self.receive(self.book, ikeja_store, 50, 25_000)

        response = self.issue(self.storekeeper)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["movement"]["location_code"], "IKEJA")

    def test_a_central_store_is_still_everybodys_to_issue_from(self, _perm):
        """A null branch means shared with the whole school on this side too."""
        shared = self.build_entity("STKSHARE", self.tenant)
        central = self.store(shared, "CENTRAL", "Central store", is_default=True)
        book = self.item(shared)
        self.receive(book, central, 80, 40_000)

        response = self.storekeeper.post(
            f"/v1/procurement/stock-items/{book.pk}/issue/?entity={shared.code}",
            {"quantity": 10, "movement_date": "2026-01-20"}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["movement"]["location_code"], "CENTRAL")


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class StockLocationBranchWriteTests(_StockFixture, TestCase):
    """Which branch a store is filed at when the person filing it names none.

    A store is master data, not a purchase: a school keeping one central store for
    every site is the ordinary arrangement, so somebody who covers several branches
    and names none has filed a school-wide store rather than left a question
    unanswered. Somebody who works at one branch has filed that branch's store.
    """

    def setUp(self):
        seed_currencies()
        self.school = make_school(slug="stock-stores", name="Store Group")
        self.tenant = self.school.tenant
        self.lekki = make_branch(self.school, name="Lekki Branch")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)

        self.entity = self.build_entity("STKSTORE", self.tenant)
        self.central = self.store(
            self.entity, "CENTRAL", "Central store", is_default=True)

    def works_at(self, client, *branches):
        """Give the client's user an active grant at each branch named.

        One role held at two sites is the arrangement a single home posting cannot
        express, and it is what makes the branch genuinely ambiguous when a store is
        filed without one.
        """
        role = make_role(self.school, name="Storekeeper")
        for branch in branches:
            make_assignment(self.school, client.test_user, role, branch=branch)
        return client

    def create(self, client, **body):
        payload = {"code": "ANNEX", "name": "Annex store"}
        payload.update(body)
        return client.post(
            f"/v1/procurement/stock-locations/?entity={self.entity.code}",
            payload, format="json",
        )

    def patch_store(self, client, location, **body):
        return client.patch(
            f"/v1/procurement/stock-locations/{location.pk}/?entity={self.entity.code}",
            body, format="json",
        )

    def test_a_storekeeper_files_a_new_store_at_their_own_branch(self, _perm):
        client = self.client_for(self.tenant, "ikeja-store@test.com", branch=self.ikeja)

        response = self.create(client)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["branch_id"], self.ikeja.pk)

    def test_somebody_over_two_branches_files_it_school_wide(self, _perm):
        """The ambiguous case, and the route that crashed before it could answer."""
        client = self.works_at(
            self.client_for(self.tenant, "both-stores@test.com"),
            self.lekki, self.ikeja,
        )

        response = self.create(client)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["data"]["branch_id"])

    def test_an_unbound_caller_leaves_a_new_store_school_wide(self, _perm):
        client = self.client_for(self.tenant, "hq-stores@test.com")

        response = self.create(client)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["data"]["branch_id"])

    def test_a_storekeeper_cannot_file_a_store_at_another_branch(self, _perm):
        client = self.client_for(self.tenant, "ikeja-cross@test.com", branch=self.ikeja)

        response = self.create(client, branch=self.lekki.pk)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(StockLocation.objects.filter(code="ANNEX").exists())

    def test_re_branching_a_store_takes_the_same_answer(self, _perm):
        """The rule holds on the way back: a store moves the way it arrived."""
        shared = self.works_at(
            self.client_for(self.tenant, "both-patch@test.com"),
            self.lekki, self.ikeja,
        )
        pinned = self.client_for(
            self.tenant, "ikeja-patch@test.com", branch=self.ikeja)

        stays_shared = self.patch_store(shared, self.central, branch="")
        becomes_ikejas = self.patch_store(pinned, self.central, branch="")

        self.assertEqual(stays_shared.status_code, 200, stays_shared.data)
        self.assertIsNone(stays_shared.data["data"]["branch_id"])
        self.assertEqual(becomes_ikejas.status_code, 200, becomes_ikejas.data)
        self.assertEqual(becomes_ikejas.data["data"]["branch_id"], self.ikeja.pk)
