"""A restricted role grant is decided by a workflow ladder.

Giving somebody a role whose restricted keys the granter does not hold raises a
``TenantRoleGrantRequest`` on the ``rbac.role_grant`` ladder instead of being
refused. The case it exists for is a school with one administrator: nobody there
holds the finance keys until a Finance Admin exists, so a refusal could never be
satisfied. These tests pin that the ladder runs, that the requester may decide
their own and is recorded as having done so, and that every entry point that
grants a role goes through it - bulk grant and replacement included.
"""
from types import SimpleNamespace

from django.test import TestCase

from schools.vs_staff.services.roles import grant_to_many
from vs_rbac.models import (
    RBACAuditLog,
    TenantRoleGrantRequest,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.services import apply_role_grant_request, grant_role
from vs_rbac.workflow_handlers import GRANT_DOCUMENT_TYPE
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.exceptions import ReversalNotAllowedError
from vs_workflow.models import WorkflowInstance
from vs_workflow.services.actions import record_action

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
)

ACTIVE = TenantUserRoleAssignment.AssignmentStatus.ACTIVE


class RoleGrantLadderTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="granting", name="Granting School")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.head = make_school_admin(self.branch, email="head@granting.test")
        self.school_admin = make_role(
            self.school, name="School Admin", key="school_admin", is_system_role=True,
        )
        make_role_permission(self.school_admin, make_permission("school.roles.approve"))
        make_assignment(self.school, self.head, self.school_admin)

        self.finance_admin = make_role(self.school, name="Finance Admin", key="finance_admin")
        make_role_permission(
            self.finance_admin,
            make_permission("finance.journal.post", is_restricted=True),
        )
        self.teacher = make_role(self.school, name="Teacher", key="teacher")
        make_role_permission(self.teacher, make_permission("students.profile.view"))

    def _grant(self, user=None, role=None, actor=None, **kwargs):
        return grant_role(
            tenant=self.tenant, user=user or self.head, role=role or self.finance_admin,
            branch=None, actor=actor or self.head, **kwargs,
        )

    def _instance(self, request):
        return WorkflowInstance.objects.for_document(request).get()

    def _holds(self, user, role):
        return TenantUserRoleAssignment.objects.filter(
            user=user, role=role, assignment_status=ACTIVE,
        ).exists()

    # ── Requesting ───────────────────────────────────────────────────────────

    def test_granting_yourself_a_restricted_role_raises_a_ladder(self):
        outcome = self._grant()

        self.assertTrue(outcome.pending)
        instance = self._instance(outcome.request)
        self.assertEqual(instance.document_type, GRANT_DOCUMENT_TYPE)
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(instance.current_stage.approver_role_key, "school_admin")
        self.assertFalse(self._holds(self.head, self.finance_admin))

    def test_a_role_without_restricted_keys_is_granted_on_the_spot(self):
        outcome = self._grant(role=self.teacher)

        self.assertFalse(outcome.pending)
        self.assertTrue(self._holds(self.head, self.teacher))

    def test_a_granter_holding_every_restricted_key_grants_directly(self):
        bursar = make_staff_user(self.branch, email="bursar@granting.test")
        make_assignment(self.school, self.head, self.finance_admin)

        outcome = self._grant(user=bursar)

        self.assertFalse(outcome.pending)
        self.assertTrue(self._holds(bursar, self.finance_admin))

    def test_the_approver_sees_who_what_and_the_restricted_keys(self):
        instance = self._instance(self._grant().request)

        self.assertEqual(instance.document_summary["subtitle"], "Role grant")
        fields, changes = instance.document_details["sections"]
        self.assertIn({"label": "Role", "value": "Finance Admin"}, fields["items"])
        self.assertEqual(len(changes["items"]), 1)
        self.assertTrue(changes["items"][0]["restricted"])

    def test_the_queue_row_names_the_document_in_words(self):
        """The inbox shows "Restricted role grant", never ``rbac.role_grant``."""
        from vs_workflow.serializers import WorkflowInstanceListSerializer

        row = WorkflowInstanceListSerializer(self._instance(self._grant().request)).data

        self.assertEqual(row["document_type_label"], "Restricted role grant")
        self.assertEqual(row["document_title"], "Finance Admin for School Admin")

    # ── Deciding ─────────────────────────────────────────────────────────────

    def test_the_only_administrator_approves_her_own_and_it_is_recorded(self):
        request = self._grant().request

        record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleGrantRequest.Status.APPROVED)
        self.assertEqual(request.reviewer, self.head)
        self.assertEqual(request.assignment.assigned_by, self.head)
        self.assertTrue(self._holds(self.head, self.finance_admin))
        log = RBACAuditLog.objects.filter(
            action_type="ROLE_ASSIGNED", metadata__grant_request_id=str(request.pk),
        ).get()
        self.assertTrue(log.metadata["self_approved"])

    def test_with_a_second_administrator_the_requester_cannot_approve_her_own(self):
        """Greenfield has Mr Bello, so Mrs Okafor's grant waits for him."""
        from vs_workflow.exceptions import NotAnEligibleApproverError
        from vs_workflow.models import WorkflowStageApprover

        deputy = make_school_admin(self.branch, email="bello@granting.test")
        make_assignment(self.school, deputy, self.school_admin)
        request = self._grant().request

        eligible = set(
            WorkflowStageApprover.objects
            .filter(stage_instance__instance=self._instance(request))
            .values_list("user_id", flat=True)
        )
        self.assertEqual(eligible, {deputy.pk})
        with self.assertRaises(NotAnEligibleApproverError):
            record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)
        self.assertFalse(self._holds(self.head, self.finance_admin))

    def test_a_second_administrator_approving_is_not_self_approval(self):
        deputy = make_school_admin(self.branch, email="deputy@granting.test")
        make_assignment(self.school, deputy, self.school_admin)
        request = self._grant().request

        record_action(self._instance(request).id, deputy, WorkflowStageAction.APPROVED)

        request.refresh_from_db()
        self.assertEqual(request.reviewer, deputy)
        self.assertEqual(request.assignment.assigned_by, self.head)
        log = RBACAuditLog.objects.filter(
            action_type="ROLE_ASSIGNED", metadata__grant_request_id=str(request.pk),
        ).get()
        self.assertFalse(log.metadata["self_approved"])

    def test_rejecting_grants_nothing(self):
        request = self._grant().request

        record_action(
            self._instance(request).id, self.head, WorkflowStageAction.REJECTED,
            comment="Not until the bursar starts.",
        )

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleGrantRequest.Status.DENIED)
        self.assertFalse(self._holds(self.head, self.finance_admin))

    def test_a_role_archived_while_waiting_is_not_granted(self):
        request = self._grant().request
        TenantRoleTemplate.objects.filter(pk=self.finance_admin.pk).update(
            status=TenantRoleTemplate.Status.ARCHIVED,
        )

        apply_role_grant_request(request, reviewer=self.head)

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleGrantRequest.Status.APPLY_FAILED)
        self.assertFalse(self._holds(self.head, self.finance_admin))

    def test_already_held_by_approval_time_writes_nothing_twice(self):
        request = self._grant().request
        make_assignment(self.school, self.head, self.finance_admin)

        apply_role_grant_request(request, reviewer=self.head)

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleGrantRequest.Status.APPROVED)
        self.assertIsNone(request.assignment)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                user=self.head, role=self.finance_admin, assignment_status=ACTIVE,
            ).count(),
            1,
        )

    def test_an_applied_grant_cannot_be_reversed_through_the_engine(self):
        request = self._grant().request
        instance = self._instance(request)
        record_action(instance.id, self.head, WorkflowStageAction.APPROVED)
        request.refresh_from_db()

        from vs_workflow.handlers.registry import get_handler

        with self.assertRaises(ReversalNotAllowedError):
            get_handler(GRANT_DOCUMENT_TYPE).validate_reversal(
                SimpleNamespace(document=request), {},
            )

    # ── Every entry point goes through it ────────────────────────────────────

    def test_replacing_a_grant_keeps_the_old_one_until_approval(self):
        clerk = make_staff_user(self.branch, email="clerk@granting.test")
        old = make_assignment(self.school, clerk, self.teacher)

        request = self._grant(user=clerk, replaces=old).request
        old.refresh_from_db()
        self.assertEqual(old.assignment_status, ACTIVE)

        record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)

        old.refresh_from_db()
        self.assertEqual(
            old.assignment_status, TenantUserRoleAssignment.AssignmentStatus.REVOKED,
        )
        self.assertTrue(self._holds(clerk, self.finance_admin))

    def test_bulk_grant_is_not_a_way_round_the_rule(self):
        """Ticking your own row on the staff list asks, the same as the profile."""
        person = SimpleNamespace(user=self.head, user_id=self.head.pk)

        granted, already, pending = grant_to_many(
            tenant=self.tenant, role=self.finance_admin, branch=None,
            people=[person], actor=self.head,
        )

        self.assertEqual((granted, already, pending), ([], [], [person]))
        self.assertFalse(self._holds(self.head, self.finance_admin))
        self.assertTrue(
            TenantRoleGrantRequest.objects.filter(
                user=self.head, status=TenantRoleGrantRequest.Status.PENDING,
            ).exists()
        )

    def test_bulk_grant_reports_somebody_already_waiting_rather_than_asking_twice(self):
        self._grant()
        person = SimpleNamespace(user=self.head, user_id=self.head.pk)

        _granted, _already, pending = grant_to_many(
            tenant=self.tenant, role=self.finance_admin, branch=None,
            people=[person], actor=self.head,
        )

        self.assertEqual(pending, [person])
        self.assertEqual(TenantRoleGrantRequest.objects.count(), 1)
