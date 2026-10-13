"""A stock transfer between branches tells a caller no more than they may read elsewhere.

Corona's Ikeja store sends a textbook to Lekki's store. Finance books the move
as an inter-branch goods transfer, so its amount, narration, reference, date,
journals and document are the same facts on the stock movement. Tunde, the
Ikeja storekeeper, works under a role that hides the inter-branch amount: he
must not read the 5,000 naira off the movement, off the running balance it
left, off the item's ledger, or off the sentence in its activity feed. Tunde
also reads only Ikeja's stores, so the transfer answers with what Ikeja sent
and not with the balance Lekki's store holds afterwards.

Three branches in the school that matters, one branch in the school where the
dimension must recede, and a rival tenant whose rows and switches stay apart.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, FinanceAuditLog
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, make_branch, set_field_access

from .models import StockItem, StockLocation
from .stock import receive_stock

RECEIVED = datetime.date(2026, 1, 15)

#: Each inter-branch switch, and the movement keys it governs.
GOVERNS = {
    "amount": ("value_amount", "value_amount_naira", "balance_value", "balance_value_naira"),
    "purpose": ("narration",),
    "reference": ("reference",),
    "transaction_date": ("movement_date",),
    "journals": ("journal_id",),
}
ALL_SWITCHES = (*GOVERNS, "related_documents")
MOVEMENT_KEYS = tuple(key for keys in GOVERNS.values() for key in keys)
RECEIVING_ONLY = ("location_id", "location_code", "balance_qty", "balance_value", "journal_id")


class StockTransferRestatedTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_branch(cls.rival_school, name="Lekki Branch", is_main=False)
        cls.ib_keys = install_declared_fields("finance.interbranch")
        cls.rival_lekki = cls.rival_school.tenant.branches.exclude(pk=cls.rival_branch.pk).get()

        cls.ikeja_store = cls.store(cls.books, "IKJ", "Ikeja store", cls.ikeja)
        cls.ikeja_lab = cls.store(cls.books, "IKJ-LAB", "Ikeja lab store", cls.ikeja)
        cls.lekki_store = cls.store(cls.books, "LEK", "Lekki store", cls.lekki)
        cls.item = cls.stocked(cls.books, cls.ikeja_store)
        cls.lekki_item_balance = receive_stock(
            cls.item, quantity=Decimal(3), value=9_000_00, movement_date=RECEIVED,
            location=cls.lekki_store,
        )

        cls.solo_main_store = cls.store(cls.solo_books, "MAIN", "Main store", cls.solo_main)
        cls.solo_annex = cls.store(cls.solo_books, "ANX", "Annex", cls.solo_main)
        cls.solo_item = cls.stocked(cls.solo_books, cls.solo_main_store)

        cls.rival_ikeja_store = cls.store(cls.rival_books, "RIK", "Rival Ikeja", cls.rival_branch)
        cls.rival_lekki_store = cls.store(cls.rival_books, "RLK", "Rival Lekki", cls.rival_lekki)
        cls.rival_item = cls.stocked(cls.rival_books, cls.rival_ikeja_store)

        cls.open_keeper = cls.keeper("keeper-open", branch=cls.ikeja)
        cls.hiding = {
            name: cls.keeper(f"keeper-no-{name}", hidden=(name,), branch=cls.ikeja)
            for name in ALL_SWITCHES
        }
        cls.closed_keeper = cls.keeper("keeper-closed", hidden=ALL_SWITCHES, branch=cls.ikeja)
        cls.head_closed = cls.keeper("head-closed", hidden=ALL_SWITCHES)
        cls.head_open = cls.keeper("head-open")
        cls.lekki_keeper = cls.keeper("keeper-lekki", branch=cls.lekki)
        cls.solo_keeper = cls.keeper(
            "solo-closed", hidden=ALL_SWITCHES, tenant=cls.solo_tenant)
        cls.rival_keeper = cls.keeper("rival-open", tenant=cls.rival_tenant)

    @classmethod
    def store(cls, books, code, name, branch):
        return StockLocation.objects.create(entity=books, code=code, name=name, branch=branch)

    @classmethod
    def stocked(cls, books, location):
        item = StockItem.objects.create(
            entity=books, code="TXT", name="Textbook",
            inventory_account=Account.objects.get(entity=books, code="1400"),
        )
        receive_stock(item, quantity=Decimal(10), value=50_000_00, movement_date=RECEIVED,
                      location=location)
        return item

    @classmethod
    def keeper(cls, tag, *, hidden=(), tenant=None, branch=None):
        """A storekeeper whose role hides the named inter-branch switches and shows the rest."""
        tenant = tenant or cls.tenant
        user = cls.user_for(tenant, f"{tag}@stock.test")
        cls.grant(user, "procurement.stock.issue", "procurement.stock.view", tenant=tenant,
                  role_key=tag, branch=branch)
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=tag)
        set_field_access(role, *cls.ib_keys, read=True, write=True)
        for name in hidden:
            set_field_access(role, f"finance.interbranch.{name}", read=False, write=False)
        return user

    def transfer(self, user, to, *, books=None, item=None, source=None, status=201):
        books = books or self.books
        item = item or self.item
        response = TenantAPIClient(user=user).post(
            f"/v1/procurement/stock-items/{item.pk}/transfer/?entity={books.code}",
            {"quantity": "1", "location": (source or self.ikeja_store).pk, "to_location": to,
             "movement_date": "2026-01-20", "reference": "REF-7",
             "narration": "Textbooks for Lekki"},
            format="json",
        )
        self.assertEqual(response.status_code, status, response.data)
        return response.data.get("data")

    def get(self, user, path, books=None):
        books = books or self.books
        sep = "&" if "?" in path else "?"
        response = TenantAPIClient(user=user).get(f"/v1/procurement/{path}{sep}entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    # -- the receiving store ------------------------------------------------- #

    def test_a_sender_outside_the_receiving_branch_is_not_told_its_balance(self):
        data = self.transfer(self.open_keeper, "LEK")

        for key in RECEIVING_ONLY:
            self.assertNotIn(key, data["received"])
        self.assertEqual(Decimal(data["received"]["quantity"]), Decimal(1))
        self.assertEqual(data["received"]["movement_type"], "TRANSFER")
        self.assertEqual(data["movement"]["location_code"], "IKJ")
        self.assertEqual(Decimal(data["movement"]["balance_qty"]), Decimal(9))

    def test_a_whole_school_sender_is_given_the_receiving_leg_in_full(self):
        data = self.transfer(self.head_open, "LEK")

        self.assertEqual(data["received"]["location_code"], "LEK")
        self.assertEqual(Decimal(data["received"]["balance_qty"]), Decimal(4))
        self.assertEqual(data["received"]["balance_value"], 14_000_00)

    def test_a_move_between_two_stores_of_one_branch_keeps_the_receiving_leg(self):
        data = self.transfer(self.closed_keeper, "IKJ-LAB")

        self.assertEqual(data["received"]["location_code"], "IKJ-LAB")
        self.assertEqual(Decimal(data["received"]["balance_qty"]), Decimal(1))

    def test_the_receiving_leg_never_reaches_the_senders_ledger(self):
        self.transfer(self.open_keeper, "LEK")

        rows = self.get(self.open_keeper, "stock-movements/")
        self.assertEqual({row["location_code"] for row in rows}, {"IKJ"})

    # -- the inter-branch switches ------------------------------------------- #

    def test_a_reader_of_every_switch_is_given_every_value(self):
        data = self.transfer(self.open_keeper, "LEK")

        for key in MOVEMENT_KEYS:
            self.assertIn(key, data["movement"])
        self.assertEqual(data["movement"]["value_amount"], -5_000_00)
        self.assertIsNotNone(data["inter_branch_transfer_id"])

    def test_each_switch_takes_out_only_the_values_it_governs(self):
        for name, user in self.hiding.items():
            with self.subTest(hidden=name):
                data = self.transfer(user, "LEK")
                for switch, keys in GOVERNS.items():
                    for key in keys:
                        if switch == name:
                            self.assertNotIn(key, data["movement"])
                        else:
                            self.assertIn(key, data["movement"])
                if name == "related_documents":
                    self.assertNotIn("inter_branch_transfer_id", data)
                else:
                    self.assertIn("inter_branch_transfer_id", data)

    def test_the_item_ledger_and_the_movement_list_follow_the_same_switches(self):
        self.transfer(self.closed_keeper, "LEK")

        detail = self.get(self.closed_keeper, f"stock-items/{self.item.pk}/")
        listed = self.get(self.closed_keeper, f"stock-movements/?stock_item={self.item.pk}")
        for rows in (detail["movements"], listed):
            sent = next(row for row in rows if row["movement_type"] == "TRANSFER")
            for key in MOVEMENT_KEYS:
                self.assertNotIn(key, sent)
            self.assertEqual(Decimal(sent["quantity"]), Decimal(-1))
            received = next(row for row in rows if row["movement_type"] == "RECEIPT")
            self.assertIn("value_amount", received)

    def test_the_activity_sentence_carries_no_amount_for_any_caller(self):
        self.transfer(self.open_keeper, "LEK")

        for user in (self.open_keeper, self.hiding["amount"]):
            detail = self.get(user, f"stock-items/{self.item.pk}/")
            sentence = next(
                row["message"] for row in detail["activity"] if row["action"] == "STOCK_TRANSFERRED"
            )
            self.assertIn("Moved 1 of TXT from IKJ to LEK", sentence)
            self.assertNotIn("₦", sentence)

    def test_a_whole_school_reader_is_held_to_the_switches_too(self):
        data = self.transfer(self.head_closed, "LEK")

        self.assertEqual(data["received"]["location_code"], "LEK")
        for part in ("movement", "received"):
            for key in MOVEMENT_KEYS:
                self.assertNotIn(key, data[part])
        self.assertNotIn("inter_branch_transfer_id", data)

    def test_a_move_inside_one_branch_has_no_inter_branch_twin_to_answer_to(self):
        data = self.transfer(self.closed_keeper, "IKJ-LAB")

        self.assertEqual(data["movement"]["value_amount"], -5_000_00)
        self.assertEqual(data["movement"]["narration"], "Textbooks for Lekki")
        self.assertNotIn("inter_branch_transfer_id", data)
        self.assertIsNone(self.transfer(self.open_keeper, "IKJ-LAB")["inter_branch_transfer_id"])

    def test_a_single_branch_school_moves_stock_between_stores_in_full(self):
        data = self.transfer(
            self.solo_keeper, "ANX", books=self.solo_books, item=self.solo_item,
            source=self.solo_main_store,
        )

        self.assertEqual(data["received"]["location_code"], "ANX")
        self.assertEqual(data["movement"]["value_amount"], -5_000_00)

    def test_one_schools_switches_do_not_reach_another(self):
        data = self.transfer(
            self.rival_keeper, "RLK", books=self.rival_books, item=self.rival_item,
            source=self.rival_ikeja_store,
        )

        for key in MOVEMENT_KEYS:
            self.assertIn(key, data["movement"])
        self.assertIsNotNone(data["inter_branch_transfer_id"])
        self.transfer(
            self.closed_keeper, "LEK", books=self.rival_books, item=self.rival_item,
            source=self.rival_ikeja_store, status=404,
        )


    # -- the item's activity feed -------------------------------------------- #

    def feed_ids(self, user, item=None, books=None):
        item = item or self.item
        detail = self.get(user, f"stock-items/{item.pk}/", books)
        return {row["id"] for row in detail["activity"]}

    def audit_ids(self, item, *, branches=None):
        rows = FinanceAuditLog.objects.filter(target_type="StockItem", target_id=str(item.pk))
        if branches is not None:
            rows = rows.filter(branch__in=branches) | rows.filter(branch__isnull=True)
        return set(rows.values_list("id", flat=True))

    def test_the_activity_feed_holds_only_the_callers_branches_entries(self):
        self.transfer(self.head_open, "LEK")

        everything = self.audit_ids(self.item)
        ikeja = self.feed_ids(self.open_keeper)
        lekki = self.feed_ids(self.lekki_keeper)

        self.assertEqual(ikeja, self.audit_ids(self.item, branches=[self.ikeja]))
        self.assertEqual(lekki, self.audit_ids(self.item, branches=[self.lekki]))
        self.assertTrue(ikeja and lekki and ikeja != lekki)
        self.assertLess(ikeja, everything)
        self.assertEqual(self.feed_ids(self.head_open), everything)

    def test_a_single_branch_school_reads_its_whole_feed(self):
        self.transfer(
            self.solo_keeper, "ANX", books=self.solo_books, item=self.solo_item,
            source=self.solo_main_store,
        )

        self.assertEqual(
            self.feed_ids(self.solo_keeper, self.solo_item, self.solo_books),
            self.audit_ids(self.solo_item),
        )

    def test_another_schools_feed_is_not_reachable_from_this_one(self):
        response = TenantAPIClient(user=self.open_keeper).get(
            f"/v1/procurement/stock-items/{self.rival_item.pk}/?entity={self.rival_books.code}")

        self.assertEqual(response.status_code, 404)
