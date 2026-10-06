"""A procurement document an approver sends back is corrected by whoever sent it, then resumed.

Mrs Bello at Corona's Ikeja Branch asks for 60 chairs. Mr Eze returns the
requisition with "make it 40". She opens it, changes 60 to 40, and resumes the
request from her approvals screen: Mr Eze sees 40 chairs and approves them. The
same holds for a vendor bill, a purchase order, a vendor payment and a vendor
credit note.

A returned document is the requester's to correct and nobody else's: a colleague
with the edit key who did not send it, somebody without the key, and a Lekki
buyer outside Ikeja's reach are all refused. While the document is with its
approver, and once it is approved, nobody edits it. Its read shape says which
of its pending rows are back with their requester (``approval_returned``).

Single Site School has one branch, and the same sequence works there.
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus
from vs_finance.models import Account, BankAccount
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action

from .approvals import submit_for_approval
from .constants import (
    PROCUREMENT_APPROVAL_TYPES,
    ProcApprovalState,
    RfqStatus,
)
from .models import (
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    Vendor,
    VendorCreditNote,
    VendorCreditNoteLine,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
)
from .purchasing import create_po_from_requisition
from .tests import _P2PFixtureMixin
from .workflow_handlers import RequisitionApprovalHandler

JAN = datetime.date(2026, 1, 10)
BUYER_KEYS = tuple(
    f"procurement.{resource}.{verb}"
    for resource in ("requisition", "purchase_order", "vendor_invoice", "vendor_payment",
                     "vendor_credit_note")
    for verb in ("view", "create", "update", "submit")
)
VIEW_KEYS = tuple(key for key in BUYER_KEYS if key.endswith(".view"))
_people = itertools.count(1)


class _ReturnedFixture(_FinanceBranchFixture):
    """Corona (Ikeja, Lekki, Yaba) and Single Site, each with a one-step approval route."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vendor = cls.stationer(cls.books)
        cls.solo_vendor = cls.stationer(cls.solo_books)
        cls.eze = cls.signer(cls.tenant)
        cls.femi = cls.signer(cls.solo_tenant)
        cls.bello = cls.person(cls.tenant, "bello", BUYER_KEYS, cls.ikeja)
        cls.tunde = cls.person(cls.tenant, "tunde", BUYER_KEYS, cls.ikeja)
        cls.chidi = cls.person(cls.tenant, "chidi", VIEW_KEYS, cls.ikeja)
        cls.ngozi = cls.person(cls.tenant, "ngozi", BUYER_KEYS, cls.lekki)
        cls.adaeze = cls.person(cls.tenant, "adaeze", BUYER_KEYS, None)
        cls.ada = cls.person(cls.solo_tenant, "ada", BUYER_KEYS, cls.solo_main)

    @classmethod
    def stationer(cls, books):
        _P2PFixtureMixin.allow_non_po_bills(books)
        return Vendor.objects.create(
            entity=books, code="STAT", name="Stationer", kyc_status="VERIFIED",
            payable_account=Account.objects.get(entity=books, code="2100"),
            default_expense_account=Account.objects.get(entity=books, code="5300"),
            email="orders@stationer.test",
        )

    @classmethod
    def signer(cls, tenant):
        """One approver for every procurement document type in ``tenant``."""
        from vs_rbac.models import TenantRoleTemplate

        for document_type in PROCUREMENT_APPROVAL_TYPES:
            template, _ = WorkflowTemplate.objects.update_or_create(
                tenant=tenant, branch=None, document_type=document_type, code="standard",
                defaults={"name": "Second signature", "is_active": True})
            template.stages.all().delete()
            WorkflowStage.objects.create(
                template=template, code="signer", label="Second signature", order=10,
                approver_role_key="proc-signer")
        user = cls.user_for(tenant, f"signer-{next(_people)}@corona.test")
        cls.grant(user, *VIEW_KEYS, tenant=tenant, role_key="proc-signer")
        TenantRoleTemplate.objects.filter(tenant=tenant, key="proc-signer").update(
            is_system_role=True)
        return user

    @classmethod
    def person(cls, tenant, name, keys, branch):
        user = cls.user_for(tenant, f"{name}-{next(_people)}@corona.test")
        cls.grant(user, *keys, tenant=tenant, role_key=f"{name}-role", branch=branch)
        return user

    # -- requests ------------------------------------------------------------- #

    @staticmethod
    def url(books, path):
        return f"/v1/procurement/{path}?entity={books.code}"

    @staticmethod
    def as_(user):
        return TenantAPIClient(user=user)

    def raise_requisition(self, user, books, quantity=60):
        response = self.as_(user).post(self.url(books, "requisitions/"), {
            "title": "Chairs", "request_date": JAN.isoformat(),
            "lines": [{"description": "Chair", "quantity": quantity,
                       "estimated_unit_price": 100_000, "expense_account": "5300"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return PurchaseRequisition.objects.get(pk=response.data["data"]["id"])

    def raise_bill(self, user, books, unit_price=50_000, **extra):
        response = self.as_(user).post(self.url(books, "vendor-invoices/"), {
            **extra,
            "vendor": "STAT", "invoice_date": JAN.isoformat(), "vendor_reference": "INV-77",
            "lines": [{"description": "Exercise books", "quantity": 1,
                       "unit_price": unit_price, "expense_account": "5300"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return VendorInvoice.objects.get(pk=response.data["data"]["id"])

    @staticmethod
    def request_for(document):
        return WorkflowInstance.all_objects.filter(
            document_type=document.workflow_document_type,
            document_object_id=str(document.pk),
        ).order_by("-created_at").first()

    def sent_and_returned(self, document, requester, approver, comment="Make it 40"):
        submit_for_approval(document, actor_user=requester)
        record_action(self.request_for(document).id, approver,
                      WorkflowStageAction.RETURNED, comment=comment)
        document.refresh_from_db()
        return document

    def resume(self, user, document):
        response = self.as_(user).post(
            f"/v1/workflow/instances/{self.request_for(document).id}/resubmit/", {},
            format="json")
        self.assertEqual(response.status_code, 200, response.data)
        document.refresh_from_db()
        return response

    def approve(self, document, approver):
        record_action(self.request_for(document).id, approver, WorkflowStageAction.APPROVED)
        document.refresh_from_db()

    def read(self, user, books, path):
        response = self.as_(user).get(self.url(books, path))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class ReturnedRequisitionTests(_ReturnedFixture):

    def forty(self, line):
        return {"lines": [{"id": line.pk, "description": "Chair", "quantity": 40,
                           "estimated_unit_price": 100_000, "expense_account": "5300"}]}

    def test_mrs_bello_cuts_60_chairs_to_40_resumes_and_it_is_approved(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        line = req.lines.get()
        read = self.read(self.bello, self.books, f"requisitions/{req.pk}/")
        self.assertEqual((read["approval_state"], read["approval_returned"]),
                         (ProcApprovalState.PENDING, True))

        edited = self.as_(self.bello).patch(
            self.url(self.books, f"requisitions/{req.pk}/"), self.forty(line), format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        req.refresh_from_db()
        self.assertEqual(req.lines.get().pk, line.pk)  # Corrected in place.
        self.assertEqual(req.estimated_total, 4_000_000)
        self.assertEqual(req.approval_state, ProcApprovalState.PENDING)

        self.resume(self.bello, req)
        self.assertEqual(req.status, DocumentStatus.PENDING_APPROVAL)
        self.assertFalse(
            self.read(self.bello, self.books, f"requisitions/{req.pk}/")["approval_returned"])
        details = str(self.request_for(req).document_details)
        self.assertIn("40", details)
        self.assertNotIn("'60'", details)

        self.approve(req, self.eze)
        self.assertEqual((req.status, req.approval_state),
                         (DocumentStatus.APPROVED, ProcApprovalState.APPROVED))

    def test_a_school_with_one_branch_corrects_and_resumes_the_same_way(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.ada, self.solo_books), self.ada, self.femi)

        edited = self.as_(self.ada).patch(
            self.url(self.solo_books, f"requisitions/{req.pk}/"),
            self.forty(req.lines.get()), format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.resume(self.ada, req)
        self.approve(req, self.femi)
        self.assertEqual((req.estimated_total, req.status), (4_000_000, DocumentStatus.APPROVED))

    def test_nobody_edits_it_while_it_is_with_its_approver_or_once_approved(self):
        req = self.raise_requisition(self.bello, self.books)
        submit_for_approval(req, actor_user=self.bello)
        url = self.url(self.books, f"requisitions/{req.pk}/")

        pending = self.as_(self.bello).patch(url, self.forty(req.lines.get()), format="json")
        self.assertEqual(pending.status_code, 400, pending.data)

        self.approve(req, self.eze)
        approved = self.as_(self.bello).patch(url, self.forty(req.lines.get()), format="json")
        self.assertEqual(approved.status_code, 400, approved.data)
        self.assertEqual(req.lines.get().quantity, 60)

    def test_a_colleague_without_the_edit_key_is_forbidden(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)

        refused = self.as_(self.chidi).patch(
            self.url(self.books, f"requisitions/{req.pk}/"), self.forty(req.lines.get()),
            format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(req.lines.get().quantity, 60)

    def test_a_colleague_with_the_key_who_did_not_send_it_is_forbidden(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)

        refused = self.as_(self.tunde).patch(
            self.url(self.books, f"requisitions/{req.pk}/"), self.forty(req.lines.get()),
            format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(req.lines.get().quantity, 60)

    def test_a_lekki_buyer_cannot_reach_ikejas_returned_requisition(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)

        refused = self.as_(self.ngozi).patch(
            self.url(self.books, f"requisitions/{req.pk}/"), self.forty(req.lines.get()),
            format="json")

        self.assertEqual(refused.status_code, 404, refused.data)
        self.assertEqual(req.lines.get().quantity, 60)

    def test_the_list_says_which_pending_rows_are_back_with_their_requester(self):
        returned = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        waiting = self.raise_requisition(self.bello, self.books, quantity=5)
        submit_for_approval(waiting, actor_user=self.bello)
        draft = self.raise_requisition(self.bello, self.books, quantity=3)

        response = self.as_(self.bello).get(self.url(self.books, "requisitions/"))

        self.assertEqual(response.status_code, 200, response.data)
        flags = {row["id"]: (row["approval_state"], row["approval_returned"])
                 for row in response.data["data"]}
        self.assertEqual(flags[returned.pk], (ProcApprovalState.PENDING, True))
        self.assertEqual(flags[waiting.pk], (ProcApprovalState.PENDING, False))
        self.assertEqual(flags[draft.pk], (ProcApprovalState.NOT_SUBMITTED, False))

    # -- one live sourcing ----------------------------------------------------- #

    def rfq_holding(self, line, status):
        rfq = RequestForQuotation.objects.create(
            entity=self.books, branch=self.ikeja, title="Chairs", issue_date=JAN,
            rfq_status=status,
        )
        RfqLine.objects.create(
            rfq=rfq, line_no=1, description="Chair", quantity=line.quantity,
            requisition_line=line, is_active=True,
            expense_account=Account.objects.get(entity=self.books, code="5300"),
        )
        return rfq

    def test_a_line_an_old_rfq_named_is_corrected_in_place_and_never_deleted(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        line = req.lines.get()
        self.rfq_holding(line, RfqStatus.CANCELLED)
        url = self.url(self.books, f"requisitions/{req.pk}/")

        replaced = self.as_(self.bello).patch(url, {"lines": [
            {"description": "Chair", "quantity": 40, "estimated_unit_price": 100_000,
             "expense_account": "5300"}]}, format="json")
        self.assertEqual(replaced.status_code, 400, replaced.data)
        self.assertIn("Line 1 ('Chair')", str(replaced.data))

        corrected = self.as_(self.bello).patch(url, self.forty(line), format="json")
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(PurchaseRequisitionLine.objects.get(pk=line.pk).quantity, 40)

    def test_a_line_a_live_rfq_holds_cannot_be_changed_or_removed(self):
        req = self.sent_and_returned(
            self.raise_requisition(self.bello, self.books), self.bello, self.eze)
        line = req.lines.get()
        rfq = self.rfq_holding(line, RfqStatus.DRAFT)
        url = self.url(self.books, f"requisitions/{req.pk}/")

        changed = self.as_(self.bello).patch(url, self.forty(line), format="json")

        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertIn(f"already on RFQ {rfq.document_number}", str(changed.data))
        self.assertEqual(PurchaseRequisitionLine.objects.get(pk=line.pk).quantity, 60)

    def test_an_approval_cannot_be_undone_while_an_rfq_holds_its_lines(self):
        req = self.raise_requisition(self.bello, self.books)
        submit_for_approval(req, actor_user=self.bello)
        self.approve(req, self.eze)
        rfq = self.rfq_holding(req.lines.get(), RfqStatus.ISSUED)

        reason = RequisitionApprovalHandler().reversal_block_reason(req)

        self.assertIsNotNone(reason)
        self.assertIn(rfq.document_number, reason)
        RequestForQuotation.objects.filter(pk=rfq.pk).update(rfq_status=RfqStatus.CANCELLED)
        self.assertIsNone(RequisitionApprovalHandler().reversal_block_reason(req))


class ReturnedVendorBillTests(_ReturnedFixture):

    def correction(self, bill, unit_price=45_000):
        return {"vendor_reference": "INV-78", "lines": [
            {"description": "Exercise books", "quantity": 1, "unit_price": unit_price,
             "expense_account": "5300"}]}

    def test_a_returned_bill_is_corrected_resumed_and_approved(self):
        bill = self.sent_and_returned(
            self.raise_bill(self.bello, self.books), self.bello, self.eze,
            comment="The supplier's invoice says 450 naira")
        self.assertTrue(
            self.read(self.bello, self.books, f"vendor-invoices/{bill.pk}/")["approval_returned"])

        edited = self.as_(self.bello).patch(
            self.url(self.books, f"vendor-invoices/{bill.pk}/"), self.correction(bill),
            format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        bill.refresh_from_db()
        self.assertEqual((bill.vendor_reference, bill.total), ("INV-78", 45_000))
        self.assertEqual(bill.approval_state, ProcApprovalState.PENDING)

        self.resume(self.bello, bill)
        self.assertFalse(
            self.read(self.bello, self.books, f"vendor-invoices/{bill.pk}/")["approval_returned"])
        self.assertIn("₦450.00", str(self.request_for(bill).document_summary))

        self.approve(bill, self.eze)
        self.assertEqual((bill.status, bill.approval_state),
                         (DocumentStatus.DRAFT, ProcApprovalState.APPROVED))

    def test_a_school_with_one_branch_corrects_and_resumes_the_same_way(self):
        bill = self.sent_and_returned(
            self.raise_bill(self.ada, self.solo_books), self.ada, self.femi)

        edited = self.as_(self.ada).patch(
            self.url(self.solo_books, f"vendor-invoices/{bill.pk}/"), self.correction(bill),
            format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.resume(self.ada, bill)
        self.approve(bill, self.femi)
        self.assertEqual((bill.total, bill.approval_state), (45_000, ProcApprovalState.APPROVED))

    def test_nobody_edits_it_while_it_is_with_its_approver_or_once_approved(self):
        bill = self.raise_bill(self.bello, self.books)
        submit_for_approval(bill, actor_user=self.bello)
        url = self.url(self.books, f"vendor-invoices/{bill.pk}/")

        pending = self.as_(self.bello).patch(url, self.correction(bill), format="json")
        self.assertEqual(pending.status_code, 400, pending.data)

        self.approve(bill, self.eze)
        approved = self.as_(self.bello).patch(url, self.correction(bill), format="json")
        self.assertEqual(approved.status_code, 400, approved.data)
        bill.refresh_from_db()
        self.assertEqual((bill.vendor_reference, bill.total), ("INV-77", 50_000))

    def test_only_its_requester_within_reach_and_holding_the_key_corrects_it(self):
        bill = self.sent_and_returned(
            self.raise_bill(self.bello, self.books), self.bello, self.eze)
        url = self.url(self.books, f"vendor-invoices/{bill.pk}/")

        for user, status in ((self.chidi, 403), (self.tunde, 403), (self.ngozi, 404)):
            refused = self.as_(user).patch(url, self.correction(bill), format="json")
            self.assertEqual(refused.status_code, status, (user.email, refused.data))
        bill.refresh_from_db()
        self.assertEqual(bill.vendor_reference, "INV-77")

    def test_a_bill_is_not_moved_onto_another_branchs_order(self):
        bill = self.sent_and_returned(
            self.raise_bill(self.adaeze, self.books, branch=self.ikeja.pk), self.adaeze, self.eze)
        lekki_order = PurchaseOrder.objects.create(
            entity=self.books, vendor=self.vendor, branch=self.lekki, order_date=JAN,
            status=DocumentStatus.APPROVED, approval_state=ProcApprovalState.APPROVED,
        )

        refused = self.as_(self.adaeze).patch(
            self.url(self.books, f"vendor-invoices/{bill.pk}/"),
            {"purchase_order": lekki_order.pk}, format="json")

        self.assertEqual(refused.status_code, 400, refused.data)
        bill.refresh_from_db()
        self.assertEqual((bill.branch_id, bill.purchase_order_id), (self.ikeja.pk, None))

    def test_a_branch_bound_clerk_cannot_attach_another_branchs_order_to_a_draft(self):
        bill = self.raise_bill(self.bello, self.books)
        lekki_order = PurchaseOrder.objects.create(
            entity=self.books, vendor=self.vendor, branch=self.lekki, order_date=JAN,
            status=DocumentStatus.APPROVED, approval_state=ProcApprovalState.APPROVED,
        )

        refused = self.as_(self.bello).patch(
            self.url(self.books, f"vendor-invoices/{bill.pk}/"),
            {"purchase_order": lekki_order.pk}, format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        bill.refresh_from_db()
        self.assertIsNone(bill.purchase_order_id)


class OtherReturnedDocumentTests(_ReturnedFixture):
    """Orders, payments and credit notes follow the same rule as requisitions and bills."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_bill = cls.posted_bill(cls.ikeja)
        cls.lekki_bill = cls.posted_bill(cls.lekki)
        cls.ikeja_bank = cls.bank("Ikeja Collections", cls.ikeja, "40")
        cls.lekki_bank = cls.bank("Lekki Collections", cls.lekki, "41")

    @classmethod
    def posted_bill(cls, branch):
        bill = VendorInvoice.objects.create(
            entity=cls.books, vendor=cls.vendor, branch=branch,
            invoice_date=JAN, due_date=JAN, total=10_000, subtotal=10_000,
            status=DocumentStatus.POSTED, approval_state=ProcApprovalState.APPROVED,
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, description="Exercise books", line_no=1, quantity=1,
            unit_price=10_000, expense_account=Account.objects.get(entity=cls.books, code="5300"),
        )
        return bill

    @classmethod
    def bank(cls, name, branch, tag):
        gl = Account.objects.create(
            entity=cls.books, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=cls.books, code="1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(entity=cls.books, name=name, branch=branch,
                                          gl_account=gl)

    def test_a_returned_order_is_corrected_and_resumed(self):
        req = self.raise_requisition(self.bello, self.books)
        submit_for_approval(req, actor_user=self.bello)
        self.approve(req, self.eze)
        order = create_po_from_requisition(req, vendor=self.vendor, order_date=JAN)
        order = self.sent_and_returned(order, self.bello, self.eze, comment="Deliver later")
        url = self.url(self.books, f"purchase-orders/{order.pk}/")
        self.assertTrue(self.read(self.bello, self.books, f"purchase-orders/{order.pk}/")[
            "approval_returned"])

        refused = self.as_(self.tunde).patch(url, {"expected_date": "2026-01-30"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        edited = self.as_(self.bello).patch(url, {"expected_date": "2026-01-30"}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)

        self.resume(self.bello, order)
        again = self.as_(self.bello).patch(url, {"expected_date": "2026-01-31"}, format="json")
        self.assertEqual(again.status_code, 400, again.data)
        self.approve(order, self.eze)
        self.assertEqual((order.expected_date, order.approval_state),
                         (datetime.date(2026, 1, 30), ProcApprovalState.APPROVED))

    def test_a_resumed_order_emails_the_vendor_it_now_names(self):
        from .models import PurchaseOrderVendorDelivery
        from .po_email import schedule_after_approval

        paper = Vendor.objects.create(
            entity=self.books, code="PAPER", name="Paper Mill", kyc_status="VERIFIED",
            payable_account=Account.objects.get(entity=self.books, code="2100"),
            email="sales@papermill.test",
        )
        req = self.raise_requisition(self.bello, self.books)
        submit_for_approval(req, actor_user=self.bello)
        self.approve(req, self.eze)
        order = create_po_from_requisition(req, vendor=self.vendor, order_date=JAN)
        schedule_after_approval(order, actor_user=self.bello)
        order = self.sent_and_returned(order, self.bello, self.eze, comment="Buy from the mill")

        edited = self.as_(self.bello).patch(
            self.url(self.books, f"purchase-orders/{order.pk}/"), {"vendor": "PAPER"},
            format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.resume(self.bello, order)

        delivery = PurchaseOrderVendorDelivery.objects.get(purchase_order=order)
        self.assertEqual((order.vendor_id, delivery.recipients),
                         (paper.pk, ["sales@papermill.test"]))

    def payment_body(self, bill, bank, narration="January stationery"):
        return {"vendor": self.vendor.pk, "payment_date": JAN.isoformat(),
                "bank_account": bank.pk, "narration": narration,
                "allocations": [{"vendor_invoice": bill.pk, "amount": 10_000}]}

    def test_a_returned_payment_is_corrected_but_keeps_its_branch(self):
        created = self.as_(self.adaeze).post(
            self.url(self.books, "vendor-payments/"),
            self.payment_body(self.ikeja_bill, self.ikeja_bank), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        payment = self.sent_and_returned(
            VendorPayment.objects.get(pk=created.data["data"]["id"]), self.adaeze, self.eze,
            comment="Say which month")
        url = self.url(self.books, f"vendor-payments/{payment.pk}/")

        moved = self.as_(self.adaeze).patch(
            url, self.payment_body(self.lekki_bill, self.lekki_bank), format="json")
        self.assertEqual(moved.status_code, 400, moved.data)
        edited = self.as_(self.adaeze).patch(
            url, self.payment_body(self.ikeja_bill, self.ikeja_bank, "Stationery, January 2026"),
            format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        payment.refresh_from_db()
        self.assertEqual((payment.branch_id, payment.approval_state),
                         (self.ikeja.pk, ProcApprovalState.PENDING))

        self.resume(self.adaeze, payment)
        self.approve(payment, self.eze)
        self.assertEqual((payment.narration, payment.approval_state),
                         ("Stationery, January 2026", ProcApprovalState.APPROVED))

    def test_a_returned_credit_note_is_corrected_and_resumed(self):
        note = VendorCreditNote.objects.create(
            entity=self.books, vendor=self.vendor, vendor_invoice=self.ikeja_bill,
            branch=self.ikeja, note_date=JAN, reason="Damaged",
        )
        VendorCreditNoteLine.objects.create(
            credit_note=note, invoice_line=self.ikeja_bill.lines.get(), net_amount=10_000,
        )
        note = self.sent_and_returned(note, self.bello, self.eze, comment="Say what was damaged")
        url = self.url(self.books, f"vendor-credit-notes/{note.pk}/")
        self.assertTrue(
            self.read(self.bello, self.books, f"vendor-credit-notes/{note.pk}/")[
                "approval_returned"])

        edited = self.as_(self.bello).patch(url, {"reason": "Ten damaged reams"}, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.resume(self.bello, note)
        self.approve(note, self.eze)
        self.assertEqual((note.reason, note.approval_state),
                         ("Ten damaged reams", ProcApprovalState.APPROVED))
        self.assertEqual(self.request_for(note).status, WorkflowInstanceStatus.APPROVED)
