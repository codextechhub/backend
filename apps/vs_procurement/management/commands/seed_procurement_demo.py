"""Seed a small, repeatable CODEX procurement dataset for screen verification."""
from __future__ import annotations

import calendar
import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from vs_finance.constants import DocumentStatus
from vs_finance.models import Account, FiscalPeriod, LedgerEntity
from vs_procurement.models import (
    GoodsReceivedNote, GoodsReceivedNoteLine, PurchaseOrder, PurchaseOrderLine,
    Vendor, VendorCategory, VendorInvoice, VendorInvoiceLine,
)
from vs_procurement.payables import post_vendor_invoice
from vs_procurement.purchasing import approve_purchase_order, post_grn, price_po


class Command(BaseCommand):
    help = "Seed idempotent CODEX procurement data for populated UI verification."

    def handle(self, *args, **options):
        entity = LedgerEntity.objects.filter(code="CODEX", is_active=True).first()
        if entity is None:
            raise CommandError("CODEX does not exist; run seed_finance_ar_demo --all first.")
        expense = Account.objects.filter(entity=entity, code="5300").first()
        payable = Account.objects.filter(entity=entity, code="2100").first()
        if expense is None or payable is None:
            raise CommandError("CODEX chart is incomplete; run seed_finance_ar_demo --all first.")

        actor = get_user_model().objects.filter(email="admin@codexng.com").first()
        categories = []
        for code, name in (
            ("CLOUD", "Cloud Infrastructure"),
            ("EQUIP", "Data Centre Equipment"),
            ("SERV", "Professional Services"),
        ):
            category, _ = VendorCategory.objects.update_or_create(
                entity=entity, code=code,
                defaults={"name": name, "default_expense_account": expense, "is_active": True},
            )
            categories.append(category)

        vendors = []
        for index, (code, name) in enumerate((
            ("MAINONE", "MainOne Cloud"),
            ("INLAKS", "Inlaks Computers"),
            ("RACK", "Rack Centre"),
        )):
            vendor, _ = Vendor.objects.update_or_create(
                entity=entity, code=code,
                defaults={
                    "name": name,
                    "category": categories[index],
                    "payable_account": payable,
                    "default_expense_account": expense,
                    "is_active": True,
                    "on_hold": index == 2,
                },
            )
            vendors.append(vendor)

        today = timezone.localdate()
        starts = []
        cursor = today.replace(day=1)
        for _ in range(8):
            starts.append(cursor)
            prior = cursor - datetime.timedelta(days=1)
            cursor = prior.replace(day=1)
        starts.reverse()

        invoice_count = 0
        amounts = (18_450_000, 31_800_000, 12_300_000, 44_000_000, 26_800_000, 38_000_000, 57_500_000, 62_600_000)
        for index, month in enumerate(starts):
            invoice_date = month.replace(day=min(8 + index, calendar.monthrange(month.year, month.month)[1]))
            if invoice_date > today:
                invoice_date = today
            if not FiscalPeriod.objects.filter(
                entity=entity, start_date__lte=invoice_date, end_date__gte=invoice_date,
            ).exists():
                continue
            reference = f"CODEX-DEMO-{month:%Y%m}"
            if VendorInvoice.objects.filter(entity=entity, vendor_reference=reference).exists():
                continue
            vendor = vendors[index % len(vendors)]
            invoice = VendorInvoice.objects.create(
                entity=entity, vendor=vendor, invoice_date=invoice_date,
                due_date=invoice_date + datetime.timedelta(days=14),
                vendor_reference=reference, created_by=actor,
                narration="Procurement dashboard verification data",
            )
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, description=f"{vendor.name} monthly services",
                expense_account=expense, quantity=1, unit_price=amounts[index], line_no=1,
            )
            post_vendor_invoice(invoice, actor_user=actor)
            invoice_count += 1

        po_count = 0
        for index, (qty, received) in enumerate(((10, 0), (12, 4), (8, 8), (5, 0)), start=1):
            reference = f"CODEX-DEMO-PO-{index}"
            po = PurchaseOrder.objects.filter(entity=entity, reference=reference).first()
            if po is None:
                po = PurchaseOrder.objects.create(
                    entity=entity, vendor=vendors[(index - 1) % len(vendors)],
                    order_date=today - datetime.timedelta(days=index * 3),
                    expected_date=today + datetime.timedelta(days=14),
                    reference=reference, created_by=actor,
                )
                PurchaseOrderLine.objects.create(
                    purchase_order=po, description=f"Demo order {index}",
                    expense_account=expense, quantity=qty,
                    unit_price=1_250_000 * index, line_no=1,
                )
                price_po(po)
                po_count += 1
            # Demo POs should mirror the populated list: approval status stays
            # APPROVED while receipt progress is represented by the GRNs.
            if po.status == DocumentStatus.DRAFT:
                approve_purchase_order(po, actor_user=actor)
            if received and not GoodsReceivedNote.objects.filter(
                entity=entity, reference=f"CODEX-DEMO-GRN-{index}",
            ).exists():
                line = po.lines.first()
                grn = GoodsReceivedNote.objects.create(
                    entity=entity, vendor=po.vendor, purchase_order=po,
                    received_date=today - datetime.timedelta(days=index),
                    reference=f"CODEX-DEMO-GRN-{index}", created_by=actor,
                )
                GoodsReceivedNoteLine.objects.create(
                    grn=grn, po_line=line, expense_account=expense,
                    accepted_qty=received, unit_price=line.unit_price, line_no=1,
                )
                post_grn(grn, actor_user=actor)

        self.stdout.write(self.style.SUCCESS(
            f"CODEX procurement demo ready: {len(vendors)} vendors, "
            f"+{invoice_count} invoices, +{po_count} purchase orders."
        ))
