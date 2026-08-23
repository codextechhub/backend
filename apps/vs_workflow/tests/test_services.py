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

def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


def _make_user(email="u@test.com"):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(tenant=_platform_tenant(), 
        email=email, first_name="Test", last_name="User",
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
    """Behaviour shared by every source: no approvers, and delegation."""

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

    def test_stage_with_no_role_key_returns_empty(self):
        stage = _make_stage(self.template)
        self.assertEqual(stage.approver_source, "ROLE")
        self.assertEqual(resolve_approvers(stage, self.instance), [])

    def test_unknown_role_key_returns_empty(self):
        stage = _make_stage(self.template, code="unknown-role")
        stage.approver_role_key = "no-such-role"
        stage.save(update_fields=["approver_role_key"])
        self.assertEqual(resolve_approvers(stage, self.instance), [])


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
        role = role if role is not None else self.role
        stage = _make_stage(self.template, code=code)
        stage.approver_source = "ROLE"
        stage.approver_role_key = role.key
        stage.approver_role = role
        stage.approver_scope = scope
        stage.save(update_fields=["approver_source", "approver_role_key",
                                  "approver_role", "approver_scope"])
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

    def test_role_key_resolves_in_the_requesting_tenant(self):
        """A central stage names a key; each tenant resolves its own role."""
        from vs_rbac.tests.helpers import make_assignment, make_role
        ours = _make_active_user("key-ours@test.com")
        self._assign(ours)
        # Another tenant has a same-keyed role with a different holder.
        from vs_rbac.tests.helpers import make_branch, make_school
        other_school = make_school(slug="key-other")
        other_role = make_role(other_school.tenant, name="Bursar", key=self.role.key)
        theirs = _make_user_in_branch("key-theirs@test.com", make_branch(other_school))
        make_assignment(other_school.tenant, theirs, other_role)

        # Key only, no FK - exactly how a central template stores it.
        stage = _make_stage(self.template, code="key-only")
        stage.approver_source = "ROLE"
        stage.approver_role_key = self.role.key
        stage.save(update_fields=["approver_source", "approver_role_key"])

        result = resolve_approvers(stage, self.instance)
        self.assertEqual([e.user.pk for e in result], [ours.pk])

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
    return get_user_model().objects.create_user(tenant=_platform_tenant(), 
        email=email, status="ACTIVE",
        first_name="Test", last_name="User",
    )


def _make_user_in_branch(email, branch):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, status="ACTIVE",
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

    def test_role_key_stored_and_anchored(self):
        t = self._publish()
        stage = t.stages.get(code="s1")
        self.assertEqual(stage.approver_role_key, "finance-officer")
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

    def test_central_template_keeps_the_role_key_without_a_tenant(self):
        """A central template names the key; there is no tenant to anchor to."""
        t = self._publish(tenant=None)
        stage = t.stages.get(code="s1")
        self.assertEqual(stage.approver_role_key, "finance-officer")
        self.assertIsNone(stage.approver_role_id)

    def test_non_role_stage_ignores_role_key(self):
        """approver_role stays empty unless the stage opts into the ROLE source."""
        t = self._publish({"approver_source": "ORGANOGRAM",
                           "organogram_target": "DIRECT_MANAGER"})
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


# ── WORKFLOW_GROUP approver source ───────────────────────────────────────────

class GroupSourceResolveApproversTests(TestCase):
    """A group resolves people, roles, and positions together into one pool."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school
        from vs_workflow.models import WorkflowApproverGroup
        self.school = make_school(slug="group-school")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.requester = _make_user_in_branch("grp-req@test.com", self.branch)
        self.template = _make_template(doc_type="GROUP_DOC")
        self.group = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="po-approvers", name="PO Approvers",
        )
        self.instance = _make_instance(self.template, self.requester)
        self.instance.branch = self.branch
        self.instance.save(update_fields=["branch"])

    def _stage(self, scope="SCHOOL", code="grp-stage", group=None):
        stage = _make_stage(self.template, code=code)
        stage.approver_source = "WORKFLOW_GROUP"
        stage.approver_group = group if group is not None else self.group
        stage.approver_scope = scope
        stage.save(update_fields=["approver_source", "approver_group", "approver_scope"])
        return stage

    def _add(self, **kwargs):
        from vs_workflow.models import WorkflowApproverGroupMember
        return WorkflowApproverGroupMember.objects.create(group=self.group, **kwargs)

    def _user(self, email):
        return _make_user_in_branch(email, self.branch)

    def _position(self, code="POS-1", title="Head of Finance", holder=None):
        from vs_user.models import OrgNode, Position, PositionAssignment
        node, _ = OrgNode.objects.get_or_create(
            code="DV-FIN", defaults={"name": "Finance Division", "kind": "DIVISION"})
        position = Position.objects.create(title=title, code=code, org_node=node)
        if holder is not None:
            PositionAssignment.objects.create(
                position=position, user=holder, is_primary=True)
        return position

    def test_user_member_resolves(self):
        alice = self._user("alice@test.com")
        self._add(kind="USER", user=alice)
        result = resolve_approvers(self._stage(), self.instance)
        self.assertEqual([e.user.pk for e in result], [alice.pk])

    def test_role_member_resolves_all_assignees(self):
        from vs_rbac.tests.helpers import make_assignment, make_role
        role = make_role(self.tenant, name="Bursar")
        a, b = self._user("bursar1@test.com"), self._user("bursar2@test.com")
        make_assignment(self.tenant, a, role)
        make_assignment(self.tenant, b, role)
        self._add(kind="ROLE", role=role)
        result = resolve_approvers(self._stage(), self.instance)
        self.assertEqual({e.user.pk for e in result}, {a.pk, b.pk})

    def test_position_member_resolves_current_holder(self):
        holder = self._user("head-fin@test.com")
        self._add(kind="POSITION", position=self._position(holder=holder))
        result = resolve_approvers(self._stage(), self.instance)
        self.assertEqual([e.user.pk for e in result], [holder.pk])

    def test_vacant_position_resolves_empty(self):
        self._add(kind="POSITION", position=self._position(code="POS-VACANT"))
        self.assertEqual(resolve_approvers(self._stage(), self.instance), [])

    def test_mixed_membership_is_unioned_and_deduped(self):
        """A person who is also a role holder appears exactly once."""
        from vs_rbac.tests.helpers import make_assignment, make_role
        both = self._user("both@test.com")
        only_user = self._user("only-user@test.com")
        holder = self._user("only-position@test.com")
        role = make_role(self.tenant, name="Bursar")
        make_assignment(self.tenant, both, role)

        self._add(kind="USER", user=both)
        self._add(kind="USER", user=only_user)
        self._add(kind="ROLE", role=role)
        self._add(kind="POSITION", position=self._position(code="POS-MIX", holder=holder))

        result = resolve_approvers(self._stage(), self.instance)
        ids = [e.user.pk for e in result]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {both.pk, only_user.pk, holder.pk})

    def test_requester_excluded_from_group(self):
        self._add(kind="USER", user=self.requester)
        self.assertEqual(resolve_approvers(self._stage(), self.instance), [])

    def test_inactive_group_resolves_empty(self):
        self._add(kind="USER", user=self._user("someone@test.com"))
        self.group.is_active = False
        self.group.save(update_fields=["is_active"])
        self.assertEqual(resolve_approvers(self._stage(), self.instance), [])

    def test_empty_group_resolves_empty(self):
        self.assertEqual(resolve_approvers(self._stage(), self.instance), [])

    def test_position_holder_outside_tenant_is_excluded(self):
        """Positions are platform-global seats - a group must never route
        approval authority to a user from another tenant."""
        outsider = _make_active_user("cx-staff@test.com")   # codex tenant
        self.assertNotEqual(outsider.tenant_id, self.tenant.pk)
        self._add(kind="POSITION", position=self._position(code="POS-CX", holder=outsider))
        self.assertEqual(resolve_approvers(self._stage(), self.instance), [])

    def test_branch_scope_narrows_role_members_only(self):
        from vs_rbac.models import TenantUserRoleAssignment
        from vs_rbac.tests.helpers import make_assignment, make_role
        role = make_role(self.tenant, name="Branch Approver")
        wide = self._user("wide-grp@test.com")
        narrow = self._user("narrow-grp@test.com")
        person = self._user("named-person@test.com")
        make_assignment(self.tenant, wide, role)
        TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=narrow, role=role, branch=self.branch,
            assignment_status="ACTIVE",
        )
        self._add(kind="ROLE", role=role)
        self._add(kind="USER", user=person)

        school_scoped = resolve_approvers(self._stage(scope="SCHOOL"), self.instance)
        self.assertEqual({e.user.pk for e in school_scoped}, {wide.pk, person.pk})

        branch_scoped = resolve_approvers(
            self._stage(scope="BRANCH", code="grp-branch"), self.instance)
        self.assertEqual({e.user.pk for e in branch_scoped},
                         {wide.pk, narrow.pk, person.pk})

    def test_delegation_expands_group_approvers(self):
        from vs_workflow.models import ApprovalDelegation
        approver = self._user("grp-delegator@test.com")
        delegate = self._user("grp-delegate@test.com")
        self._add(kind="USER", user=approver)
        now = timezone.now()
        ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=approver, delegate=delegate,
            starts_at=now - timezone.timedelta(hours=1),
            ends_at=now + timezone.timedelta(hours=1),
        )
        result = resolve_approvers(self._stage(), self.instance)
        pairs = {(e.user.pk, e.on_behalf_of.pk if e.on_behalf_of else None) for e in result}
        self.assertEqual(pairs, {(approver.pk, None), (delegate.pk, approver.pk)})

    def test_describe_group_members_explains_each_row(self):
        """The screen's per-member breakdown runs the engine's own resolution."""
        from vs_rbac.tests.helpers import make_assignment, make_role
        from vs_workflow.services.approvers import describe_group_members
        person = self._user("described@test.com")
        role = make_role(self.tenant, name="Bursar")
        make_assignment(self.tenant, self._user("bursar-x@test.com"), role)
        self._add(kind="USER", user=person)
        self._add(kind="ROLE", role=role)
        self._add(kind="POSITION", position=self._position(code="POS-DESC"))

        rows = {r["kind"]: r for r in describe_group_members(self.group, self.tenant)}
        self.assertEqual(rows["USER"]["resolved_count"], 1)
        self.assertEqual(rows["ROLE"]["resolved_count"], 1)
        self.assertEqual(rows["ROLE"]["target_code"], role.key)
        # A vacant seat is the state the screen must warn about.
        self.assertEqual(rows["POSITION"]["resolved_count"], 0)


class PublishGroupStageTests(TestCase):

    def setUp(self):
        from vs_rbac.tests.helpers import make_school
        from vs_workflow.models import WorkflowApproverGroup
        self.school = make_school(slug="pub-group-school")
        self.tenant = self.school.tenant
        self.group = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="exam-board", name="Exam Board",
        )

    def _publish(self, overrides=None, tenant="default"):
        stage = {
            "code": "s1", "label": "Board", "kind": "APPROVAL", "order": 1,
            "approver_source": "WORKFLOW_GROUP", "approver_group_code": "exam-board",
        }
        stage.update(overrides or {})
        return templates_svc.publish_template(
            tenant=self.tenant if tenant == "default" else tenant,
            document_type="GROUP_TPL", code="default", name="T",
            stages_payload=[stage],
        )

    def test_group_code_resolved_to_fk(self):
        t = self._publish()
        self.assertEqual(t.stages.get(code="s1").approver_group_id, self.group.pk)

    def test_unknown_group_code_fails_publish(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish({"approver_group_code": "nope"})

    def test_missing_group_code_fails_publish(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish({"approver_group_code": ""})

    def test_inactive_group_fails_publish(self):
        self.group.is_active = False
        self.group.save(update_fields=["is_active"])
        with self.assertRaises(TemplateInvalidError):
            self._publish()

    def test_other_tenant_group_fails_publish(self):
        from vs_rbac.tests.helpers import make_school
        other = make_school(slug="other-pub-school")
        with self.assertRaises(TemplateInvalidError):
            self._publish(tenant=other.tenant)

    def test_global_template_cannot_use_group_stage(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish(tenant=None)


class GroupMemberConstraintTests(TestCase):
    """The DB is the last line of defence on member shape."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_school
        from vs_workflow.models import WorkflowApproverGroup
        self.tenant = make_school(slug="constraint-school").tenant
        self.group = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="c", name="C")

    def test_kind_must_match_populated_target(self):
        from django.db.utils import IntegrityError
        from vs_workflow.models import WorkflowApproverGroupMember
        user = _make_active_user("mismatch@test.com")
        with self.assertRaises(IntegrityError):
            WorkflowApproverGroupMember.objects.create(
                group=self.group, kind="ROLE", user=user)

    def test_duplicate_user_member_rejected(self):
        from django.db.utils import IntegrityError
        from vs_workflow.models import WorkflowApproverGroupMember
        user = _make_active_user("dupe@test.com")
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="USER", user=user)
        with self.assertRaises(IntegrityError):
            WorkflowApproverGroupMember.objects.create(
                group=self.group, kind="USER", user=user)


