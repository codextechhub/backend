"""Seed a small, repeatable CODEX procurement dataset for screen verification."""
from __future__ import annotations

import calendar
import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.models import Account, BankAccount, FiscalPeriod, LedgerEntity, TaxCode
from vs_procurement.models import (
    CatalogItem, ContractMilestone, GoodsReceivedNote, GoodsReceivedNoteLine, PurchaseOrder,
    PurchaseOrderLine, RequestForQuotation, RfqLine, Vendor, VendorCategory, VendorContract,
    VendorInvoice, VendorInvoiceLine, VendorPayment, VendorPaymentAllocation, VendorQuotation,
    VendorQuotationLine,
)
from vs_procurement.constants import ProcApprovalState, RfqStatus
from vs_procurement.contracts import (
    activate_contract, complete_milestone, mark_expired, renew_contract,
)
from vs_procurement.approvals import ensure_default_approval_templates, submit_for_approval
from vs_procurement.payables import (
    post_vendor_invoice, post_vendor_payment, reverse_vendor_payment,
)
from vs_procurement.purchasing import approve_purchase_order, post_grn, price_po
from vs_procurement.sourcing import (
    award_quotation, cancel_rfq, issue_rfq, price_quotation, set_rfq_invitations,
    submit_quotation,
)
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

        # Persist all three supported depths without inventing transactional links:
        # equipment (L1) -> cloud (L2) -> managed cloud (L3).
        category_by_code = {category.code: category for category in categories}
        equipment = category_by_code["EQUIP"]
        services = category_by_code["SERV"]
        cloud = category_by_code["CLOUD"]
        VendorCategory.objects.filter(pk__in=[equipment.pk, services.pk]).update(parent=None)
        VendorCategory.objects.filter(pk=cloud.pk).update(parent=equipment)
        cloud.parent = equipment
        VendorCategory.objects.update_or_create(
            entity=entity, code="MCLOUD",
            defaults={
                "name": "Managed Cloud Services",
                "parent": cloud,
                "default_expense_account": expense,
                "is_active": True,
            },
        )

        # A master-only inactive row verifies governance and historical visibility
        # without fabricating transactional spend or changing deterministic vendor links.
        VendorCategory.objects.update_or_create(
            entity=entity, code="RETIRED",
            defaults={
                "name": "Retired Procurement Services",
                "parent": services,
                "default_expense_account": expense,
                "is_active": False,
            },
        )

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

        # Master-only fixtures make every vendor governance state visually verifiable
        # without fabricating transactional spend or payment history.
        for code, name, kyc_status, is_active in (
            ("PENDING-KYC", "Northstar Office Supplies", "PENDING", True),
            ("REJECTED-KYC", "Legacy Field Services", "REJECTED", True),
            ("INACTIVE-V", "Retired Technology Partners", "VERIFIED", False),
        ):
            Vendor.objects.update_or_create(
                entity=entity, code=code,
                defaults={
                    "name": name, "category": categories[2], "payable_account": payable,
                    "default_expense_account": expense, "kyc_status": kyc_status,
                    "is_active": is_active, "on_hold": False,
                },
            )

        # One extra purchase-eligible vendor is invited to an RFQ but never responds, so
        # the RFQ's Vendors Invited tab shows a genuine "Awaited" row.
        Vendor.objects.update_or_create(
            entity=entity, code="SIDMACH",
            defaults={
                "name": "Sidmach Technologies", "category": categories[0],
                "payable_account": payable, "default_expense_account": expense,
                "kyc_status": "VERIFIED", "is_active": True, "on_hold": False,
            },
        )

        purchase_tax = TaxCode.objects.filter(
            entity=entity, is_active=True, is_recoverable=True,
            paid_account__is_active=True, paid_account__is_postable=True,
        ).first()
        # Catalog fixtures are master data only: they populate every governance/default
        # state without retroactively inventing requisition, stock, or PO history.
        for code, name, description, unit, category, vendor, price, lead_time, active in (
            (
                "SRV-R760", "Dell PowerEdge R760 Server", "Rack server for data-centre workloads",
                "Unit", equipment, vendors[1], 420_000_000, 14, True,
            ),
            (
                "LIC-M365E5", "Microsoft 365 E5 Licence", "Annual enterprise user licence",
                "Seat / yr", cloud, vendors[0], 12_700_000, 1, True,
            ),
            (
                "SVC-CLOUD", "Managed Cloud Support", "Monthly managed cloud support service",
                "Month", cloud, vendors[0], 8_500_000, 3, True,
            ),
            (
                "OLD-TAPE", "Legacy Backup Tape", "Retired backup-media specification",
                "Pack", equipment, None, 48_500_000, None, False,
            ),
        ):
            CatalogItem.objects.update_or_create(
                entity=entity, code=code,
                defaults={
                    "name": name, "description": description, "unit_of_measure": unit,
                    "category": category, "preferred_vendor": vendor,
                    "default_expense_account": expense, "default_tax_code": purchase_tax,
                    "standard_unit_price": price, "lead_time_days": lead_time,
                    "is_active": active,
                },
            )

        active_contract, _ = VendorContract.objects.update_or_create(
            entity=entity, reference="CODEX-DEMO-CONTRACT-001",
            defaults={
                "vendor": vendors[0], "title": "Cloud hosting and support",
                "start_date": timezone.localdate().replace(month=1, day=1),
                "end_date": timezone.localdate().replace(month=12, day=31),
                "contract_value": 268_000_000, "payment_terms": "NET_30",
                "created_by": actor,
            },
        )
        if active_contract.status == "DRAFT":
            activate_contract(active_contract, actor_user=actor)
        # Milestones on the standing active contract (one delivered, two ahead) — added once.
        if not active_contract.milestones.exists():
            m1 = ContractMilestone.objects.create(
                contract=active_contract, line_no=1, name="Onboarding & migration",
                due_date=active_contract.start_date + datetime.timedelta(days=30), amount=40_000_000)
            complete_milestone(m1, actor_user=actor)
            ContractMilestone.objects.create(
                contract=active_contract, line_no=2, name="Mid-term service review",
                due_date=active_contract.start_date + datetime.timedelta(days=180), amount=0)
            ContractMilestone.objects.create(
                contract=active_contract, line_no=3, name="Annual renewal review",
                due_date=active_contract.end_date - datetime.timedelta(days=30), amount=0)

        today_c = timezone.localdate()
        # A draft contract (never activated) — exercises the DRAFT list/edit/activate path.
        VendorContract.objects.update_or_create(
            entity=entity, reference="CODEX-DEMO-CONTRACT-DRAFT",
            defaults={
                "vendor": vendors[1], "title": "Hardware maintenance (draft)", "status": "DRAFT",
                "start_date": today_c, "end_date": today_c + datetime.timedelta(days=365),
                "contract_value": 24_000_000, "payment_terms": "NET_30", "created_by": actor,
            },
        )
        # An ACTIVE contract ending inside the 30-day window → surfaces as "Expiring".
        expiring, _ = VendorContract.objects.update_or_create(
            entity=entity, reference="CODEX-DEMO-CONTRACT-EXPIRING",
            defaults={
                "vendor": vendors[1], "title": "Colocation (expiring soon)",
                "start_date": today_c - datetime.timedelta(days=340),
                "end_date": today_c + datetime.timedelta(days=20),
                "contract_value": 46_800_000, "payment_terms": "NET_30", "created_by": actor,
            },
        )
        if expiring.status == "DRAFT":
            activate_contract(expiring, actor_user=actor)
        # An ACTIVE contract already past its end_date, then swept to EXPIRED by the real service.
        VendorContract.objects.update_or_create(
            entity=entity, reference="CODEX-DEMO-CONTRACT-EXPIRED",
            defaults={
                "vendor": vendors[1], "title": "Connectivity (expired)",
                "start_date": today_c - datetime.timedelta(days=400),
                "end_date": today_c - datetime.timedelta(days=20),
                "contract_value": 54_200_000, "payment_terms": "NET_30",
                "status": "ACTIVE", "created_by": actor,
            },
        )
        mark_expired(entity)
        # A renewed chain: an active source contract renewed into a live successor.
        renew_source, _ = VendorContract.objects.update_or_create(
            entity=entity, reference="CODEX-DEMO-CONTRACT-RENEWSRC",
            defaults={
                "vendor": vendors[0], "title": "Managed services (renewed)",
                "start_date": today_c - datetime.timedelta(days=200),
                "end_date": today_c + datetime.timedelta(days=10),
                "contract_value": 38_400_000, "payment_terms": "NET_30",
                "status": "ACTIVE", "created_by": actor,
            },
        )
        if renew_source.status == "ACTIVE" and not renew_source.renewed_by.exists():
            renew_contract(
                renew_source, reference="CODEX-DEMO-CONTRACT-RENEWED",
                start_date=today_c + datetime.timedelta(days=11),
                end_date=today_c + datetime.timedelta(days=376),
                copy_milestones=False, actor_user=actor,
            )

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

        # Explicitly link one in-term MainOne order as a call-off so the active contract's
        # Linked POs tab shows a real "Linked" row alongside the in-term association rows.
        first_po = PurchaseOrder.objects.filter(entity=entity, reference="CODEX-DEMO-PO-1").first()
        if first_po and first_po.contract_id is None and first_po.vendor_id == active_contract.vendor_id:
            first_po.contract = active_contract
            first_po.save(update_fields=["contract", "updated_at"])

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

        # ── Sourcing: RFQs + vendor quotations (no GL effect) ─────────────────
        # Every fixture below runs through the real services (issue/submit/award/cancel);
        # none writes a status field directly. Idempotency guards on the RFQ title, which
        # is unique per fixture, so a re-run neither duplicates nor re-quotes.
        eligible = list(
            Vendor.objects.filter(entity=entity, is_active=True, on_hold=False)
            .exclude(kyc_status="REJECTED").order_by("id")[:4]
        )

        def seed_rfq(title, line_specs, *, issue=False, invited=(), budget=None):
            existing = RequestForQuotation.objects.filter(entity=entity, title=title).first()
            if existing is not None:
                return existing, False
            rfq = RequestForQuotation.objects.create(
                entity=entity, title=title, issue_date=today,
                response_due_date=today + datetime.timedelta(days=10),
                budget_estimate=budget,
                notes="Procurement sourcing verification data", created_by=actor,
            )
            for line_no, (description, quantity) in enumerate(line_specs, start=1):
                RfqLine.objects.create(
                    rfq=rfq, description=description, quantity=quantity,
                    expense_account=expense, line_no=line_no,
                )
            # Invite the addressee vendors while the RFQ is still a draft — issuing now
            # requires at least one invitation.
            if invited:
                set_rfq_invitations(rfq, list(invited), actor_user=actor)
            if issue:
                issue_rfq(rfq, actor_user=actor)
            return rfq, True

        def seed_quote(rfq, vendor, prices, *, lead_time, submit=True):
            quo = VendorQuotation.objects.create(
                entity=entity, rfq=rfq, vendor=vendor, quote_date=today,
                valid_until=today + datetime.timedelta(days=30),
                lead_time_days=lead_time, reference=f"{vendor.code}-Q{rfq.pk}",
                created_by=actor,
            )
            for line_no, (rfq_line, price) in enumerate(
                zip(rfq.lines.order_by("line_no", "id"), prices), start=1,
            ):
                VendorQuotationLine.objects.create(
                    quotation=quo, rfq_line=rfq_line, description=rfq_line.description,
                    expense_account=expense, quantity=rfq_line.quantity,
                    unit_price=price, line_no=line_no,
                )
            price_quotation(quo)
            if submit:
                submit_quotation(quo, actor_user=actor)
            return quo

        rfq_count = 0
        _, created = seed_rfq(
            "Demo sourcing — office laptops (draft)",
            [("15-inch business laptop", 25), ("Docking station", 25), ("3-year onsite warranty", 25)],
            invited=eligible[:2], budget=95_000_000,
        )
        rfq_count += int(created)

        # An issued RFQ with three competing submitted quotes plus a fourth invited vendor
        # who does not respond (so the Vendors Invited tab shows a real "Awaited" row).
        issued_rfq, created = seed_rfq(
            "Demo sourcing — data-centre switches",
            [("48-port managed switch", 6), ("10G SFP+ transceiver", 24)],
            issue=True, invited=eligible[:4], budget=48_000_000,
        )
        if created and len(eligible) >= 3:
            seed_quote(issued_rfq, eligible[0], [42_000_000, 3_200_000], lead_time=21)
            seed_quote(issued_rfq, eligible[1], [39_500_000, 3_600_000], lead_time=30)
            seed_quote(issued_rfq, eligible[2], [44_000_000, 2_900_000], lead_time=14)
            # eligible[3] (SIDMACH) is invited but deliberately never quotes → Awaited.
        rfq_count += int(created)

        # An awarded RFQ: the cheaper quote wins (real DRAFT PO), the sibling is rejected.
        awarded_rfq, created = seed_rfq(
            "Demo sourcing — managed cloud (awarded)",
            [("Managed cloud support — annual", 1)],
            issue=True, invited=eligible[:2], budget=110_000_000,
        )
        if created and len(eligible) >= 2:
            winner = seed_quote(awarded_rfq, eligible[0], [96_000_000], lead_time=7)
            seed_quote(awarded_rfq, eligible[1], [104_000_000], lead_time=10)
            award_quotation(winner, actor_user=actor)
        rfq_count += int(created)

        # A cancelled RFQ with one live quote, so the reject-on-cancel path is seeded too.
        cancelled_rfq, created = seed_rfq(
            "Demo sourcing — cancelled pilot",
            [("Pilot hardware bundle", 2)],
            issue=True, invited=eligible[:1],
        )
        if created and len(eligible) >= 1:
            seed_quote(cancelled_rfq, eligible[0], [15_000_000], lead_time=20)
            cancel_rfq(cancelled_rfq, reason="Requirement withdrawn", actor_user=actor)
        rfq_count += int(created)

        self.stdout.write(self.style.SUCCESS(
            f"CODEX procurement demo ready: {Vendor.objects.filter(entity=entity).count()} vendors, "
            f"+{invoice_count} invoices, +{po_count} purchase orders, "
            f"+{payment_count} vendor payments, +{rfq_count} RFQs."
        ))
