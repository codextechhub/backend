"""A role change is decided by a workflow ladder.

It used to be decided by a grant ceiling: you could approve a restricted
permission only if you already held it. That refused the wrong people. A school
whose only holder of ``school.roles.approve`` is the head teacher could raise a
request for a key she did not have and then never close it, because approving it
required already holding it - and thirteen of sixteen schools have exactly one
person who can approve at all.

So the document type has a ladder, like a refund or a purchase order, and one
rule is relaxed for it that holds everywhere else: the requester may decide their
own. These tests pin both halves - that the ladder runs, and that the exemption
is narrow enough to be safe.
"""
from django.test import TestCase

from vs_rbac.models import (
    RBACAuditLog,
    TenantRoleChangeRequest,
    TenantRolePermission,
    TenantRoleTemplate,
)
from vs_rbac.services import raise_role_change_request
from vs_rbac.workflow_handlers import DOCUMENT_TYPE
from vs_workflow.constants import WorkflowStageAction, WorkflowInstanceStatus
from vs_workflow.exceptions import RequesterCannotApproveError
from vs_workflow.models import WorkflowInstance, WorkflowStageApprover
from vs_workflow.services.actions import record_action

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


def _school_admin_role(school):
    """The provisioned School Admin role the ladder resolves.

    ``is_system_role`` is what the engine checks, so that role-create cannot be
    turned into approver authority by naming a role "School Admin".
    """
    return make_role(
        school, name="School Admin", key="school_admin", is_system_role=True,
    )


class RoleChangeLadderTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="ladder", name="Ladder School")
        self.branch = make_branch(self.school)
        self.head = make_school_admin(self.branch, email="head@ladder.test")
        self.role_admin = _school_admin_role(self.school)
        make_role_permission(self.role_admin, make_permission("school.roles.approve"))
        make_assignment(self.school, self.head, self.role_admin)

        self.target = make_role(self.school, name="Bursar")
        self.restricted = make_permission("finance.payout.approve", is_restricted=True)

    def _raise(self, requester=None, key=None):
        return raise_role_change_request(
            tenant=self.school.tenant,
            requested_by=requester or self.head,
            target_role=self.target,
            justification="Ada is covering fees while Ngozi is on leave.",
            deltas=[{"permission_key": key or self.restricted.key, "operation": "ADD"}],
        )

    def _instance(self, request):
        return WorkflowInstance.objects.for_document(request).get()

    def _granted(self):
        return set(
            TenantRolePermission.objects.filter(role=self.target, granted=True)
            .values_list("permission_id", flat=True)
        )

    # ── The ladder runs ──────────────────────────────────────────────────────

    def test_raising_a_request_starts_a_ladder(self):
        request = self._raise()

        instance = self._instance(request)
        self.assertEqual(instance.document_type, DOCUMENT_TYPE)
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(instance.current_stage.approver_role_key, "school_admin")
        self.assertEqual(request.status, TenantRoleChangeRequest.Status.PENDING)

    def test_nothing_is_granted_before_the_ladder_finishes(self):
        """A request in flight has changed nobody's access."""
        self._raise()

        self.assertNotIn(self.restricted.key, self._granted())

    def test_approving_applies_the_delta(self):
        request = self._raise()

        record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleChangeRequest.Status.APPROVED)
        self.assertIn(self.restricted.key, self._granted())

    def test_rejecting_closes_the_request_and_grants_nothing(self):
        request = self._raise()

        record_action(
            self._instance(request).id, self.head, WorkflowStageAction.REJECTED,
            comment="Ada does not need this.",
        )

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleChangeRequest.Status.DENIED)
        self.assertNotIn(self.restricted.key, self._granted())

    def test_the_approver_of_record_is_whoever_voted(self):
        """Not the requester, who is the one person here who did not approve."""
        deputy = make_school_admin(self.branch, email="deputy@ladder.test")
        make_assignment(self.school, deputy, self.role_admin)
        request = self._raise()

        record_action(self._instance(request).id, deputy, WorkflowStageAction.APPROVED)

        request.refresh_from_db()
        self.assertEqual(request.reviewer, deputy)
        log = RBACAuditLog.objects.filter(
            entity_type="TenantRoleTemplate", entity_id=str(self.target.pk),
            action_type="PERMISSION_CHANGED",
        ).latest("created_at")
        self.assertEqual(log.metadata["source"], "approved_change_request")
        self.assertFalse(log.metadata["self_approved"])

    # ── The exemption, and its limits ────────────────────────────────────────

    def test_the_head_teacher_may_decide_her_own_request(self):
        """The whole reason this document type is exempt.

        Holy Cross has one person who can approve a role change, and she is the
        one who raises them. Excluding her leaves the stage with nobody on it.
        """
        request = self._raise()

        self.assertTrue(
            WorkflowStageApprover.objects.filter(
                stage_instance__instance=self._instance(request), user=self.head,
            ).exists()
        )
        record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)

        request.refresh_from_db()
        self.assertEqual(request.status, TenantRoleChangeRequest.Status.APPROVED)

    def test_self_approval_is_recorded_as_self_approval(self):
        """Allowed is not the same as unremarkable. The log has to say which."""
        request = self._raise()

        record_action(self._instance(request).id, self.head, WorkflowStageAction.APPROVED)

        log = RBACAuditLog.objects.filter(
            entity_type="TenantRoleTemplate", entity_id=str(self.target.pk),
            action_type="PERMISSION_CHANGED",
        ).latest("created_at")
        self.assertEqual(log.metadata["source"], "self_approved_change_request")
        self.assertTrue(log.metadata["self_approved"])

    def test_a_second_administrator_gets_the_ordinary_two_person_review(self):
        """The exemption does not remove the second person where there is one.

        Brightfield Lekki has sixteen people who can approve. Both are on the
        stage, so the school's own convention decides who acts - the exemption
        only stops the ladder emptying where a school has nobody else.
        """
        deputy = make_school_admin(self.branch, email="deputy2@ladder.test")
        make_assignment(self.school, deputy, self.role_admin)
        request = self._raise()

        eligible = set(
            WorkflowStageApprover.objects
            .filter(stage_instance__instance=self._instance(request))
            .values_list("user_id", flat=True)
        )
        self.assertEqual(eligible, {self.head.pk, deputy.pk})

    def test_the_exemption_does_not_leak_to_other_document_types(self):
        """The reason it is declared on a handler and not switched on globally."""
        from vs_workflow.services.approvers import requester_may_self_approve

        request = self._raise()
        instance = self._instance(request)
        self.assertTrue(requester_may_self_approve(instance))

        instance.document_type = "finance.refund"
        self.assertFalse(requester_may_self_approve(instance))

    def test_a_requester_on_an_ordinary_document_is_still_refused(self):
        """The engine's rule, unchanged, exercised end to end."""
        request = self._raise()
        instance = self._instance(request)
        # Same instance, same approver list, a document type that is not exempt.
        instance.document_type = "finance.refund"
        instance.save(update_fields=["document_type"])

        with self.assertRaises(RequesterCannotApproveError):
            record_action(instance.id, self.head, WorkflowStageAction.APPROVED)

    def test_a_look_alike_role_confers_no_approval(self):
        """Naming a role "School Admin" must not make its holders approvers.

        A tenant role's key is slugified from whatever its creator typed, so
        without the system-role boundary anybody holding role-create could mint
        approval authority with a string.
        """
        impostor = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant, key="school_admin-2", name="School Admin (Ops)",
            status="ACTIVE", is_system_role=False,
        )
        outsider = make_school_admin(self.branch, email="outsider@ladder.test")
        make_assignment(self.school, outsider, impostor)
        request = self._raise()

        self.assertFalse(
            WorkflowStageApprover.objects.filter(
                stage_instance__instance=self._instance(request), user=outsider,
            ).exists()
        )

