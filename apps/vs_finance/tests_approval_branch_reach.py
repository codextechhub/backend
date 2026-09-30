"""A money document's approval follows the document's branch, exclusively.

Corona's journal ladder has one step, approved by a group holding Mrs Bello
(Ikeja's bursar), Mrs Tola (Lekki's) and Mr Eze (the whole-school bursar). Three
journals wait: Ikeja's, Lekki's, and one raised before journals named a branch.

The engine files each approval under its document's branch and reads an approval
with no branch as the school's, which is right for a workflow template or a group
and wrong for a transaction: the unbranched journal is not shared, it is waiting
to be given its branch. So Mrs Bello sees Ikeja's journal only, Mrs Tola Lekki's
only, and Mr Eze all three, in the inbox, the dashboard count and every action.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient

from .tests_branch_scope import _FinanceBranchFixture

JAN_10 = datetime.date(2026, 1, 10)
GROUP = "journal-approvers"


class JournalApprovalsFollowTheJournalsBranchTests(_FinanceBranchFixture):

    def setUp(self):
        from vs_workflow.constants import GroupMemberKind
        from vs_workflow.models import WorkflowApproverGroupMember
        from vs_workflow.services.groups import ensure_approver_group
        from vs_workflow.services.submission import submit_for_approval
        from vs_workflow.services.templates import publish_template

        super().setUp()
        group, _ = ensure_approver_group(self.tenant, GROUP, description="Journal approvers.")
        self.bello = self.approver("bello@corona.test", self.ikeja)
        self.tola = self.approver("tola@corona.test", self.lekki)
        self.eze = self.approver("eze@corona.test", None)
        for user in (self.bello, self.tola, self.eze):
            WorkflowApproverGroupMember.objects.create(group=group, kind=GroupMemberKind.USER, user=user)
        publish_template(
            tenant=self.tenant, branch=None, document_type="finance.journal", code="standard",
            name="Journal approval",
            stages_payload=[{
                "code": "check", "label": "Check", "kind": "APPROVAL", "order": 1,
                "approver_source": "WORKFLOW_GROUP", "approver_group_code": GROUP,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,
            }],
        )
        clerk = self.user_for(self.tenant, "clerk@corona.test")
        self.journals = {name: self.draft(branch) for name, branch in (
            ("ikeja", self.ikeja), ("lekki", self.lekki), ("unbranched", None))}
        self.instances = {
            name: submit_for_approval(entry, requested_by=clerk)
            for name, entry in self.journals.items()
        }

    def approver(self, email, branch):
        return self.grant(self.user_for(self.tenant, email), "finance.journal.view",
                          tenant=self.tenant, role_key=f"role-{email}", branch=branch)

    def draft(self, branch):
        from vs_finance.posting import create_direct_entry

        return create_direct_entry(
            self.books, lines=[("1100", 10_000, 0), ("3100", 0, 10_000)], date=JAN_10,
            narration="Capital injection", branch=branch,
        )

    def inbox(self, user):
        response = TenantAPIClient(user=user).get("/v1/workflow/dashboard/pending/")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["document_object_id"] for row in response.data["results"]}

    def ids(self, *names):
        return {str(self.journals[name].pk) for name in names}

    def test_each_branch_approver_sees_only_their_branchs_journal(self):
        self.assertEqual(self.inbox(self.bello), self.ids("ikeja"))
        self.assertEqual(self.inbox(self.tola), self.ids("lekki"))

    def test_the_whole_school_approver_sees_every_journal(self):
        self.assertEqual(self.inbox(self.eze), self.ids("ikeja", "lekki", "unbranched"))

    def test_the_finance_dashboard_counts_the_same_journals(self):
        from vs_finance.dashboard_blocks import approvals_waiting_on

        self.assertEqual(approvals_waiting_on(self.books, self.bello)["total"], 1)
        self.assertEqual(approvals_waiting_on(self.books, self.eze)["total"], 3)

    def test_an_unbranched_journals_approval_cannot_be_read_or_decided_by_a_branch_approver(self):
        instance = self.instances["unbranched"]
        client = TenantAPIClient(user=self.bello)

        self.assertEqual(client.get(f"/v1/workflow/instances/{instance.pk}/").status_code, 404)
        response = client.post(f"/v1/workflow/instances/{instance.pk}/actions/",
                               {"action": "APPROVED", "comment": "ok"}, format="json")
        self.assertEqual(response.status_code, 404)
        instance.refresh_from_db()
        self.assertEqual(instance.status, "IN_PROGRESS")

    def test_a_journal_given_a_branch_after_submission_follows_it(self):
        from vs_finance.models import JournalEntry

        JournalEntry.objects.filter(pk=self.journals["unbranched"].pk).update(branch=self.ikeja)

        self.assertEqual(self.inbox(self.bello), self.ids("ikeja", "unbranched"))
        self.assertEqual(self.inbox(self.tola), self.ids("lekki"))

    def test_a_school_level_approval_keeps_the_engines_reading(self):
        from vs_workflow.handlers.registry import list_registered_handlers

        handlers = list_registered_handlers()
        for document_type in ("PLATFORM_USER_CREATION",):
            self.assertIsNone(handlers[document_type].hidden_document_ids(self.bello, self.tenant))
        self.assertIsNotNone(handlers["finance.journal"].hidden_document_ids(self.bello, self.tenant))
        self.assertIsNone(handlers["finance.journal"].hidden_document_ids(self.eze, self.tenant))

    def test_every_money_document_type_follows_its_document(self):
        from vs_procurement.constants import (
            WF_DOCTYPE_PURCHASE_ORDER, WF_DOCTYPE_REQUISITION, WF_DOCTYPE_VENDOR_INVOICE,
            WF_DOCTYPE_VENDOR_PAYMENT,
        )
        from vs_workflow.handlers.registry import list_registered_handlers

        handlers = list_registered_handlers()
        for document_type in (
            "finance.refund", "finance.write_off", "finance.concession", "finance.credit_note",
            "finance.expense_claim", WF_DOCTYPE_REQUISITION, WF_DOCTYPE_PURCHASE_ORDER,
            WF_DOCTYPE_VENDOR_INVOICE, WF_DOCTYPE_VENDOR_PAYMENT,
        ):
            with self.subTest(document_type=document_type):
                handler = handlers[document_type]
                self.assertIsNotNone(handler.hidden_document_ids(self.bello, self.tenant))
                self.assertIsNone(handler.hidden_document_ids(self.eze, self.tenant))
