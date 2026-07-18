"""Seed a small, repeatable CODEX procurement dataset for screen verification."""
from __future__ import annotations

import calendar
import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.models import Account, BankAccount, FiscalPeriod, LedgerEntity
from vs_procurement.models import (
    GoodsReceivedNote, GoodsReceivedNoteLine, PurchaseOrder, PurchaseOrderLine,
    Vendor, VendorCategory, VendorInvoice, VendorInvoiceLine, VendorPayment,
    VendorPaymentAllocation,
)
from vs_procurement.constants import ProcApprovalState
from vs_procurement.approvals import ensure_default_approval_templates, submit_for_approval
from vs_procurement.payables import (
    post_vendor_invoice, post_vendor_payment, reverse_vendor_payment,
)
from vs_procurement.purchasing import approve_purchase_order, post_grn, price_po
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.services import actions as workflow_actions


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
        if actor is None:
            raise CommandError("admin@codexng.com does not exist; seed the dev users first.")
        requester = (
            get_user_model().objects.filter(tenant=entity.tenant, status="ACTIVE")
            .exclude(pk=getattr(actor, "pk", None)).order_by("id").first()
            or actor
        )
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
                    # Vendor and category tuples share the same deterministic order.
                    "category": categories[index],
                    "payable_account": payable,
                    "default_expense_account": expense,
                    "is_active": True,
                    # Demo vendors are payment-eligible unless the explicit hold gate applies.
                    "kyc_status": "VERIFIED",
                    # Keep exactly one on-hold vendor for the Dashboard KPI state.
                    "on_hold": index == 2,
                },
            )
            vendors.append(vendor)

        today = timezone.localdate()
        starts = []
        cursor = today.replace(day=1)
        # Walk backward via the day before each month start, then restore chronology.
        for _ in range(8):
            starts.append(cursor)
            prior = cursor - datetime.timedelta(days=1)
            cursor = prior.replace(day=1)
        starts.reverse()

        invoice_count = 0
        # All seed amounts are integer kobo; the sequence creates a visible real trend.
        amounts = (18_450_000, 31_800_000, 12_300_000, 44_000_000, 26_800_000, 38_000_000, 57_500_000, 62_600_000)
        for index, month in enumerate(starts):
            # Stagger invoice days while clamping to the month's actual final day.
            invoice_date = month.replace(day=min(8 + index, calendar.monthrange(month.year, month.month)[1]))
            if invoice_date > today:
                # The current-month seed must never create a future-dated invoice.
                invoice_date = today
            if not FiscalPeriod.objects.filter(
                entity=entity, start_date__lte=invoice_date, end_date__gte=invoice_date,
            ).exists():
                continue
            reference = f"CODEX-DEMO-{month:%Y%m}"
            if VendorInvoice.objects.filter(entity=entity, vendor_reference=reference).exists():
                continue
            # Round-robin assignment spreads historical spend across demo vendors.
            vendor = vendors[index % len(vendors)]
            invoice = VendorInvoice.objects.create(
                entity=entity, vendor=vendor, invoice_date=invoice_date,
                # A fixed 14-day term makes older posted invoices genuinely overdue.
                due_date=invoice_date + datetime.timedelta(days=14),
                vendor_reference=reference, created_by=actor,
                narration="Procurement dashboard verification data",
                # Standing historical demo bills represent an already-approved
                # legacy period before the posting service enforces governance.
                approval_state=ProcApprovalState.APPROVED,
            )
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, description=f"{vendor.name} monthly services",
                expense_account=expense, quantity=1, unit_price=amounts[index], line_no=1,
            )
            post_vendor_invoice(invoice, actor_user=actor)
            invoice_count += 1

        po_count = 0
        # Ordered/received pairs cover untouched, partial, complete, and draft-origin POs.
        for index, (qty, received) in enumerate(((10, 0), (12, 4), (8, 8), (5, 0)), start=1):
            reference = f"CODEX-DEMO-PO-{index}"
            po = PurchaseOrder.objects.filter(entity=entity, reference=reference).first()
            if po is None:
                po = PurchaseOrder.objects.create(
                    entity=entity, vendor=vendors[(index - 1) % len(vendors)],
                    # Space demo orders three days apart so recent activity is readable.
                    order_date=today - datetime.timedelta(days=index * 3),
                    expected_date=today + datetime.timedelta(days=14),
                    reference=reference, created_by=actor,
                )
                PurchaseOrderLine.objects.create(
                    purchase_order=po, description=f"Demo order {index}",
                    expense_account=expense, quantity=qty,
                    # Scale unit price by row index to avoid four identical PO totals.
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
                    # Receipt dates remain recent but deterministic for activity ordering.
                    received_date=today - datetime.timedelta(days=index),
                    reference=f"CODEX-DEMO-GRN-{index}", created_by=actor,
                )
                GoodsReceivedNoteLine.objects.create(
                    grn=grn, po_line=line, expense_account=expense,
                    accepted_qty=received, unit_price=line.unit_price, line_no=1,
                )
                post_grn(grn, actor_user=actor)

        payment_count = 0
        bank, _ = BankAccount.objects.update_or_create(
            gl_account=Account.objects.get(entity=entity, code="1100"),
            defaults={
                "entity": entity, "name": "CODEX Operating Account",
                "bank_name": "Providus Bank", "account_number": "1023456789",
                "is_active": True, "is_primary": True,
            },
        )
        ensure_default_approval_templates(created_by=actor)
        eligible_invoices = list(
            VendorInvoice.objects.filter(
                entity=entity, status=DocumentStatus.POSTED,
                vendor__is_active=True, vendor__kyc_status="VERIFIED", vendor__on_hold=False,
                total__gt=F("amount_paid"),
            ).select_related("vendor").order_by("invoice_date", "id")[:6]
        )

        def make_payment(reference, invoice, amount):
            nonlocal payment_count
            payment = VendorPayment.objects.filter(entity=entity, reference=reference).first()
            if payment is not None:
                return payment, False
            # Draft allocations are approval instructions; posting alone advances
            # the invoice's authoritative settlement totals.
            payment = VendorPayment.objects.create(
                entity=entity, vendor=invoice.vendor, payment_date=today,
                method=PaymentMethod.BANK_TRANSFER, payment_account=bank.gl_account,
                gross_amount=amount, net_amount=amount, allocated_amount=0,
                reference=reference, narration="Procurement payment verification data",
                created_by=requester,
            )
            VendorPaymentAllocation.objects.create(
                payment=payment, vendor_invoice=invoice, amount=amount,
            )
            payment_count += 1
            return payment, True

        def decide_payment(payment, action):
            if payment.approval_state == ProcApprovalState.NOT_SUBMITTED:
                instance = submit_for_approval(payment, actor_user=requester)
            else:
                from vs_workflow.models import WorkflowInstance

                instance = WorkflowInstance.all_objects.filter(
                    document_type="procurement.vendor_payment",
                    document_object_id=str(payment.pk),
                ).order_by("-created_at").first()
                if instance is None:
                    raise CommandError(f"{payment.document_number} has no approval workflow.")
            instance.refresh_from_db()
            # Low/high-value templates may expose one or two approval stages; drive
            # each real active stage until the requested terminal outcome is reached.
            for _ in range(3):
                if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
                    break
                workflow_actions.record_action(
                    instance.id, actor, action,
                    comment="Seeded verification rejection" if action == WorkflowStageAction.REJECTED else "",
                )
                instance.refresh_from_db()
                if action == WorkflowStageAction.REJECTED:
                    break
            payment.refresh_from_db()

        if len(eligible_invoices) >= 1:
            make_payment(
                "CODEX-DEMO-PAY-DRAFT", eligible_invoices[0],
                min(6_000_000, eligible_invoices[0].balance_due),
            )
        if len(eligible_invoices) >= 2:
            rejected, created = make_payment(
                "CODEX-DEMO-PAY-REJECTED-V2", eligible_invoices[1],
                min(4_000_000, eligible_invoices[1].balance_due),
            )
            if created or rejected.approval_state == ProcApprovalState.PENDING:
                decide_payment(rejected, WorkflowStageAction.REJECTED)
        if len(eligible_invoices) >= 3:
            approved, created = make_payment(
                "CODEX-DEMO-PAY-APPROVED-V2", eligible_invoices[2],
                min(5_000_000, eligible_invoices[2].balance_due),
            )
            if created:
                decide_payment(approved, WorkflowStageAction.APPROVED)
        if len(eligible_invoices) >= 4:
            pending, created = make_payment(
                "CODEX-DEMO-PAY-PENDING-V2", eligible_invoices[3],
                min(7_000_000, eligible_invoices[3].balance_due),
            )
            if created:
                # Real workflow submission remains pending when approvers exist;
                # all-skipped templates may approve synchronously by design.
                submit_for_approval(pending, actor_user=requester)
        if len(eligible_invoices) >= 5:
            posted, created = make_payment(
                "CODEX-DEMO-PAY-POSTED-V2", eligible_invoices[4],
                min(8_000_000, eligible_invoices[4].balance_due),
            )
            if created:
                decide_payment(posted, WorkflowStageAction.APPROVED)
                post_vendor_payment(posted, actor_user=actor, auto_allocate=False)
        if eligible_invoices:
            reversal_invoice = eligible_invoices[5] if len(eligible_invoices) >= 6 else eligible_invoices[0]
            reversed_payment, created = make_payment(
                "CODEX-DEMO-PAY-REVERSED-V2", reversal_invoice,
                min(3_000_000, reversal_invoice.balance_due),
            )
            if created:
                decide_payment(reversed_payment, WorkflowStageAction.APPROVED)
                post_vendor_payment(reversed_payment, actor_user=actor, auto_allocate=False)
                reverse_vendor_payment(reversed_payment, actor_user=actor, date=today)

        self.stdout.write(self.style.SUCCESS(
            f"CODEX procurement demo ready: {len(vendors)} vendors, "
            f"+{invoice_count} invoices, +{po_count} purchase orders, "
            f"+{payment_count} vendor payments."
        ))