# ── DYNAMIC_ROLE approver source ─────────────────────────────────────────────

class DynamicRoleResolveTests(TestCase):
    """The document picks the role; the role picks the people."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_assignment, make_branch, make_role, make_school
        self.school = make_school(slug="dyn-school")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.requester = _make_user_in_branch("dyn-req@test.com", self.branch)
        self.template = _make_template(doc_type="DYN_DOC")

        self.officer_role = make_role(self.tenant, name="Finance Officer", key="finance-officer")
        self.bursar_role = make_role(self.tenant, name="Bursar", key="bursar")
        self.officer = _make_user_in_branch("officer@test.com", self.branch)
        self.bursar = _make_user_in_branch("bursar-dyn@test.com", self.branch)
        make_assignment(self.tenant, self.officer, self.officer_role)
        make_assignment(self.tenant, self.bursar, self.bursar_role)

    def _stage(self, rules, code="dyn-stage", scope="SCHOOL"):
        from vs_workflow.models import WorkflowStageDynamicRule
        stage = _make_stage(self.template, code=code)
        stage.approver_source = "DYNAMIC_ROLE"
        stage.approver_scope = scope
        stage.save(update_fields=["approver_source", "approver_scope"])
        for i, (condition, role) in enumerate(rules):
            WorkflowStageDynamicRule.objects.create(
                stage=stage, order=i, condition=condition,
                role_key=role.key, role=role)
        return stage

    def _instance_with(self, document):
        """An instance whose resolved document is the dict *document*.

        The engine reads conditions off ``instance.document``, a
        GenericForeignKey whose descriptor insists the value match the stored
        content type, so the stand-in is installed by patching the descriptor
        for the duration of the test. The evaluator walks dicts and objects
        alike, which is what makes a plain dict a fair stand-in here.
        """
        instance = _make_instance(self.template, self.requester)
        instance.branch = self.branch
        instance.save(update_fields=["branch"])
        patcher = patch.object(WorkflowInstance, "document", document)
        patcher.start()
        self.addCleanup(patcher.stop)
        return instance

    def test_low_amount_picks_first_matching_rule(self):
        stage = self._stage([
            ({"op": "lt", "field": "amount", "value": 100000}, self.officer_role),
            (None, self.bursar_role),
        ])
        result = resolve_approvers(stage, self._instance_with({"amount": 50000}))
        self.assertEqual([e.user.pk for e in result], [self.officer.pk])

    def test_high_amount_falls_through_to_fallback(self):
        stage = self._stage([
            ({"op": "lt", "field": "amount", "value": 100000}, self.officer_role),
            (None, self.bursar_role),
        ])
        result = resolve_approvers(stage, self._instance_with({"amount": 250000}))
        self.assertEqual([e.user.pk for e in result], [self.bursar.pk])

    def test_boundary_value_uses_the_later_rule(self):
        """`lt 100000` must not match exactly 100000 - the classic off-by-one."""
        stage = self._stage([
            ({"op": "lt", "field": "amount", "value": 100000}, self.officer_role),
            (None, self.bursar_role),
        ])
        result = resolve_approvers(stage, self._instance_with({"amount": 100000}))
        self.assertEqual([e.user.pk for e in result], [self.bursar.pk])

    def test_no_match_and_no_fallback_resolves_empty(self):
        stage = self._stage([
            ({"op": "gte", "field": "amount", "value": 1000000}, self.bursar_role),
        ])
        result = resolve_approvers(stage, self._instance_with({"amount": 10}))
        self.assertEqual(result, [])

    def test_missing_field_does_not_raise(self):
        """A document without the field compares as None rather than exploding."""
        stage = self._stage([
            ({"op": "gte", "field": "amount", "value": 100}, self.bursar_role),
            (None, self.officer_role),
        ])
        result = resolve_approvers(stage, self._instance_with({"other": 1}))
        self.assertEqual([e.user.pk for e in result], [self.officer.pk])

    def test_compound_condition(self):
        stage = self._stage([
            ({"all": [
                {"op": "gte", "field": "amount", "value": 100000},
                {"op": "eq", "field": "category", "value": "capital"},
            ]}, self.bursar_role),
            (None, self.officer_role),
        ])
        both = self._instance_with({"amount": 150000, "category": "capital"})
        self.assertEqual([e.user.pk for e in resolve_approvers(stage, both)],
                         [self.bursar.pk])
        one = self._instance_with({"amount": 150000, "category": "consumable"})
        self.assertEqual([e.user.pk for e in resolve_approvers(stage, one)],
                         [self.officer.pk])

    def test_requester_excluded_from_dynamic_role(self):
        from vs_rbac.tests.helpers import make_assignment
        make_assignment(self.tenant, self.requester, self.bursar_role)
        stage = self._stage([(None, self.bursar_role)])
        ids = [e.user.pk for e in resolve_approvers(stage, self._instance_with({}))]
        self.assertNotIn(self.requester.pk, ids)
        self.assertIn(self.bursar.pk, ids)

    def test_delegation_applies_to_the_matched_role(self):
        from vs_workflow.models import ApprovalDelegation
        delegate = _make_user_in_branch("dyn-delegate@test.com", self.branch)
        now = timezone.now()
        ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=self.bursar, delegate=delegate,
            starts_at=now - timezone.timedelta(hours=1),
            ends_at=now + timezone.timedelta(hours=1))
        stage = self._stage([(None, self.bursar_role)])
        pairs = {(e.user.pk, e.on_behalf_of.pk if e.on_behalf_of else None)
                 for e in resolve_approvers(stage, self._instance_with({}))}
        self.assertEqual(pairs, {(self.bursar.pk, None), (delegate.pk, self.bursar.pk)})

    def test_match_dynamic_rule_reports_why(self):
        from vs_workflow.services.approvers import match_dynamic_rule
        stage = self._stage([
            ({"op": "lt", "field": "amount", "value": 100000}, self.officer_role),
            (None, self.bursar_role),
        ])
        rule, evaluations = match_dynamic_rule(stage, {"amount": 250000})
        self.assertEqual(rule.role_key, self.bursar_role.key)
        # The first rule was tried and rejected; evaluation stops at the match.
        self.assertEqual([e["picked"] for e in evaluations], [False, True])
        self.assertEqual(evaluations[0]["trace"]["result"], False)


class PublishDynamicRoleTests(TestCase):

    def setUp(self):
        from vs_rbac.tests.helpers import make_role, make_school
        self.school = make_school(slug="dyn-pub-school")
        self.tenant = self.school.tenant
        make_role(self.tenant, name="Bursar", key="bursar")
        make_role(self.tenant, name="Finance Officer", key="finance-officer")

    def _publish(self, rules, tenant="default", extra=None):
        stage = {
            "code": "s1", "label": "Approval", "kind": "APPROVAL", "order": 1,
            "approver_source": "DYNAMIC_ROLE", "dynamic_role_rules": rules,
        }
        stage.update(extra or {})
        return templates_svc.publish_template(
            tenant=self.tenant if tenant == "default" else tenant,
            document_type="DYN_TPL", code="default", name="T",
            stages_payload=[stage],
        )

    def test_rules_persisted_in_evaluation_order(self):
        t = self._publish([
            {"role_key": "finance-officer",
             "condition": {"op": "lt", "field": "amount", "value": 100000}},
            {"role_key": "bursar", "condition": None},
        ])
        rules = list(t.stages.get(code="s1").dynamic_rules.all())
        self.assertEqual([r.role.key for r in rules], ["finance-officer", "bursar"])
        self.assertEqual([r.order for r in rules], [0, 1])
        self.assertTrue(rules[1].is_fallback)

    def test_republish_replaces_rules(self):
        self._publish([{"role_key": "bursar", "condition": None}])
        t = self._publish([{"role_key": "finance-officer", "condition": None}])
        rules = list(t.stages.get(code="s1").dynamic_rules.all())
        self.assertEqual([r.role.key for r in rules], ["finance-officer"])

    def test_empty_rules_rejected(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish([])

    def test_unknown_role_key_rejected(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish([{"role_key": "nope", "condition": None}])

    def test_bad_operator_rejected_at_publish(self):
        """A typo'd operator must fail the publish, not the approval."""
        with self.assertRaises(TemplateInvalidError):
            self._publish([
                {"role_key": "bursar",
                 "condition": {"op": "greater_than", "field": "amount", "value": 1}},
            ])

    def test_condition_missing_field_rejected(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish([{"role_key": "bursar", "condition": {"op": "gte", "value": 1}}])

    def test_in_operator_requires_list_value(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish([
                {"role_key": "bursar",
                 "condition": {"op": "in", "field": "category", "value": "capital"}},
            ])

    def test_rule_after_fallback_rejected(self):
        """Anything after the catch-all could never fire, so it is a mistake."""
        with self.assertRaises(TemplateInvalidError):
            self._publish([
                {"role_key": "bursar", "condition": None},
                {"role_key": "finance-officer",
                 "condition": {"op": "gte", "field": "amount", "value": 1}},
            ])

    def test_central_template_keeps_dynamic_role_keys(self):
        t = self._publish([{"role_key": "bursar", "condition": None}], tenant=None)
        rule = t.stages.get(code="s1").dynamic_rules.get()
        self.assertEqual(rule.role_key, "bursar")
        self.assertIsNone(rule.role_id)

    def test_switching_source_away_drops_stale_rules(self):
        from vs_workflow.models import WorkflowStageDynamicRule
        self._publish([{"role_key": "bursar", "condition": None}])
        t = templates_svc.publish_template(
            tenant=self.tenant, document_type="DYN_TPL", code="default", name="T",
            stages_payload=[{
                "code": "s1", "label": "Approval", "kind": "APPROVAL", "order": 1,
                "approver_source": "ROLE", "approver_role_key": "bursar",
            }],
        )
        self.assertEqual(
            WorkflowStageDynamicRule.objects.filter(stage=t.stages.get(code="s1")).count(), 0)

    def test_bad_route_condition_also_rejected(self):
        """The same validation now guards route and inclusion conditions."""
        with self.assertRaises(TemplateInvalidError):
            templates_svc.publish_template(
                tenant=self.tenant, document_type="DYN_TPL2", code="default", name="T",
                stages_payload=[
                    {"code": "a", "label": "A", "order": 1},
                    {"code": "b", "label": "B", "order": 2},
                ],
                routes_payload=[{"from_stage_code": "a", "to_stage_code": "b",
                                 "condition": {"op": "bogus", "field": "x", "value": 1}}],
            )

    def test_bad_inclusion_condition_rejected(self):
        with self.assertRaises(TemplateInvalidError):
            templates_svc.publish_template(
                tenant=self.tenant, document_type="DYN_TPL3", code="default", name="T",
                stages_payload=[{"code": "a", "label": "A", "order": 1,
                                 "inclusion_condition": {"op": "nope", "field": "x"}}],
            )


# ── Tenant overrides on a central stage ──────────────────────────────────────

class StageApproverOverrideTests(TestCase):
    """A tenant repoints one step of a shared central template."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_assignment, make_branch, make_role, make_school
        self.school = make_school(slug="ovr-school")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.requester = _make_user_in_branch("ovr-req@test.com", self.branch)

        # A central template: no tenant, names its approver by key.
        self.template = WorkflowTemplate.all_objects.create(
            tenant=None, document_type="OVR_DOC", code="central", name="Central")
        self.stage = _make_stage(self.template, code="approval")
        self.stage.approver_source = "ROLE"
        self.stage.approver_role_key = "central-approver"
        self.stage.save(update_fields=["approver_source", "approver_role_key"])

        self.central_role = make_role(self.tenant, name="Central Approver",
                                      key="central-approver")
        self.default_approver = _make_user_in_branch("ovr-default@test.com", self.branch)
        make_assignment(self.tenant, self.default_approver, self.central_role)

        self.instance = _make_instance(self.template, self.requester)
        self.instance.branch = self.branch
        self.instance.save(update_fields=["branch"])

    def _override(self, **kwargs):
        from vs_workflow.models import WorkflowStageApproverOverride
        return WorkflowStageApproverOverride.objects.create(
            tenant=self.tenant, stage=self.stage, **kwargs)

    def test_without_an_override_the_central_role_applies(self):
        result = resolve_approvers(self.stage, self.instance)
        self.assertEqual([e.user.pk for e in result], [self.default_approver.pk])

    def test_override_to_another_role_wins(self):
        from vs_rbac.tests.helpers import make_assignment, make_role
        ours = make_role(self.tenant, name="Our Approver", key="our-approver")
        chosen = _make_user_in_branch("ovr-chosen@test.com", self.branch)
        make_assignment(self.tenant, chosen, ours)
        self._override(approver_source="ROLE", approver_role_key="our-approver")

        result = resolve_approvers(self.stage, self.instance)
        self.assertEqual([e.user.pk for e in result], [chosen.pk])

    def test_override_to_a_group_wins(self):
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember
        group = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="ovr-group", name="Our Committee")
        member = _make_user_in_branch("ovr-committee@test.com", self.branch)
        WorkflowApproverGroupMember.objects.create(
            group=group, kind="USER", user=member)
        self._override(approver_source="WORKFLOW_GROUP", approver_group=group)

        result = resolve_approvers(self.stage, self.instance)
        self.assertEqual([e.user.pk for e in result], [member.pk])

    def test_override_is_scoped_to_its_own_tenant(self):
        """Another tenant on the same central stage keeps the default."""
        from vs_rbac.tests.helpers import (
            make_assignment, make_branch, make_role, make_school)
        self._override(approver_source="ROLE", approver_role_key="our-approver")

        other = make_school(slug="ovr-other")
        other_branch = make_branch(other)
        other_requester = _make_user_in_branch("ovr-other-req@test.com", other_branch)
        other_role = make_role(other.tenant, name="Central Approver",
                               key="central-approver")
        other_approver = _make_user_in_branch("ovr-other-apr@test.com", other_branch)
        make_assignment(other.tenant, other_approver, other_role)

        other_instance = _make_instance(self.template, other_requester)
        result = resolve_approvers(self.stage, other_instance)
        self.assertEqual([e.user.pk for e in result], [other_approver.pk])

    def test_override_still_excludes_the_requester(self):
        from vs_rbac.tests.helpers import make_assignment, make_role
        ours = make_role(self.tenant, name="Our Approver", key="our-approver")
        make_assignment(self.tenant, self.requester, ours)
        self._override(approver_source="ROLE", approver_role_key="our-approver")
        self.assertEqual(resolve_approvers(self.stage, self.instance), [])

    def test_override_still_expands_delegation(self):
        from vs_rbac.tests.helpers import make_assignment, make_role
        from vs_workflow.models import ApprovalDelegation
        ours = make_role(self.tenant, name="Our Approver", key="our-approver")
        approver = _make_user_in_branch("ovr-delegator@test.com", self.branch)
        delegate = _make_user_in_branch("ovr-delegate@test.com", self.branch)
        make_assignment(self.tenant, approver, ours)
        now = timezone.now()
        ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=approver, delegate=delegate,
            starts_at=now - timezone.timedelta(hours=1),
            ends_at=now + timezone.timedelta(hours=1))
        self._override(approver_source="ROLE", approver_role_key="our-approver")

        pairs = {(e.user.pk, e.on_behalf_of.pk if e.on_behalf_of else None)
                 for e in resolve_approvers(self.stage, self.instance)}
        self.assertEqual(pairs, {(approver.pk, None), (delegate.pk, approver.pk)})

    def test_one_override_per_tenant_per_stage(self):
        from django.db.utils import IntegrityError
        self._override(approver_source="ROLE", approver_role_key="our-approver")
        with self.assertRaises(IntegrityError):
            self._override(approver_source="ROLE", approver_role_key="another")

    def test_override_target_must_match_its_source(self):
        from django.db.utils import IntegrityError
        with self.assertRaises(IntegrityError):
            # ROLE source with no role key is not a usable override.
            self._override(approver_source="ROLE", approver_role_key="")


# ── ORGANOGRAM approver source ───────────────────────────────────────────────

class OrganogramSourceResolutionTests(TestCase):
    """An ORGANOGRAM stage takes the live path, and parks when the climb is empty.

    :class:`~vs_workflow.services.parking.ResolutionCache` memoises exactly one
    thing: "who holds this role key in this scope". That memo is opt-in per source
    and ORGANOGRAM is deliberately not in it, because an organogram climb is
    relative to the *requester* - two documents on one page sharing a stage do not
    share an answer, so a memo keyed on the stage would be wrong rather than merely
    slow. Everything here pins that the un-memoised path stays un-memoised.

    The second half matters more than the first. Every ladder over money sets
    ``skip_if_no_approvers=False``, so a climb that reaches nobody has to leave the
    document ACTIVE and unstaffed (parked) and wait for a human. The failure mode
    being excluded is the opposite one: a silent auto-skip that walks spend to a
    terminal APPROVED decision because the requester happens to have no manager.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.school = make_school(slug="organogram-school")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.requester = _make_user_in_branch("org-req@test.com", self.branch)
        self.template = _make_template(doc_type="ORG_DOC")
        self.instance = _make_instance(self.template, self.requester)
        self.instance.branch = self.branch
        self.instance.save(update_fields=["branch"])

    # -- fixtures ------------------------------------------------------------ #

    def _node(self):
        from vs_user.models import OrgNode

        node, _ = OrgNode.objects.get_or_create(
            code="DV-ORG", defaults={"name": "Operations", "kind": "DIVISION"},
        )
        return node

    def _seat(self, code, *, title="Seat", reports_to=None, holder=None,
              primary=True):
        """One organogram seat, optionally filled.

        ``primary`` distinguishes the seat that drives the climb (a user has one)
        from a second seat the same person also occupies, which is how somebody
        ends up above themselves in the chart.
        """
        from vs_user.models import Position, PositionAssignment

        position = Position.objects.create(
            title=title, code=code, org_node=self._node(), reports_to=reports_to,
        )
        if holder is not None:
            PositionAssignment.objects.create(
                position=position, user=holder, is_primary=primary,
            )
        return position

    def _stage(self, *, code="org-stage", target="DIRECT_MANAGER",
               skip_if_no_approvers=False):
        stage = _make_stage(
            self.template, code=code, skip_if_no_approvers=skip_if_no_approvers,
        )
        stage.approver_source = "ORGANOGRAM"
        stage.organogram_target = target
        stage.approver_role_key = ""
        stage.save(update_fields=[
            "approver_source", "organogram_target", "approver_role_key",
        ])
        return stage

    def _reporting_line(self, *, manager_email=None):
        """Seat the requester under a manager seat, filled or vacant."""
        manager = (
            None if manager_email is None
            else _make_user_in_branch(manager_email, self.branch)
        )
        top = self._seat("ORG-MGR", title="Head of Operations", holder=manager)
        self._seat("ORG-STAFF", title="Officer", reports_to=top,
                   holder=self.requester)
        return manager

    # -- the live path -------------------------------------------------------- #

    def test_an_organogram_stage_resolves_live_and_is_never_memoised(self):
        """The cache must answer "I cannot say" and leave the memo untouched.

        ``_holder_ids`` returning ``None`` is what routes the stage to the live
        resolver. If it ever started answering from the role-holder memo, an
        organogram stage would be resolved once and that one answer reused for
        every requester on the page, which is not the same question.
        """
        from vs_workflow.services.parking import ResolutionCache

        manager = self._reporting_line(manager_email="org-manager@test.com")
        stage = self._stage()
        cache = ResolutionCache()

        self.assertTrue(cache.has_candidates(stage, self.instance))
        self.assertEqual(
            [approver.user.pk for approver in cache.resolve(stage, self.instance)],
            [manager.pk],
        )
        # Nothing was written into the role-holder memo: there is no role key here
        # to memoise on, and inventing one would key the answer on the wrong thing.
        self.assertEqual(cache._holders, {})

    def test_an_empty_climb_is_still_offered_to_the_live_resolver(self):
        """"Provably nobody" is a claim the memo is not entitled to make here.

        The requester holds no seat at all, so the climb reaches nobody. The cache
        must still say "resolve it live": treating an unanswerable source as
        unstaffable is what would make the repair skip the stage forever, and a
        stage the repair skips is a document that parks and never un-parks.
        """
        from vs_workflow.services.parking import ResolutionCache

        stage = self._stage(code="org-empty")
        cache = ResolutionCache()

        self.assertTrue(
            cache.has_candidates(stage, self.instance),
            "an organogram stage was written off without resolving it",
        )
        self.assertEqual(cache.resolve(stage, self.instance), [])
        self.assertEqual(cache._holders, {})

    def test_a_vacant_manager_seat_resolves_to_nobody_rather_than_raising(self):
        """A seat that exists but is empty is a normal answer, not a template fault."""
        self._reporting_line(manager_email=None)
        self.assertEqual(
            resolve_approvers(self._stage(code="org-vacant"), self.instance), [],
        )

    def test_the_requester_is_never_their_own_organogram_approver(self):
        """The climb can lead back to the submitter; self-approval still cannot.

        A small school really does put one person in two seats, so the manager
        seat above the requester's own seat is held by the requester. The climb
        finds them and the resolver must then drop them, leaving the stage parked
        rather than handing somebody their own submission to approve.
        """
        top = self._seat("ORG-SOLO", title="Principal",
                         holder=self.requester, primary=False)
        self._seat("ORG-SOLO-STAFF", title="Officer", reports_to=top,
                   holder=self.requester)

        self.assertEqual(
            resolve_approvers(self._stage(code="org-self"), self.instance), [],
        )

    # -- what an empty climb must do ------------------------------------------ #

    def test_an_unstaffed_organogram_stage_parks_instead_of_auto_approving(self):
        """The invariant that keeps spend from approving itself on an empty chart."""
        stage = self._stage(code="org-park")

        routing_svc.advance_instance(self.instance, current_attempt=1)

        self.instance.refresh_from_db()
        # Emphatically *not* APPROVED: no stage ran, so nothing has been decided.
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        stage_instance = WorkflowStageInstance.objects.get(
            instance=self.instance, stage=stage,
        )
        self.assertEqual(stage_instance.status, WorkflowStageStatus.ACTIVE)
        self.assertFalse(
            WorkflowStageApprover.objects.filter(
                stage_instance=stage_instance, attempt=stage_instance.attempt,
            ).exists(),
        )

    def test_filling_the_manager_seat_lets_the_repair_staff_a_parked_stage(self):
        """The live path is what the repair reads, so appointing somebody is enough.

        This is the organogram equivalent of granting a role after the snapshot was
        frozen: no resubmission, no new attempt, and the person appointed becomes
        the eligible approver on the attempt that is already running.
        """
        from vs_workflow.services import parking as parking_service

        stage = self._stage(code="org-repair")
        routing_svc.advance_instance(self.instance, current_attempt=1)
        stage_instance = WorkflowStageInstance.objects.get(
            instance=self.instance, stage=stage,
        )
        # Nobody to climb to yet, so the repair has nothing it can do.
        self.assertEqual(parking_service.repair_workflows(tenant=self.tenant), 0)

        manager = self._reporting_line(manager_email="org-late@test.com")

        self.assertEqual(parking_service.repair_workflows(tenant=self.tenant), 1)
        self.assertEqual(
            list(
                WorkflowStageApprover.objects
                .filter(stage_instance=stage_instance,
                        attempt=stage_instance.attempt)
                .values_list("user_id", flat=True)
            ),
            [manager.pk],
        )
        # Reachability was restored; nothing was decided or advanced.
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        stage_instance.refresh_from_db()
        self.assertEqual(stage_instance.status, WorkflowStageStatus.ACTIVE)

    # -- graceful degradation ------------------------------------------------- #

    def test_an_unavailable_organogram_degrades_to_parking_not_to_approval(self):
        """``_organogram_base_users`` swallows an ImportError; that must not approve.

        The defensive branch exists so a deployment without ``vs_user`` does not
        crash the engine. Returning an empty list there is only safe while an empty
        list means "park" - which it does for every ladder over money, because they
        all set ``skip_if_no_approvers=False``. The degraded answer is asserted next
        to the outcome so the two can never be changed apart.

        Unavailability is simulated the way Python itself reports it: a ``None``
        entry in ``sys.modules`` makes the import raise, without unloading a module
        the rest of the suite still needs.
        """
        import sys

        self._reporting_line(manager_email="org-unavailable@test.com")
        stage = self._stage(code="org-degraded")

        with patch.dict(sys.modules, {"vs_user.services.organogram": None}):
            self.assertEqual(resolve_approvers(stage, self.instance), [])
            routing_svc.advance_instance(self.instance, current_attempt=1)

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        stage_instance = WorkflowStageInstance.objects.get(
            instance=self.instance, stage=stage,
        )
        self.assertEqual(stage_instance.status, WorkflowStageStatus.ACTIVE)
        self.assertFalse(
            WorkflowStageApprover.objects.filter(
                stage_instance=stage_instance).exists(),
        )

    # -- tenant containment ---------------------------------------------------- #
    #
    # Organogram seats are platform-global: a Position belongs to the CX org
    # chart, not to a tenant. Every other approver source resolves through a
    # tenant-filtered query, so ORGANOGRAM was the one way a tenant's document
    # could be routed to somebody outside that tenant. The invariant these pin is
    # that approval authority never crosses a tenant boundary, whichever source
    # produced the candidate.

    def _outsider(self, email="org-outsider@test.com"):
        """An active user whose home tenant is not the one raising the document."""
        from vs_rbac.tests.helpers import make_branch, make_school

        other = make_school(slug=f"organogram-other-{email.split('@')[0]}")
        return _make_user_in_branch(email, make_branch(other))

    def _cx_staff(self, email):
        """An active CX staff member, whose home tenant is the codex PLATFORM one."""
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(tenant=_platform_tenant(), 
            email=email, status="ACTIVE",
            first_name="Platform", last_name="Staff",
        )

    def test_a_climb_reaching_another_tenants_user_resolves_to_nobody(self):
        """The regression: a manager seat held from outside the tenant is not eligible.

        The climb genuinely reaches the outsider - that is asserted first, so this
        can never quietly become a test of an empty organogram instead. What must
        not happen is the resolver handing them the document: their home tenant is
        not the tenant that raised it, and approval authority does not cross that
        boundary. Before the containment filter this returned the outsider.
        """
        from vs_user.services.organogram import OrganogramService

        outsider = self._outsider("org-outsider-mgr@test.com")
        top = self._seat("ORG-X-MGR", title="Outside Head", holder=outsider)
        self._seat("ORG-X-STAFF", title="Officer", reports_to=top,
                   holder=self.requester)

        # The organogram itself is happy to climb across the boundary...
        self.assertEqual(
            [u.pk for u in OrganogramService.resolve_direct_manager(self.requester)],
            [outsider.pk],
        )
        self.assertNotEqual(outsider.tenant_id, self.instance.tenant_id)

        # ...and the resolver is what refuses to act on it.
        self.assertEqual(
            resolve_approvers(self._stage(code="org-cross"), self.instance), [],
        )

    def test_a_specific_position_cannot_reach_across_the_tenant_boundary(self):
        """SPECIFIC_POSITION is the sharpest form of the same hole.

        The other three climb modes start from the requester's own seat, so a
        requester outside the CX chart reaches nobody by accident. SPECIFIC_POSITION
        ignores the requester entirely and returns the named seat's holders, so a
        tenant stage pointed at a platform seat would resolve to platform staff
        every time rather than only in odd chart shapes.
        """
        outsider = self._outsider("org-outsider-seat@test.com")
        seat = self._seat("ORG-X-SEAT", title="Group Auditor", holder=outsider)
        stage = self._stage(code="org-cross-seat", target="SPECIFIC_POSITION")
        stage.organogram_position = seat
        stage.save(update_fields=["organogram_position"])

        self.assertEqual(resolve_approvers(stage, self.instance), [])

    def test_a_same_tenant_organogram_approver_survives_containment(self):
        """Containment must remove only outsiders, never the legitimate approver.

        The counterweight to the two tests above: a filter that emptied the source
        outright would pass them both and break every real organogram ladder.
        """
        manager = self._reporting_line(manager_email="org-inside@test.com")

        self.assertEqual(manager.tenant_id, self.instance.tenant_id)
        self.assertEqual(
            [e.user.pk for e in
             resolve_approvers(self._stage(code="org-inside"), self.instance)],
            [manager.pk],
        )

    def test_a_stage_emptied_by_containment_parks_instead_of_auto_approving(self):
        """Refusing an outsider must park the document, not wave it through.

        This is the consequence that decides whether the fix is an improvement at
        all. With ``skip_if_no_approvers=False`` - what every ladder over money
        sets - a stage left with nobody has to stay ACTIVE and unstaffed and wait
        for a human. The failure mode being excluded is a stage that skips itself
        because the only candidate was disqualified, walking the document to a
        terminal decision nobody made.
        """
        outsider = self._outsider("org-outsider-park@test.com")
        top = self._seat("ORG-X-PARK", title="Outside Head", holder=outsider)
        self._seat("ORG-X-PARK-STAFF", title="Officer", reports_to=top,
                   holder=self.requester)
        stage = self._stage(code="org-cross-park")

        routing_svc.advance_instance(self.instance, current_attempt=1)

        self.instance.refresh_from_db()
        # Emphatically *not* APPROVED: the only candidate was refused, so the
        # document is waiting, not decided.
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        stage_instance = WorkflowStageInstance.objects.get(
            instance=self.instance, stage=stage,
        )
        self.assertEqual(stage_instance.status, WorkflowStageStatus.ACTIVE)
        self.assertFalse(
            WorkflowStageApprover.objects.filter(
                stage_instance=stage_instance,
                attempt=stage_instance.attempt,
            ).exists(),
        )

    def test_a_platform_tenant_climb_is_not_emptied_by_containment(self):
        """The platform ladder keeps its approvers, which is why this is safe at all.

        Containing at the shared choke point would be far worse than the bug it
        fixes if it emptied platform-scoped stages: the seeded
        ``PLATFORM_USER_CREATION`` stage sets ``skip_if_no_approvers=True``, so an
        empty resolution there does not park, it finalises the invitation
        unapproved. It cannot happen. ``WorkflowInstance.tenant`` is NOT NULL, a
        platform request carries the codex PLATFORM tenant, and CX staff derive
        that same tenant as their home, so the holders a climb finds are inside it.
        """
        requester = self._cx_staff("org-cx-req@test.com")
        manager = self._cx_staff("org-cx-mgr@test.com")
        instance = _make_instance(self.template, requester)
        top = self._seat("ORG-CX-MGR", title="Head of Platform", holder=manager)
        self._seat("ORG-CX-STAFF", title="Analyst", reports_to=top, holder=requester)

        # The premise the safety argument rests on, asserted rather than assumed.
        self.assertEqual(instance.tenant.kind, "PLATFORM")
        self.assertIsNotNone(instance.tenant_id)
        self.assertEqual(manager.tenant_id, instance.tenant_id)

        self.assertEqual(
            [e.user.pk for e in
             resolve_approvers(self._stage(code="org-cx"), instance)],
            [manager.pk],
        )


class DelegationTenantContainmentTests(TestCase):
    """A delegation cannot carry approval authority across a tenant boundary.

    ``ApprovalDelegation`` is the only way one user names another as an approver,
    and the expansion at the end of ``resolve_approvers`` used to trust the row
    outright. Its queryset filters ``tenant=instance.tenant``, which scopes the
    delegation ROW and reads like containment without being it: nothing
    constrained the user the row names, so a delegation to somebody in another
    tenant seated that outsider on the document.

    The write boundary now refuses such a row (see
    ``vs_workflow.tests.test_tenant_scoping.DelegationWriteBoundaryTests``).
    These pin the other half, which is what neutralises the rows written before
    the boundary existed - deliberately without a data migration, because
    ignoring a row at resolution is reversible and deleting somebody's delegation
    history is not.

    The counterweights matter as much as the regressions: a fix that simply
    stopped expanding delegations would pass every cross-tenant case here and
    silently break the feature.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import (
            make_assignment, make_branch, make_role, make_school,
        )

        self.school = make_school(slug="delegation-school")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.requester = _make_user_in_branch("deleg-req@test.com", self.branch)
        self.template = _make_template(doc_type="DELEG_DOC")
        self.instance = _make_instance(self.template, self.requester)

        self.role = make_role(self.tenant, name="Delegating Bursar")
        self.approver = _make_user_in_branch("deleg-approver@test.com", self.branch)
        make_assignment(self.tenant, self.approver, self.role)

    # -- fixtures ------------------------------------------------------------ #

    def _stage(self, code="deleg-stage"):
        """A plain ROLE stage the delegating approver qualifies for."""
        stage = _make_stage(self.template, code=code)
        stage.approver_source = "ROLE"
        stage.approver_role_key = self.role.key
        stage.approver_role = self.role
        stage.save(update_fields=[
            "approver_source", "approver_role_key", "approver_role",
        ])
        return stage

    def _outsider(self, email):
        """An active user whose home tenant is not the one raising the document."""
        from vs_rbac.tests.helpers import make_branch, make_school

        other = make_school(slug=f"delegation-other-{email.split('@')[0]}")
        return _make_user_in_branch(email, make_branch(other))

    def _insider(self, email):
        return _make_user_in_branch(email, self.branch)

    def _delegate_to(self, delegate, *, exclusive=False):
        """An active, unrevoked row of exactly the shape the resolver reads."""
        from vs_workflow.models import ApprovalDelegation

        now = timezone.now()
        return ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=self.approver, delegate=delegate,
            starts_at=now - timezone.timedelta(hours=1),
            ends_at=now + timezone.timedelta(hours=1),
            exclusive=exclusive,
        )

    def _assert_row_is_live(self, row):
        """Every filter the resolver applies except containment, asserted.

        Without this the cross-tenant tests could quietly decay into tests of a
        delegation that was never going to fire - an expired window or a row in
        the wrong tenant would make them pass for the wrong reason.
        """
        now = timezone.now()
        self.assertEqual(row.tenant_id, self.instance.tenant_id)
        self.assertLessEqual(row.starts_at, now)
        self.assertGreaterEqual(row.ends_at, now)
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.document_type, "")
        self.assertEqual(row.delegator_id, self.approver.pk)
        self.assertNotEqual(row.delegate_id, self.instance.requested_by_id)

    # -- the regression -------------------------------------------------------- #

    def test_a_delegate_in_another_tenant_is_not_an_eligible_approver(self):
        """The hole: an existing row hands a tenant's document to an outsider.

        Before containment reached the delegates this returned two approvers, the
        second of them a user with no relationship to the tenant that raised the
        document, acting on the delegator's behalf.
        """
        outsider = self._outsider("deleg-outsider@test.com")
        row = self._delegate_to(outsider)

        self.assertNotEqual(outsider.tenant_id, self.instance.tenant_id)
        self._assert_row_is_live(row)

        result = resolve_approvers(self._stage(), self.instance)
        self.assertEqual([e.user.pk for e in result], [self.approver.pk])
        self.assertIsNone(result[0].on_behalf_of)

    def test_an_exclusive_cross_tenant_delegation_does_not_strip_the_delegator(self):
        """A row that may not add an approver may not remove one either.

        This is what decides where containment goes in the function. Filtering
        the outsider out *after* ``excluded_delegators`` is computed would refuse
        him and still honour his row's exclusivity, leaving a stage with nobody on
        it - and with ``skip_if_no_approvers=False``, which every ladder over
        money sets, that parks a document that had a perfectly good approver all
        along. A refused row must have no effect in either direction.
        """
        outsider = self._outsider("deleg-outsider-excl@test.com")
        row = self._delegate_to(outsider, exclusive=True)
        self._assert_row_is_live(row)
        self.assertTrue(row.exclusive)

        result = resolve_approvers(self._stage(code="deleg-cross-excl"), self.instance)
        self.assertEqual([e.user.pk for e in result], [self.approver.pk])
        self.assertIsNone(result[0].on_behalf_of)

    def test_a_deactivated_delegate_is_not_an_eligible_approver(self):
        """The second thing the shared filter fixes, free of charge.

        Every base source excludes inactive users, but the delegation expansion
        checked nothing at all, so a delegate was the one route by which a closed
        account stayed eligible to approve. Routing delegates through
        ``_tenant_members`` - the single definition of "may act here" - closes
        that with the same line, which is the point of having one definition.
        """
        stand_in = self._insider("deleg-gone@test.com")
        self._delegate_to(stand_in)
        # save() re-derives is_active from status, so deactivate via status.
        stand_in.status = "DEACTIVATED"
        stand_in.save(update_fields=["status"])

        result = resolve_approvers(self._stage(code="deleg-inactive"), self.instance)
        self.assertEqual([e.user.pk for e in result], [self.approver.pk])

    # -- the counterweights ---------------------------------------------------- #

    def test_a_same_tenant_delegation_still_expands_the_approver_list(self):
        """Delegation still works, which is what makes the fix a fix.

        Both people are eligible on a non-exclusive row, and the delegate carries
        ``on_behalf_of`` so the audit trail names both.
        """
        stand_in = self._insider("deleg-standin@test.com")
        self._delegate_to(stand_in)

        self.assertEqual(stand_in.tenant_id, self.instance.tenant_id)
        result = resolve_approvers(self._stage(code="deleg-inside"), self.instance)
        pairs = {
            (e.user.pk, e.on_behalf_of.pk if e.on_behalf_of else None)
            for e in result
        }
        self.assertEqual(
            pairs, {(self.approver.pk, None), (stand_in.pk, self.approver.pk)},
        )

    def test_an_exclusive_same_tenant_delegation_still_replaces_the_delegator(self):
        """Exclusivity survives containment for a legitimate row.

        The pair with the test above: containment must not be implemented by
        dropping the exclusivity handling, and must not be applied so late that a
        valid exclusive row stops removing its delegator.
        """
        stand_in = self._insider("deleg-standin-excl@test.com")
        self._delegate_to(stand_in, exclusive=True)

        result = resolve_approvers(
            self._stage(code="deleg-inside-excl"), self.instance,
        )
        self.assertEqual(
            [(e.user.pk, e.on_behalf_of.pk) for e in result],
            [(stand_in.pk, self.approver.pk)],
        )
