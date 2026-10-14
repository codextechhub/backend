"""A payroll run is approved by a second person before it is posted, and its preparer never can.

Corona runs one January payroll for Ikeja and Lekki. Mr Bello prepares it and
sends it; Mrs Eze, who also holds the key that posts payroll, approves it. The
route is made for the tenant the first time a run is sent, with one approval stage
through a payroll approver group that starts with the roles that can post payroll,
and the engine keeps the person who prepared or sent the run off every decision.
Harbour Primary, with one branch, goes through the same route.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, PayrollRunStatus
from vs_finance.exceptions import PayrollError
from vs_finance.models import PayrollRun
from vs_finance.payroll import post_payroll, submit_payroll_run
from vs_workflow.constants import GroupMemberKind, WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.exceptions import NotAnEligibleApproverError, RequesterCannotApproveError
from vs_workflow.models import (
    WorkflowApproverGroup,
    WorkflowApproverGroupMember,
    WorkflowInstance,
    WorkflowTemplate,
)
from vs_workflow.services import actions as wf_actions

from .approvals import ensure_tenant_payroll_approval, payroll_approval_route, payroll_run_approved
from .constants import WF_PAYROLL_APPROVER_GROUP, WF_PAYROLL_DOCUMENT_TYPE
from .tests_payroll_split import _SplitFixture


class _ApprovalFixture(_SplitFixture):
    KEYS = _SplitFixture.KEYS + ("finance.payrollrun.submit",)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # bello_user (the base fixture's) prepares; these hold the post key too, so
        # they sit in the route's group when it is made.
        cls.eze_user = cls.user_holding(cls.tenant, "payroll-eze@corona.test")
        cls.fola_user = cls.user_holding(cls.tenant, "payroll-fola@corona.test")
        cls.harbour_preparer = cls.user_holding(
            cls.solo_tenant, "payroll-prep@harbour.test", branch=None)
        cls.harbour_approver = cls.user_holding(cls.solo_tenant, "payroll-appr@harbour.test")
        cls.salary(cls.solo_books, "Solo Staff", None, gross=40_000)

    def prepared_run(self, client=None, books=None):
        response = self.generate(client, books)
        self.assertEqual(response.status_code, 201, response.data)
        return PayrollRun.objects.get(pk=response.data["data"]["id"])

    def submit(self, run, client=None, books=None, expect=200):
        response = self.act(run, "submit", client=client, books=books)
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def instance(self, run):
        return WorkflowInstance.all_objects.for_document(run).get()

    def decide(self, run, user, action=WorkflowStageAction.APPROVED, comment=""):
        return wf_actions.record_action(self.instance(run).id, user, action, comment)

    def approved_run(self, approver=None, client=None, books=None):
        run = self.prepared_run(client, books)
        self.submit(run, client, books)
        self.decide(run, approver or self.eze_user)
        run.refresh_from_db()
        return run


class DefaultRouteTests(_ApprovalFixture):
    """A tenant with no route is given one; it never means a run goes unreviewed."""

    def test_the_first_submission_makes_the_route_with_one_stage_and_a_staffed_group(self):
        self.assertIsNone(payroll_approval_route(self.tenant))
        run = self.prepared_run()
        self.submit(run)

        route = payroll_approval_route(self.tenant)
        (stage,) = route.stages.filter(retired_at__isnull=True)
        self.assertEqual((stage.approver_source, stage.skip_if_no_approvers), ("WORKFLOW_GROUP", False))
        group = WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=WF_PAYROLL_APPROVER_GROUP)
        roles = set(group.members.filter(kind=GroupMemberKind.ROLE).values_list(
            "role__key", flat=True))
        self.assertIn("role-payroll-eze@corona.test", roles)
        self.assertEqual(run.status, DocumentStatus.DRAFT)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)

    def test_making_the_route_again_changes_nothing(self):
        route, created = ensure_tenant_payroll_approval(self.tenant)
        again, created_again = ensure_tenant_payroll_approval(self.tenant)
        self.assertEqual((created, created_again, route.pk), (True, False, again.pk))
        group = WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=WF_PAYROLL_APPROVER_GROUP)
        members = group.members.count()
        ensure_tenant_payroll_approval(self.tenant)
        self.assertEqual(group.members.count(), members)

    def test_a_route_the_tenant_edited_is_left_alone(self):
        ensure_tenant_payroll_approval(self.tenant)
        group = WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=WF_PAYROLL_APPROVER_GROUP)
        group.members.all().delete()
        WorkflowApproverGroupMember.objects.create(
            group=group, kind=GroupMemberKind.USER, user=self.eze_user)
        ensure_tenant_payroll_approval(self.tenant)
        self.assertEqual(list(group.members.values_list("user_id", flat=True)), [self.eze_user.pk])

    def test_the_provisioning_hook_makes_the_route_for_new_books(self):
        from .provisioning_hooks import provision_payroll_approval

        provision_payroll_approval(self.solo_books)
        self.assertIsNotNone(payroll_approval_route(self.solo_tenant))

    def test_nobody_can_ask_to_send_a_run_without_approval(self):
        run = self.prepared_run()
        response = self.bello.post(
            f"/v1/finance/payroll-runs/{run.pk}/submit/?entity={self.books.code}",
            {"confirm_without_approval": True, "reason": "urgent"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)
        self.assertEqual(self.instance(run).status, WorkflowInstanceStatus.IN_PROGRESS)

    def test_a_route_whose_only_approver_is_the_preparer_refuses_the_submission(self):
        route, _ = ensure_tenant_payroll_approval(self.tenant)
        group = WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=WF_PAYROLL_APPROVER_GROUP)
        group.members.all().delete()
        WorkflowApproverGroupMember.objects.create(
            group=group, kind=GroupMemberKind.USER, user=self.bello_user)
        run = self.prepared_run()
        response = self.act(run, "submit")
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Nobody except the person who prepared", response.data["message"])
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.DRAFT)
        self.assertFalse(WorkflowInstance.all_objects.for_document(run).exists())


class MakerCheckerTests(_ApprovalFixture):

    def test_the_preparer_cannot_approve_even_holding_the_group_seat(self):
        run = self.prepared_run()
        self.submit(run)
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            self.decide(run, self.bello_user)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)

    def test_someone_who_prepared_the_run_cannot_approve_it_when_a_colleague_sent_it(self):
        run = self.prepared_run()  # Prepared by Mr Bello.
        sender = self.eze_user
        self.submit(run, TenantAPIClient(user=sender))
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            self.decide(run, self.bello_user)  # The preparer.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            self.decide(run, sender)  # The sender.
        self.decide(run, self.fola_user)  # A third person can.
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.APPROVED)

    def test_a_second_person_approves_and_the_run_is_approved_not_posted(self):
        run = self.approved_run()
        self.assertEqual((run.status, run.run_status),
                         (DocumentStatus.APPROVED, PayrollRunStatus.DRAFT))
        self.assertTrue(payroll_run_approved(run))
        self.assertIsNone(run.journal_id)

    def test_a_proxied_session_of_the_preparer_cannot_approve_either(self):
        from unittest.mock import patch

        run = self.prepared_run()
        self.submit(run)
        with patch("vs_workflow.services.approvers.real_person_id", return_value=self.bello_user.pk):
            with self.assertRaises(RequesterCannotApproveError):
                self.decide(run, self.eze_user)

    def test_another_tenants_approver_cannot_decide(self):
        run = self.prepared_run()
        self.submit(run)
        with self.assertRaises(NotAnEligibleApproverError):
            self.decide(run, self.harbour_approver)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)


class PostingNeedsApprovalTests(_ApprovalFixture):

    def test_posting_is_refused_before_approval_through_the_api_and_the_service(self):
        run = self.prepared_run()
        response = self.act(run, "post")
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("must be approved", response.data["message"])
        with self.assertRaises(PayrollError):
            post_payroll(run)
        self.submit(run)
        self.assertEqual(self.act(run, "post").status_code, 422)  # Still with its approvers.
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.DRAFT)

    def test_an_approved_run_posts_one_journal_per_branch(self):
        run = self.approved_run()
        response = self.act(run, "post")
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual((run.run_status, run.status), (PayrollRunStatus.POSTED, DocumentStatus.POSTED))
        self.assertEqual(run.branch_shares.count(), 2)

    def test_a_one_branch_tenant_goes_through_the_same_route(self):
        client = TenantAPIClient(user=self.harbour_preparer)
        run = self.prepared_run(client, self.solo_books)
        self.assertEqual(self.act(run, "post", client=client, books=self.solo_books).status_code, 422)
        self.submit(run, client, self.solo_books)
        self.decide(run, self.harbour_approver)
        response = self.act(run, "post", client=client, books=self.solo_books)
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertFalse(run.branch_shares.exists())
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)

    def test_a_posted_run_cannot_have_its_approval_undone(self):
        run = self.approved_run()
        self.act(run, "post")
        from vs_workflow.exceptions import ReversalNotAllowedError

        action = self.instance(run).stage_instances.first().actions.first()
        with self.assertRaises(ReversalNotAllowedError):
            wf_actions.reverse_action(action.pk, self.eze_user, "mistake")


class MultiStageRouteTests(_ApprovalFixture):
    """A tenant that adds a second stage gets it, and the preparer is kept off both."""

    def test_two_stages_each_decided_by_someone_other_than_the_preparer(self):
        from vs_workflow.services.groups import ensure_approver_group
        from vs_workflow.services.templates import publish_template

        ensure_tenant_payroll_approval(self.tenant)
        senior, _ = ensure_approver_group(self.tenant, "senior-payroll")
        senior_user = self.user_holding(self.tenant, "senior-payroll@corona.test")
        for user in (senior_user, self.bello_user):  # The preparer sits in the senior group too.
            WorkflowApproverGroupMember.objects.create(group=senior, kind=GroupMemberKind.USER, user=user)
        first = {
            "code": "payroll-approval", "label": "Payroll approval", "kind": "APPROVAL", "order": 10,
            "approver_source": "WORKFLOW_GROUP", "approver_group_code": WF_PAYROLL_APPROVER_GROUP,
            "approver_scope": "SCHOOL", "advance_rule": "ANY", "on_rejection": "RETURN_TO_REQUESTER",
            "skip_if_no_approvers": False,
        }
        second = {**first, "code": "payroll-senior", "label": "Senior payroll approval",
                  "order": 20, "approver_group_code": "senior-payroll"}
        publish_template(
            tenant=self.tenant, document_type=WF_PAYROLL_DOCUMENT_TYPE, code="standard",
            name="Payroll run approval", stages_payload=[first, second])

        run = self.prepared_run()
        self.submit(run)
        self.decide(run, self.eze_user)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)  # One stage left.
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            self.decide(run, self.bello_user)
        self.decide(run, senior_user)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.APPROVED)


class ReturnedRunTests(_ApprovalFixture):
    """Fix and resend: a returned run is corrected by whoever sent it, and nobody else."""

    def returned(self):
        run = self.prepared_run()
        self.submit(run)
        self.decide(run, self.eze_user, WorkflowStageAction.RETURNED, "Use the March label.")
        run.refresh_from_db()
        return run

    def url(self, run):
        return f"/v1/finance/payroll-runs/{run.pk}/?entity={self.books.code}"

    def test_a_returned_run_reads_returned_and_is_a_draft_again(self):
        run = self.returned()
        self.assertEqual(run.status, DocumentStatus.DRAFT)
        data = self.bello.get(self.url(run)).data["data"]
        self.assertEqual((data["approval_state"], data["approval_returned"]), ("PENDING", True))
        self.assertEqual(data["workflow_instance_id"], str(self.instance(run).pk))

    def test_only_the_sender_corrects_it_even_holding_the_create_key(self):
        run = self.returned()
        colleague = TenantAPIClient(user=self.eze_user)
        response = colleague.patch(self.url(run), {"period_label": "March 2026"}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        run.refresh_from_db()
        self.assertEqual(run.period_label, "January 2026")

        response = self.bello.patch(self.url(run), {"period_label": "March 2026"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.period_label, "March 2026")

    def test_the_sender_resumes_it_and_it_goes_back_to_its_approvers(self):
        run = self.returned()
        self.bello.patch(self.url(run), {"period_label": "March 2026"}, format="json")
        wf_actions.resubmit(self.instance(run).id, self.bello_user)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)
        self.decide(run, self.eze_user)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.APPROVED)

    def test_a_run_with_its_approvers_cannot_be_edited_or_cancelled(self):
        run = self.prepared_run()
        self.submit(run)
        self.assertEqual(
            self.bello.patch(self.url(run), {"narration": "x"}, format="json").status_code, 422)
        response = self.act(run, "cancel")
        self.assertEqual(response.status_code, 422, response.data)
        run.refresh_from_db()
        self.assertEqual(run.status, DocumentStatus.PENDING_APPROVAL)

    def test_a_draft_run_can_be_corrected_by_anyone_who_holds_the_key(self):
        run = self.prepared_run()
        response = TenantAPIClient(user=self.eze_user).patch(
            self.url(run), {"narration": "Staff pay"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_unknown_field_is_refused(self):
        run = self.prepared_run()
        self.assertEqual(
            self.bello.patch(self.url(run), {"net_total": 1}, format="json").status_code, 400)


class SubmitSecurityTests(_ApprovalFixture):

    def test_a_caller_without_the_submit_key_is_refused(self):
        run = self.prepared_run()
        poster = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "no-submit@corona.test"),
            "finance.payrollrun.view", "finance.payrollrun.post",
            tenant=self.tenant, role_key="no-submit"))
        self.assertEqual(self.act(run, "submit", client=poster).status_code, 403)
        self.assertFalse(WorkflowInstance.all_objects.for_document(run).exists())

    def test_another_tenants_run_cannot_be_submitted_or_read(self):
        run = self.prepared_run()
        rival = TenantAPIClient(user=self.grant(
            self.user_for(self.rival_tenant, "rival-payroll@rival.test"),
            *self.KEYS, tenant=self.rival_tenant, role_key="rival-payroll"))
        self.assertEqual(self.act(run, "submit", client=rival, books=self.rival_books).status_code, 404)
        self.assertEqual(rival.get(
            f"/v1/finance/payroll-runs/{run.pk}/?entity={self.rival_books.code}").status_code, 404)

    def test_a_branch_bound_caller_cannot_send_a_run_that_covers_every_branch(self):
        run = self.prepared_run()
        lekki = self.client_for(self.tenant, "lekki-submit@corona.test", branch=self.lekki)
        self.assertIn(self.act(run, "submit", client=lekki).status_code, (403, 404))
        self.assertFalse(WorkflowInstance.all_objects.for_document(run).exists())

    def test_a_run_that_could_not_post_is_refused_at_submission(self):
        self.salary(self.books, "Okon Udo", None, gross=30_000)  # Nobody has placed this person.
        run = self.prepared_run()
        response = self.act(run, "submit")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Okon Udo", response.data["message"])

    def test_the_list_and_detail_show_the_approval_state(self):
        run = self.prepared_run()
        self.submit(run)
        listed = self.bello.get(f"/v1/finance/payroll-runs/?entity={self.books.code}").data["data"]
        row = next(r for r in listed if r["id"] == run.pk)
        self.assertEqual((row["approval_state"], row["approval_returned"]), ("PENDING", False))
