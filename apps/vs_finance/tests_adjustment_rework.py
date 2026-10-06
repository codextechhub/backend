"""A credit note or concession back from approval is corrected and sent again.

Mrs Okafor, Ikeja's bursar, raises a 50k credit note on Tunde's term bill and a
30k bursary on the same bill, and sends both to Mr Adeyemi for a second
signature. When the approval ends without approving it (Mr Adeyemi rejects it,
she withdraws it, or an administrator cancels it) each is her draft again. She
sends the credit note again through ``submit/``; she corrects the bursary to
25k with PATCH and sends it again the same way. While the approvers hold a
document the correction is refused, and a second ``submit/`` is refused while
its request is open, including one returned to her and waiting to be resumed
from her approvals: that would put a second request for the same document in
front of them. Correcting a returned document is covered in
:mod:`vs_finance.tests_returned_correction`.

Lekki's bursar reaches neither (404), a reader without the create key may not
correct one (403), Rival Group's bursar sees nothing of Corona's, and Single
Site, with one branch, corrects its own concession without naming a branch.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import cancel, record_action, withdraw

from .constants import DocumentStatus
from .models import Concession, CreditNote
from .tests_inter_branch import _InterBranchFixture

_seq = itertools.count(1)
ENDINGS = ("rejected", "withdrawn", "cancelled")
NOTE_KEYS = ("finance.creditnote.view", "finance.creditnote.create", "finance.creditnote.submit")
CONCESSION_KEYS = ("finance.concession.view", "finance.concession.create",
                   "finance.concession.submit")


class _ReworkFixture(_InterBranchFixture):

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import TenantRoleTemplate

        super().setUpTestData()
        keys = NOTE_KEYS + CONCESSION_KEYS
        cls.okafor = cls.bursar(cls.ikeja, keys=keys)
        cls.lekki_bursar = cls.bursar(cls.lekki, keys=keys)
        cls.viewer = cls.bursar(
            cls.ikeja, keys=("finance.concession.view", "finance.creditnote.view"))
        cls.solo_bursar = cls.bursar(None, tenant=cls.solo_tenant, keys=keys)
        cls.rival = cls.bursar(None, tenant=cls.rival_tenant, keys=keys)
        cls.signers = {}
        for tenant in (cls.tenant, cls.solo_tenant):
            signer = cls.user_for(tenant, f"adj-signer-{next(_seq)}@corona.test")
            cls.grant(signer, tenant=tenant, role_key="finance-approver")
            TenantRoleTemplate.objects.filter(tenant=tenant, key="finance-approver").update(
                is_system_role=True)
            cls.signers[tenant.pk] = signer
            for document_type in ("finance.credit_note", "finance.concession"):
                template, _ = WorkflowTemplate.objects.update_or_create(
                    tenant=tenant, branch=None, document_type=document_type, code="standard",
                    defaults={"name": "Second signature", "is_active": True})
                template.stages.all().delete()
                WorkflowStage.objects.create(
                    template=template, code="signer", label="Second signature", order=10,
                    approver_role_key="finance-approver")
        cls.admin = cls.user_for(cls.tenant, f"adj-admin-{next(_seq)}@corona.test")
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.bill = cls.posted_invoice(cls.tunde, cls.ikeja, amount=500_000_00)
        cls.solo_parent = cls.customer(cls.solo_books, "SOLOPARENT", cls.solo_main)

    def end(self, model, pk, how):
        instance = WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type=model.workflow_document_type,
        ).order_by("-created_at").first()
        signer = self.signers[instance.tenant_id]
        if how == "rejected":
            record_action(instance.id, signer, WorkflowStageAction.REJECTED, comment="Too much")
        elif how == "returned":
            record_action(instance.id, signer, WorkflowStageAction.RETURNED, comment="Check it")
        elif how == "withdrawn":
            withdraw(instance.id, instance.requested_by)
        else:
            cancel(instance.id, self.admin, "Raised twice")


class CreditNoteResubmitTests(_ReworkFixture):

    def raise_note(self, client):
        made = client.post(self.url("credit-notes/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "CREDIT",
            "note_date": "2026-01-16", "reason": "Overcharged for the bus",
            "lines": [{"description": "Bus fee", "revenue_account": "4100",
                       "quantity": 1, "unit_price": 50_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        sent = client.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        return pk

    def test_a_note_back_from_approval_is_submitted_again(self):
        okafor = TenantAPIClient(user=self.okafor)
        for how in ENDINGS:
            with self.subTest(how=how):
                pk = self.raise_note(okafor)

                self.end(CreditNote, pk, how)

                self.assertEqual(CreditNote.objects.get(pk=pk).status, DocumentStatus.DRAFT)
                again = okafor.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")
                self.assertEqual(again.status_code, 200, again.data)
                self.assertEqual(again.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)

    def test_a_returned_note_is_not_submitted_a_second_time(self):
        okafor = TenantAPIClient(user=self.okafor)
        pk = self.raise_note(okafor)
        self.end(CreditNote, pk, "returned")

        again = okafor.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")

        self.assertIn(again.status_code, (400, 409, 422), again.data)
        self.assertEqual(WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.credit_note").count(), 1)

    def test_another_branch_cannot_submit_ikejas_note(self):
        pk = self.raise_note(TenantAPIClient(user=self.okafor))
        self.end(CreditNote, pk, "withdrawn")

        response = TenantAPIClient(user=self.lekki_bursar).post(
            self.url(f"credit-notes/{pk}/submit/"), {}, format="json")

        self.assertEqual(response.status_code, 404, response.data)

    def test_without_the_submit_key_the_route_is_refused(self):
        pk = self.raise_note(TenantAPIClient(user=self.okafor))
        self.end(CreditNote, pk, "withdrawn")

        response = TenantAPIClient(user=self.viewer).post(
            self.url(f"credit-notes/{pk}/submit/"), {}, format="json")

        self.assertEqual(response.status_code, 403, response.data)


class ConcessionEditTests(_ReworkFixture):

    def raise_bursary(self, client, *, books=None, customer="TUNDE", invoice=None, amount=30_000_00):
        made = client.post(self.url("concessions/", books), {
            "customer": customer, "invoice": (invoice or self.bill).pk, "kind": "SCHOLARSHIP",
            "concession_date": "2026-01-16", "amount": amount, "reason": "Bursary",
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        sent = client.post(self.url(f"concessions/{pk}/submit/", books), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        return pk

    def patch(self, client, pk, body, books=None):
        return client.patch(self.url(f"concessions/{pk}/", books), body, format="json")

    def test_a_concession_back_from_approval_is_corrected_and_sent_again(self):
        okafor = TenantAPIClient(user=self.okafor)
        for how in ENDINGS:
            with self.subTest(how=how):
                pk = self.raise_bursary(okafor)
                self.end(Concession, pk, how)

                edited = self.patch(okafor, pk, {"amount": 25_000_00, "reason": "Half bursary"})

                self.assertEqual(edited.status_code, 200, edited.data)
                row = edited.data["data"]
                self.assertEqual((row["amount"], row["reason"], row["status"]),
                                 (25_000_00, "Half bursary", DocumentStatus.DRAFT))
                again = okafor.post(self.url(f"concessions/{pk}/submit/"), {}, format="json")
                self.assertEqual(again.status_code, 200, again.data)
                self.assertEqual(again.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)

    def test_a_concession_with_its_approvers_is_not_edited(self):
        okafor = TenantAPIClient(user=self.okafor)
        pk = self.raise_bursary(okafor)

        pending = self.patch(okafor, pk, {"amount": 1})

        self.assertEqual(pending.status_code, 422, pending.data)
        self.assertEqual(Concession.objects.get(pk=pk).amount, 30_000_00)

    def test_an_unknown_kind_or_a_bad_date_is_refused_on_the_field(self):
        okafor = TenantAPIClient(user=self.okafor)
        pk = self.raise_bursary(okafor)
        self.end(Concession, pk, "withdrawn")

        bad_kind = self.patch(okafor, pk, {"kind": "GIFT"})
        bad_date = self.patch(okafor, pk, {"concession_date": "soon"})

        self.assertEqual(bad_kind.status_code, 400, bad_kind.data)
        self.assertEqual(bad_date.status_code, 400, bad_date.data)

    def test_the_debt_it_discounts_is_fixed(self):
        okafor = TenantAPIClient(user=self.okafor)
        pk = self.raise_bursary(okafor)
        self.end(Concession, pk, "withdrawn")
        other = self.posted_invoice(self.tunde, self.ikeja, amount=10_000_00)

        response = self.patch(okafor, pk, {"invoice": other.pk})

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Concession.objects.get(pk=pk).invoice_id, self.bill.pk)

    def test_without_the_create_key_a_correction_is_refused(self):
        pk = self.raise_bursary(TenantAPIClient(user=self.okafor))
        self.end(Concession, pk, "withdrawn")

        response = self.patch(TenantAPIClient(user=self.viewer), pk, {"amount": 1})

        self.assertEqual(response.status_code, 403, response.data)

    def test_another_branch_and_another_school_cannot_reach_it(self):
        pk = self.raise_bursary(TenantAPIClient(user=self.okafor))
        self.end(Concession, pk, "withdrawn")

        lekki = self.patch(TenantAPIClient(user=self.lekki_bursar), pk, {"amount": 1})
        rival = TenantAPIClient(user=self.rival).patch(
            self.url(f"concessions/{pk}/", self.rival_books), {"amount": 1}, format="json")

        self.assertEqual(lekki.status_code, 404, lekki.data)
        self.assertEqual(rival.status_code, 404, rival.data)
        self.assertEqual(Concession.objects.get(pk=pk).amount, 30_000_00)

    def test_a_posted_concession_is_not_edited(self):
        from .installments import post_concession

        okafor = TenantAPIClient(user=self.okafor)
        pk = self.raise_bursary(okafor)
        self.end(Concession, pk, "withdrawn")
        WorkflowTemplate.objects.filter(
            tenant=self.tenant, document_type="finance.concession").update(is_active=False)
        post_concession(Concession.objects.get(pk=pk), actor_user=self.signers[self.tenant.pk])

        response = self.patch(okafor, pk, {"amount": 1})

        self.assertEqual(response.status_code, 422, response.data)

    def test_a_one_branch_school_corrects_its_concession(self):
        solo = TenantAPIClient(user=self.solo_bursar)
        invoice = self.posted_solo_invoice()
        pk = self.raise_bursary(solo, books=self.solo_books, customer="SOLOPARENT",
                                invoice=invoice, amount=10_000_00)
        self.end(Concession, pk, "rejected")

        edited = self.patch(solo, pk, {"amount": 8_000_00}, books=self.solo_books)

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(Concession.objects.get(pk=pk).branch_id, self.solo_main.pk)

    def posted_solo_invoice(self):
        from .models import Account, Invoice, InvoiceLine
        from .receivables import post_invoice

        invoice = Invoice.objects.create(
            entity=self.solo_books, customer=self.solo_parent, branch=self.solo_main,
            invoice_date="2026-01-10", due_date="2026-01-25",
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=50_000_00,
            revenue_account=Account.objects.get(entity=self.solo_books, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice
