"""Tests for routing, approvers, and templates services."""
from unittest.mock import MagicMock, patch
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from vs_workflow.constants import (
    StageAdvanceRule, StageKind, StageOnRejection,
    WorkflowInstanceStatus, WorkflowStageStatus,
)
from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.models import (
    WorkflowInstance, WorkflowStage, WorkflowStageApprover,
    WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.services import routing as routing_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import EligibleApprover, resolve_approvers


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(email="u@test.com"):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="CX_STAFF",
        first_name="Test", last_name="User",
    )


def _make_template(doc_type="ROUTE_DOC", code="default"):
    return WorkflowTemplate.objects.create(
        document_type=doc_type, code=code, name="Route Test Template",
    )


def _make_stage(template, code="s1", order=1, kind="APPROVAL",
                advance_rule="ANY", skip_if_no_approvers=False, on_rejection="TERMINAL"):
    return WorkflowStage.objects.create(
        template=template, code=code, label=code,
        kind=kind, order=order,
        advance_rule=advance_rule,
        on_rejection=on_rejection,
        skip_if_no_approvers=skip_if_no_approvers,
    )


def _make_instance(template, user, stage=None,
                   status=WorkflowInstanceStatus.IN_PROGRESS):
    ct = ContentType.objects.get_for_model(WorkflowTemplate)
    return WorkflowInstance.objects.create(
        tenant=user.tenant,
        template=template,
        document_content_type=ct,
        document_object_id="fdoc",
        document_type=template.document_type,
        status=status,
        requested_by=user,
        current_stage=stage,
        submitted_at=timezone.now(),
    )


# ── Routing ───────────────────────────────────────────────────────────────────

class AdvanceInstanceTests(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.template = _make_template()

    def test_terminal_instance_is_returned_unchanged(self):
        instance = _make_instance(
            self.template, self.user,
            status=WorkflowInstanceStatus.APPROVED,
        )
        result = routing_svc.advance_instance(instance, current_attempt=1)
        self.assertEqual(result.status, WorkflowInstanceStatus.APPROVED)

    def test_no_stages_raises_template_invalid(self):
        instance = _make_instance(self.template, self.user)
        with self.assertRaises(TemplateInvalidError):
            routing_svc.advance_instance(instance, current_attempt=1)

    def test_single_stage_activated_on_first_advance(self):
        stage = _make_stage(self.template)
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(instance.current_stage, stage)
        self.assertTrue(
            WorkflowStageInstance.objects.filter(
                instance=instance, stage=stage, status=WorkflowStageStatus.ACTIVE,
            ).exists()
        )

    def test_stage_skipped_when_no_approvers_and_skip_flag_set(self):
        _make_stage(self.template, skip_if_no_approvers=True)
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            # No more stages after the skipped one - should terminate APPROVED.
            with patch("vs_workflow.services.routing.get_handler") as mock_handler:
                mock_handler.return_value.on_approved.return_value = None
                routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)

    def test_retired_stage_is_skipped(self):
        stage = _make_stage(self.template)
        stage.retired_at = timezone.now()
        stage.save(update_fields=["retired_at"])
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.get_handler") as mock_handler:
            mock_handler.return_value.on_approved.return_value = None
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertTrue(
            WorkflowStageInstance.objects.filter(
                instance=instance, stage=stage, status=WorkflowStageStatus.SKIPPED,
            ).exists()
        )

    def test_two_stages_advances_to_second(self):
        s1 = _make_stage(self.template, code="s1", order=1)
        s2 = _make_stage(self.template, code="s2", order=2)
        instance = _make_instance(self.template, self.user, stage=s1)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.current_stage, s2)

    def test_all_stages_complete_terminates_approved(self):
        _make_stage(self.template)
        instance = _make_instance(self.template, self.user)
        # Simulate advancing past the only stage (current_stage already set).
        instance.current_stage = _make_stage(self.template, code="last", order=99)
        instance.save(update_fields=["current_stage"])
        with patch("vs_workflow.services.routing.get_handler") as mock_handler:
            mock_handler.return_value.on_approved.return_value = None
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertIsNone(instance.current_stage)
        self.assertIsNotNone(instance.completed_at)


# ── Approvers ─────────────────────────────────────────────────────────────────

class ResolveApproversTests(TestCase):

    def setUp(self):
        self.requester = _make_user("req@test.com")
        self.template  = _make_template()
        ct = ContentType.objects.get_for_model(WorkflowTemplate)
        self.instance = WorkflowInstance.objects.create(
            tenant=self.requester.tenant,
            template=self.template,
            document_content_type=ct,
            document_object_id="doc1",
            document_type="ROUTE_DOC",
            status=WorkflowInstanceStatus.IN_PROGRESS,
            requested_by=self.requester,
            submitted_at=timezone.now(),
        )

    def test_no_permission_key_returns_empty(self):
        stage = _make_stage(self.template)
        stage.approver_permission_key = ""
        stage.save(update_fields=["approver_permission_key"])
        result = resolve_approvers(stage, self.instance)
        self.assertEqual(result, [])

    def test_requester_excluded_from_approvers(self):
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])
        mock_qs = MagicMock()
        # .exclude() returns a mock whose .distinct() returns empty list
        mock_qs.exclude.return_value.distinct.return_value = []
        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = []
            result = resolve_approvers(stage, self.instance)
        self.assertEqual(result, [])

    def test_eligible_approver_included(self):
        approver = _make_user("aprv@test.com")
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])

        mock_qs = MagicMock()
        mock_qs.exclude.return_value = mock_qs
        mock_qs.distinct.return_value = [approver]

        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = []
            result = resolve_approvers(stage, self.instance)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].user, approver)
        self.assertIsNone(result[0].on_behalf_of)

    def test_exclusive_delegation_removes_delegator(self):
        """When a delegation is exclusive the delegator must not appear alongside the delegate."""
        delegator  = _make_user("delgtr@test.com")
        delegate   = _make_user("delgte@test.com")
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])

        mock_qs = MagicMock()
        mock_qs.exclude.return_value = mock_qs
        mock_qs.distinct.return_value = [delegator]

        mock_delegation = MagicMock()
        mock_delegation.delegator_id  = delegator.pk
        mock_delegation.delegate_id   = delegate.pk
        mock_delegation.delegator     = delegator
        mock_delegation.delegate      = delegate
        mock_delegation.exclusive     = True

        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = [mock_delegation]
            result = resolve_approvers(stage, self.instance)

        user_ids = [r.user.pk for r in result]
        self.assertIn(delegate.pk, user_ids)
        self.assertNotIn(delegator.pk, user_ids)


# ── EligibleApprover dataclass ────────────────────────────────────────────────

class EligibleApproverTests(SimpleTestCase):

    def test_on_behalf_of_defaults_to_none(self):
        user = MagicMock()
        ea = EligibleApprover(user=user)
        self.assertIsNone(ea.on_behalf_of)

    def test_fields_stored_correctly(self):
        user      = MagicMock()
        delegator = MagicMock()
        ea = EligibleApprover(user=user, on_behalf_of=delegator)
        self.assertEqual(ea.user, user)
        self.assertEqual(ea.on_behalf_of, delegator)


# ── publish_template ──────────────────────────────────────────────────────────

class PublishTemplateTests(TestCase):

    def test_create_new_template(self):
        t = templates_svc.publish_template(
            tenant=None, document_type="TPL_TEST", code="default",
            name="Test", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        self.assertEqual(WorkflowTemplate.objects.filter(
            document_type="TPL_TEST", code="default").count(), 1)
        self.assertEqual(t.stages.count(), 1)

    def test_republish_updates_fields_in_place(self):
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UPD", code="default",
            name="Original", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        t = templates_svc.publish_template(
            tenant=None, document_type="TPL_UPD", code="default",
            name="Updated", stages_payload=[
                {"code": "s1", "label": "Step 1 updated", "kind": "APPROVAL", "order": 1},
            ],
        )
        self.assertEqual(t.name, "Updated")
        self.assertEqual(t.stages.count(), 1)
        self.assertEqual(WorkflowTemplate.objects.filter(
            document_type="TPL_UPD").count(), 1)

    def test_removed_stage_is_soft_retired(self):
        """A stage absent from a republish payload must be retired, not deleted."""
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "Step 2", "kind": "APPROVAL", "order": 2},
            ],
        )
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        s2 = WorkflowStage.objects.get(
            template__document_type="TPL_RET", code="s2")
        self.assertIsNotNone(s2.retired_at)

    def test_republishing_retired_stage_reactivates_it(self):
        """Including a previously retired stage code in the payload un-retires it."""
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        # Remove s1.
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[],
        )
        # Re-include s1.
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1 back", "kind": "APPROVAL", "order": 1},
            ],
        )
        s1 = WorkflowStage.objects.get(
            template__document_type="TPL_UNRET", code="s1")
        self.assertIsNone(s1.retired_at)

    def test_routes_replaced_entirely_on_republish(self):
        """Routes have no instance-level references so they are fully replaced."""
        from vs_workflow.models import WorkflowRoutePath
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RT", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "S1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "S2", "kind": "APPROVAL", "order": 2},
            ],
            routes_payload=[
                {"from_stage_code": "s1", "to_stage_code": "s2", "order": 1},
            ],
        )
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RT", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "S1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "S2", "kind": "APPROVAL", "order": 2},
            ],
            routes_payload=[],  # intentionally cleared
        )
        tpl = WorkflowTemplate.objects.get(document_type="TPL_RT")
        self.assertEqual(WorkflowRoutePath.objects.filter(template=tpl).count(), 0)


