"""Stock the demo school's stores for the Stock & receiving tab.

A development aid, run after ``seed_procurement_suppliers_demo`` on the same
books. It adds a kitchen store at the main branch and a store at the annex
beside the school-wide main store, receives stock into them through ordinary
purchase orders and goods receipts (so the inventory and GR/IR ledgers move as
they would), bills some of those receipts, and issues stock through September to
the departments that use it, named by cost centre. Printer toner and white chalk
run out; exercise books, diesel, lab gloves, detergent and rice fall below their
reorder levels; coloured chalk and paper stay healthy. One late delivery arrives
short, and one count correction is posted.

Everything goes through the procurement services and is marked ``DEMO``; no
vendor is emailed. Refuses to run twice and on production settings.

Usage::

    manage.py seed_procurement_stock_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .seed_procurement_dashboard_demo import MARK
from .seed_procurement_suppliers_demo import Seeder as SuppliersSeeder


class Command(BaseCommand):
    help = "Stock one school's demo stores: receipts, issues to cost centres, a short delivery."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import LedgerEntity
        from vs_procurement.models import StockItem, Vendor

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if not Vendor.objects.filter(entity=entity, code__startswith=MARK).exists():
            raise CommandError("Run seed_procurement_dashboard_demo on these books first.")
        if StockItem.objects.filter(entity=entity, code__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the stock demo data.")
        with mock.patch("vs_workflow.services.routing.notify", return_value=None), transaction.atomic():
            Seeder(entity, datetime.date.today(), None, self.stdout).run_stock()


# (key, name, unit, reorder level, reorder qty, store, vendor, received, unit price, issues, cost centre)
ITEMS = [
    ("TONER", "Printer toner, HP 26A", "cartridge", 6, 12, "school", "SCI", 10, 45_000_00, [3, 3, 2, 2], "ADMIN"),
    ("CHALK", "Chalk, white (box)", "box", 20, 60, "annex", "BOOKS", 40, 1_200_00, [12, 10, 10, 8], "TEACH"),
    ("BOOKS", "Exercise books, 40 leaves", "book", 600, 2400, "school", "BOOKS", 1200, 350_00, [400, 320, 300], "TEACH"),
    ("DIESEL", "Diesel", "litre", 1000, 2000, "school", "OIL", 3000, 1_000_00, [900, 850, 850], "FAC"),
    ("GLOVES", "Lab gloves (box)", "box", 20, 40, "school", "SCI", 40, 3_500_00, [12, 10, 8], "LAB"),
    ("SOAP", "Detergent (5 L)", "can", 24, 30, "school", "KLEEN", 40, 6_500_00, [10, 8, 8], "FAC"),
    ("RICE", "Rice, 50 kg bag", "bag", 12, 20, "kitchen", "FOOD", 30, 62_000_00, [8, 7, 6], "FEED"),
    ("COLOUR", "Chalk, coloured (box)", "box", 10, 40, "annex", "BOOKS", 60, 1_800_00, [10, 10], "TEACH"),
    ("PAPER", "A4 paper (ream)", "ream", 40, 100, "school", "SCI", 100, 4_800_00, [15, 15], "ADMIN"),
]


class Seeder(SuppliersSeeder):
    def run_stock(self):
        from vs_finance.models import CostCenter
        from vs_procurement.models import StockLocation, Vendor

        self.out.write(f"Stocking the demo stores on {self.entity.code}:")
        self.expense, self.payable = self.account("5300"), self.account("2100")
        self.inventory = self.account("1400")
        self.vendors = {v.code.removeprefix(f"{MARK}-"): v
                        for v in Vendor.objects.filter(entity=self.entity, code__startswith=MARK)}
        self.contracts = {}
        self.centres = {c.code.removeprefix(f"{MARK}-"): c
                        for c in CostCenter.objects.filter(entity=self.entity, code__startswith=MARK)}
        self.centres["LAB"] = CostCenter.objects.create(entity=self.entity, code=f"{MARK}-LAB", name="Science lab")
        self.stores = {
            "school": StockLocation.objects.get(entity=self.entity, is_default=True),
            "kitchen": StockLocation.objects.create(entity=self.entity, branch=self.main, code=f"{MARK}-KITCHEN",
                                                    name="Kitchen"),
            "annex": StockLocation.objects.create(entity=self.entity, branch=self.annex, code=f"{MARK}-ANNEX",
                                                  name="Annex store"),
        }
        self.branch_for = {"school": None, "kitchen": self.main, "annex": self.annex}
        items = self.items()
        self.receive_all(items)
        self.issue_all(items)
        self.late_short_delivery(items)
        self.count_correction(items)
        self.out.write("Done.")

    def items(self):
        from vs_procurement.models import StockItem

        items = {}
        for key, name, unit, level, qty, *_ in ITEMS:
            items[key] = StockItem.objects.create(
                entity=self.entity, code=f"{MARK}-{key}", name=name, unit_of_measure=unit,
                inventory_account=self.inventory, default_expense_account=self.expense,
                reorder_level=level, reorder_qty=qty,
            )
        self.say(f"{len(items)} stock items with reorder levels")
        return items

    def stock_order(self, vendor, store, lines, ordered, received, *, bill=False, rejected=None):
        """One order for several stocked items, received in full (less any rejected) into a store."""
        from vs_procurement.models import (
            GoodsReceivedNote, GoodsReceivedNoteLine, PurchaseOrder, PurchaseOrderLine,
        )
        from vs_procurement.purchasing import approve_purchase_order, post_grn, price_po

        branch = self.branch_for[store]
        po = PurchaseOrder.objects.create(
            entity=self.entity, vendor=self.vendors[vendor], branch=branch, order_date=ordered,
            expected_date=ordered + datetime.timedelta(days=5), reference=f"{MARK}-STOCK-{vendor}",
            created_by=self.actor, approval_state="APPROVED",
        )
        for n, (item, qty, price) in enumerate(lines, start=1):
            PurchaseOrderLine.objects.create(purchase_order=po, line_no=n, description=item.name, quantity=qty,
                                             unit_price=price, expense_account=self.expense)
        price_po(po)
        approve_purchase_order(po, actor_user=self.actor)
        grn = GoodsReceivedNote.objects.create(
            entity=self.entity, vendor=po.vendor, purchase_order=po, branch=branch, received_date=received,
            received_by=self.actor, reference=f"{MARK}-GRN-STOCK-{po.pk}", created_by=self.actor,
        )
        for n, ((item, qty, price), po_line) in enumerate(zip(lines, po.lines.order_by("line_no")), start=1):
            bad = (rejected or {}).get(item.code, 0)
            GoodsReceivedNoteLine.objects.create(
                grn=grn, po_line=po_line, stock_item=item, expense_account=self.inventory, line_no=n,
                expected_qty=qty, accepted_qty=qty - bad, rejected_qty=bad, unit_price=price,
            )
        post_grn(grn, actor_user=self.actor)
        if bill:
            invoice = self.bill(po, grn, None, received + datetime.timedelta(days=2))
            return po, invoice
        return po, None

    def bill(self, po, grn, qty, date, **kwargs):
        """Bill every received line of a stock order at its order price."""
        from vs_procurement.models import VendorInvoice, VendorInvoiceLine
        from vs_procurement.payables import match_vendor_invoice, post_vendor_invoice, price_vendor_invoice

        invoice = VendorInvoice.objects.create(
            entity=self.entity, vendor=po.vendor, purchase_order=po, branch=po.branch, invoice_date=date,
            due_date=date + datetime.timedelta(days=30), vendor_reference=f"{MARK}-INV-STOCK-{po.pk}",
            narration=f"{MARK} stock", created_by=self.actor, approval_state="APPROVED",
        )
        for n, line in enumerate(grn.lines.order_by("line_no"), start=1):
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, po_line=line.po_line, grn_line=line, line_no=n,
                description=line.po_line.description, expense_account=self.expense,
                quantity=line.accepted_qty, unit_price=line.unit_price,
            )
        price_vendor_invoice(invoice)
        match_vendor_invoice(invoice)
        post_vendor_invoice(invoice, actor_user=self.actor, allow_variance=True)
        return invoice

    def receive_all(self, items):
        groups = {}
        for key, _n, _u, _l, _q, store, vendor, received, price, *_ in ITEMS:
            if key == "COLOUR":
                continue  # Arrives late and short; see late_short_delivery.
            groups.setdefault((vendor, store), []).append((items[key], received, price))
        billed = 0
        for n, ((vendor, store), lines) in enumerate(sorted(groups.items())):
            _po, invoice = self.stock_order(vendor, store, lines, self.start - datetime.timedelta(days=4),
                                            self.day(1), bill=n % 2 == 0)
            billed += invoice is not None
        self.say(f"{len(groups)} stock orders received into 3 stores, {billed} of them billed")

    def issue_all(self, items):
        from vs_procurement.stock import issue_stock

        count = 0
        for key, _n, _u, _l, _q, store, _v, _r, _p, issues, centre in ITEMS:
            if key == "COLOUR":
                continue
            for k, qty in enumerate(issues):
                issue_stock(items[key], quantity=Decimal(qty), movement_date=self.day(3 + k * 6),
                            location=self.stores[store], cost_center=self.centres[centre],
                            actor_user=self.actor, reference=f"{MARK}-ISS-{key}-{k + 1}",
                            narration=f"Issued to {self.centres[centre].name}")
                count += 1
        self.say(f"{count} issues to departments through the month")

    def late_short_delivery(self, items):
        from vs_procurement.stock import issue_stock

        item = items["COLOUR"]
        self.stock_order("BOOKS", "annex", [(item, 60, 1_800_00)], self.day(12), self.day(22),
                         rejected={item.code: 5})
        issue_stock(item, quantity=Decimal(15), movement_date=self.day(23), location=self.stores["annex"],
                    cost_center=self.centres["TEACH"], actor_user=self.actor, reference=f"{MARK}-ISS-COLOUR")
        self.say("coloured chalk delivered 5 days late, 5 of 60 rejected")

    def count_correction(self, items):
        from vs_procurement.stock import adjust_stock

        adjust_stock(items["GLOVES"], quantity_delta=Decimal(-2), movement_date=self.day(24),
                     location=self.stores["school"], adjustment_account=self.account("5150"),
                     actor_user=self.actor, reference=f"{MARK}-COUNT", narration="Count correction")
        self.say("1 count correction on lab gloves")