# ── ROLE approver source ─────────────────────────────────────────────────────

class RoleSourceResolveApproversTests(TestCase):
    """resolve_approvers with approver_source=ROLE reads TenantUserRoleAssignment
    rows directly - no permission keys involved."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_role
        self.requester = _make_active_user("role-req@test.com")
        self.tenant = self.requester.tenant
        self.template = _make_template(doc_type="ROLE_DOC")
        self.role = make_role(self.tenant, name="Bursar")
        self.instance = _make_instance(self.template, self.requester)

    def _role_stage(self, role=None, scope="SCHOOL", code="role-stage"):
        stage = _make_stage(self.template, code=code)
        stage.approver_source = "ROLE"
        stage.approver_role = role if role is not None else self.role
        stage.approver_scope = scope
        stage.save(update_fields=["approver_source", "approver_role", "approver_scope"])
        return stage

    def _assign(self, user, role=None, **kwargs):
        from vs_rbac.tests.helpers import make_assignment
        return make_assignment(self.tenant, user, role or self.role, **kwargs)

    def test_active_assignee_is_eligible(self):
        approver = _make_active_user("bursar@test.com")
        self._assign(approver)
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual([e.user.pk for e in result], [approver.pk])
        self.assertIsNone(result[0].on_behalf_of)

    def test_requester_excluded_even_when_assigned(self):
        self._assign(self.requester)
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual(result, [])

    def test_revoked_assignment_not_eligible(self):
        approver = _make_active_user("ex-bursar@test.com")
        self._assign(approver, assignment_status="REVOKED")
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual(result, [])

    def test_inactive_user_not_eligible(self):
        approver = _make_active_user("gone@test.com")
        self._assign(approver)
        # save() re-derives is_active from status, so deactivate via status.
        approver.status = "DEACTIVATED"
        approver.save(update_fields=["status"])
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual(result, [])

    def test_archived_role_resolves_empty(self):
        approver = _make_active_user("archived-role@test.com")
        self._assign(approver)
        self.role.status = "ARCHIVED"
        self.role.save(update_fields=["status"])
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual(result, [])

    def test_stage_without_role_resolves_empty(self):
        stage = _make_stage(self.template, code="no-role")
        stage.approver_source = "ROLE"
        stage.save(update_fields=["approver_source"])
        self.assertEqual(resolve_approvers(stage, self.instance), [])

    def test_other_tenant_assignment_not_eligible(self):
        """A same-key role in another tenant never leaks approvers across tenants."""
        from vs_rbac.tests.helpers import make_assignment, make_branch, make_role, make_school
        school = make_school(slug="other-school")
        branch = make_branch(school)
        other_user = _make_user_in_branch("other-tenant@test.com", branch)
        other_role = make_role(school.tenant, name="Bursar")
        make_assignment(school.tenant, other_user, other_role)
        # The stage points at OUR tenant's role; the other tenant's rows are invisible.
        result = resolve_approvers(self._role_stage(), self.instance)
        self.assertEqual(result, [])

    def test_school_scope_ignores_branch_limited_assignments(self):
        """Mirrors the RBAC path: outside BRANCH scope only tenant-wide
        assignments count."""
        from vs_rbac.tests.helpers import make_branch, make_school
        school = make_school(slug="scope-school")
        branch = make_branch(school)
        requester = _make_user_in_branch("scope-req@test.com", branch)
        wide = _make_user_in_branch("wide@test.com", branch)
        narrow = _make_user_in_branch("narrow@test.com", branch)
        from vs_rbac.tests.helpers import make_assignment, make_role
        role = make_role(school.tenant, name="Branch Head")
        from vs_rbac.models import TenantUserRoleAssignment
        make_assignment(school.tenant, wide, role)  # tenant-wide
        TenantUserRoleAssignment.objects.create(   # branch-limited
            tenant=school.tenant, user=narrow, role=role, branch=branch,
            assignment_status="ACTIVE",
        )
        instance = _make_instance(self.template, requester)
        instance.branch = branch
        instance.save(update_fields=["branch"])

        stage = self._role_stage(role=role, scope="SCHOOL", code="school-scope")
        self.assertEqual({e.user.pk for e in resolve_approvers(stage, instance)},
                         {wide.pk})

        stage_b = self._role_stage(role=role, scope="BRANCH", code="branch-scope")
        self.assertEqual({e.user.pk for e in resolve_approvers(stage_b, instance)},
                         {wide.pk, narrow.pk})

    def test_delegation_expands_role_approvers(self):
        from vs_workflow.models import ApprovalDelegation
        approver = _make_active_user("delegating-bursar@test.com")
        delegate = _make_active_user("stand-in@test.com")
        self._assign(approver)
        now = timezone.now()
        ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=approver, delegate=delegate,
            starts_at=now - timezone.timedelta(hours=1),
            ends_at=now + timezone.timedelta(hours=1),
        )
        result = resolve_approvers(self._role_stage(), self.instance)
        pairs = {(e.user.pk, e.on_behalf_of.pk if e.on_behalf_of else None) for e in result}
        self.assertEqual(pairs, {(approver.pk, None), (delegate.pk, approver.pk)})


def _make_active_user(email):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="CX_STAFF", status="ACTIVE",
        first_name="Test", last_name="User",
    )


def _make_user_in_branch(email, branch):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="STAFF", status="ACTIVE",
        first_name="Branch", last_name="User", branch=branch,
    )


# ── publish_template with ROLE stages ────────────────────────────────────────

class PublishRoleStageTests(TestCase):

    def setUp(self):
        from vs_rbac.tests.helpers import make_role
        self.user = _make_user("publisher@test.com")
        self.tenant = self.user.tenant
        self.role = make_role(self.tenant, name="Finance Officer", key="finance-officer")

    def _publish(self, stage_overrides=None, tenant="default"):
        stage = {
            "code": "s1", "label": "Finance", "kind": "APPROVAL", "order": 1,
            "approver_source": "ROLE", "approver_role_key": "finance-officer",
        }
        stage.update(stage_overrides or {})
        return templates_svc.publish_template(
            tenant=self.tenant if tenant == "default" else tenant,
            document_type="ROLE_TPL", code="default", name="T",
            stages_payload=[stage],
        )

    def test_role_key_resolved_to_fk(self):
        t = self._publish()
        stage = t.stages.get(code="s1")
        self.assertEqual(stage.approver_role_id, self.role.pk)
        self.assertEqual(stage.approver_source, "ROLE")

    def test_unknown_role_key_fails_publish(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish({"approver_role_key": "no-such-role"})
        self.assertFalse(WorkflowTemplate.objects.filter(
            document_type="ROLE_TPL").exists())

    def test_missing_role_key_fails_publish(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish({"approver_role_key": ""})

    def test_inactive_role_fails_publish(self):
        self.role.status = "INACTIVE"
        self.role.save(update_fields=["status"])
        with self.assertRaises(TemplateInvalidError):
            self._publish()

    def test_global_template_cannot_use_role_stage(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish(tenant=None)

    def test_non_role_stage_ignores_role_key(self):
        """approver_role stays empty unless the stage opts into the ROLE source."""
        t = self._publish({"approver_source": "RBAC_PERMISSION",
                           "approver_permission_key": "x.y.z"})
        self.assertIsNone(t.stages.get(code="s1").approver_role_id)


# ── serializer validation for ROLE stages ────────────────────────────────────

class RoleStageSerializerValidationTests(SimpleTestCase):

    def test_publish_requires_role_key_for_role_source(self):
        from vs_workflow.serializers import WorkflowTemplatePublishSerializer
        s = WorkflowTemplatePublishSerializer(data={
            "document_type": "d", "code": "c", "name": "n",
            "stages": [{"code": "s1", "label": "S1", "approver_source": "ROLE"}],
        })
        self.assertFalse(s.is_valid())
        self.assertIn("approver_role_key", str(s.errors))

    def test_preview_requires_role_key_for_role_source(self):
        from vs_workflow.serializers import ApproverPreviewRequestSerializer
        s = ApproverPreviewRequestSerializer(data={
            "requester": "u1", "approver_source": "ROLE",
        })
        self.assertFalse(s.is_valid())
        self.assertIn("approver_role_key", str(s.errors))

    def test_preview_accepts_role_config(self):
        from vs_workflow.serializers import ApproverPreviewRequestSerializer
        s = ApproverPreviewRequestSerializer(data={
            "requester": "u1", "approver_source": "ROLE",
            "approver_role_key": "bursar",
        })
        self.assertTrue(s.is_valid(), s.errors)
