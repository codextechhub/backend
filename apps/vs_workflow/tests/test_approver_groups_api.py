"""API tests for the approver group endpoints behind the Workflow Approver screen.

Security-critical cases first: permission-denied and cross-tenant isolation on
every entry point, then the group lifecycle and the live resolve preview.

Requests are driven through ``APIRequestFactory`` with the tenant attached the
way ``TenantJWTAuthentication`` attaches it, rather than minting a real JWT.
That keeps the tests focused on the view's own authorization and scoping, and
avoids depending on a JWT/crypto stack at test time.
"""
import itertools

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_workflow.constants import PERM_GROUP_MANAGE, PERM_GROUP_VIEW
from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember
from vs_workflow.views import (
    WorkflowApproverGroupViewSet, WorkflowStageApproverOverrideViewSet,
    WorkflowTemplateViewSet,
)

_counter = itertools.count(1)

BASE = "/v1/workflow/approver-groups/"

LIST   = WorkflowApproverGroupViewSet.as_view({"get": "list", "post": "create"})
DETAIL = WorkflowApproverGroupViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
RESOLVE     = WorkflowApproverGroupViewSet.as_view({"get": "resolve"})
ADD_MEMBER  = WorkflowApproverGroupViewSet.as_view({"post": "add_member"})
DEL_MEMBER  = WorkflowApproverGroupViewSet.as_view({"delete": "remove_member"})

factory = APIRequestFactory()


def _grant(user, keys):
    """Give *user* a fresh role carrying *keys* on their own tenant."""
    role = make_role(user.tenant, name=f"grp-grant-{next(_counter)}")
    for k in keys:
        make_role_permission(role, make_permission(k))
    make_assignment(user.tenant, user, role)
    return role


def _call(view, method, path, user, tenant, data=None, **view_kwargs):
    """Issue one authenticated request with the tenant context the auth layer sets."""
    request = getattr(factory, method)(path, data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    if user is not None:
        force_authenticate(request, user=user)
    return view(request, **view_kwargs)


def _body(resp):
    """Unwrap the success_response envelope when the renderer applies one."""
    data = resp.data
    if isinstance(data, dict) and "data" in data and "success" in data:
        return data["data"]
    return data


class ApproverGroupApiTests(TestCase):

    def setUp(self):
        self.school = make_school(slug="grp-api-school", name="Group API School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant

        self.manager = make_school_admin(self.branch, email="grp-manager@test.com")
        _grant(self.manager, [PERM_GROUP_MANAGE, PERM_GROUP_VIEW])
        self.viewer = make_school_admin(self.branch, email="grp-viewer@test.com")
        _grant(self.viewer, [PERM_GROUP_VIEW])
        self.nobody = make_school_admin(self.branch, email="grp-nobody@test.com")

        self.group = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="po-approvers", name="PO Approvers",
        )

    # ── Authorization ────────────────────────────────────────────────────────

    def test_anonymous_denied(self):
        resp = _call(LIST, "get", BASE, None, self.tenant)
        self.assertIn(resp.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_user_without_view_permission_denied(self):
        resp = _call(LIST, "get", BASE, self.nobody, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_permission_cannot_create(self):
        resp = _call(LIST, "post", BASE, self.viewer, self.tenant,
                     {"code": "x", "name": "X"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_permission_cannot_add_member(self):
        resp = _call(ADD_MEMBER, "post", BASE, self.viewer, self.tenant,
                     {"kind": "USER", "user": str(self.manager.pk)}, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_permission_cannot_delete(self):
        resp = _call(DETAIL, "delete", BASE, self.viewer, self.tenant, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_can_list(self):
        resp = _call(LIST, "get", BASE, self.viewer, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # ── Tenant isolation ─────────────────────────────────────────────────────

    def test_other_tenant_group_not_listed(self):
        other = make_school(slug="grp-other-school", name="Other")
        WorkflowApproverGroup.objects.create(
            tenant=other.tenant, code="secret", name="Secret Group")
        resp = _call(LIST, "get", BASE, self.manager, self.tenant)
        rows = _body(resp)
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        self.assertEqual([r["code"] for r in rows], ["po-approvers"])

    def test_other_tenant_group_detail_is_404(self):
        other = make_school(slug="grp-other-detail", name="Other")
        foreign = WorkflowApproverGroup.objects.create(
            tenant=other.tenant, code="foreign", name="Foreign")
        resp = _call(DETAIL, "get", BASE, self.manager, self.tenant, pk=foreign.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_modify_another_tenants_group(self):
        other = make_school(slug="grp-other-patch", name="Other")
        foreign = WorkflowApproverGroup.objects.create(
            tenant=other.tenant, code="foreign2", name="Foreign")
        resp = _call(DETAIL, "patch", BASE, self.manager, self.tenant,
                     {"name": "Hijacked"}, pk=foreign.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Foreign")

    def test_cannot_add_user_from_another_tenant(self):
        other = make_school(slug="grp-outsider-school", name="Outsider")
        outsider = make_school_admin(make_branch(other), email="outsider@test.com")
        resp = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                     {"kind": "USER", "user": str(outsider.pk)}, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.group.members.count(), 0)

    def test_cannot_add_role_from_another_tenant(self):
        other = make_school(slug="grp-outsider-role", name="Outsider Role")
        foreign_role = make_role(other.tenant, name="Foreign Bursar", key="foreign-bursar")
        resp = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                     {"kind": "ROLE", "role_key": foreign_role.key}, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_remove_member_of_another_tenants_group(self):
        other = make_school(slug="grp-foreign-member", name="Foreign Member")
        foreign_group = WorkflowApproverGroup.objects.create(
            tenant=other.tenant, code="fg", name="FG")
        foreign_user = make_school_admin(make_branch(other), email="fm@test.com")
        member = WorkflowApproverGroupMember.objects.create(
            group=foreign_group, kind="USER", user=foreign_user)
        resp = _call(DEL_MEMBER, "delete", BASE, self.manager, self.tenant,
                     pk=foreign_group.pk, member_id=member.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(WorkflowApproverGroupMember.objects.filter(pk=member.pk).exists())

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def test_create_group_sets_tenant_from_request(self):
        resp = _call(LIST, "post", BASE, self.manager, self.tenant,
                     {"code": "exam-board", "name": "Exam Board"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        group = WorkflowApproverGroup.all_objects.get(code="exam-board")
        self.assertEqual(group.tenant_id, self.tenant.pk)
        self.assertEqual(group.created_by_id, self.manager.pk)

    def test_create_ignores_tenant_in_payload(self):
        """Mass-assignment guard: tenant comes from the request, never the body."""
        other = make_school(slug="grp-mass-assign", name="Mass")
        resp = _call(LIST, "post", BASE, self.manager, self.tenant,
                     {"code": "ma", "name": "MA", "tenant": str(other.tenant.pk)})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            WorkflowApproverGroup.all_objects.get(code="ma").tenant_id, self.tenant.pk)

    def test_duplicate_code_rejected(self):
        resp = _call(LIST, "post", BASE, self.manager, self.tenant,
                     {"code": "po-approvers", "name": "Dupe"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_code_allowed_in_another_tenant(self):
        other = make_school(slug="grp-same-code", name="Same Code")
        admin = make_school_admin(make_branch(other), email="other-admin@test.com")
        _grant(admin, [PERM_GROUP_MANAGE, PERM_GROUP_VIEW])
        resp = _call(LIST, "post", BASE, admin, other.tenant,
                     {"code": "po-approvers", "name": "Their POs"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_code_is_immutable(self):
        resp = _call(DETAIL, "patch", BASE, self.manager, self.tenant,
                     {"code": "renamed"}, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_and_remove_user_member(self):
        member_user = make_school_admin(self.branch, email="grp-member@test.com")
        add = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                    {"kind": "USER", "user": str(member_user.pk)}, pk=self.group.pk)
        self.assertEqual(add.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.group.members.count(), 1)

        member_id = self.group.members.first().pk
        remove = _call(DEL_MEMBER, "delete", BASE, self.manager, self.tenant,
                       pk=self.group.pk, member_id=member_id)
        self.assertEqual(remove.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.members.count(), 0)

    def test_add_role_and_position_members(self):
        from vs_user.models import OrgNode, Position
        role = make_role(self.tenant, name="Bursar", key="bursar")
        node = OrgNode.objects.create(code="DV-OPS", name="Ops", kind="DIVISION")
        Position.objects.create(title="Head of Ops", code="POS-OPS", org_node=node)

        r1 = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                   {"kind": "ROLE", "role_key": "bursar"}, pk=self.group.pk)
        r2 = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                   {"kind": "POSITION", "position_code": "POS-OPS"}, pk=self.group.pk)
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertEqual({m.kind for m in self.group.members.all()}, {"ROLE", "POSITION"})
        self.assertEqual(self.group.members.get(kind="ROLE").role_id, role.pk)

    def test_add_member_requires_target_matching_kind(self):
        resp = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                     {"kind": "ROLE"}, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role_key", str(resp.data))

    def test_adding_same_member_twice_is_idempotent(self):
        member_user = make_school_admin(self.branch, email="grp-twice@test.com")
        body = {"kind": "USER", "user": str(member_user.pk)}
        first = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                      body, pk=self.group.pk)
        second = _call(ADD_MEMBER, "post", BASE, self.manager, self.tenant,
                       body, pk=self.group.pk)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group.members.count(), 1)

    def test_delete_blocked_while_a_stage_uses_the_group(self):
        from vs_workflow.models import WorkflowStage, WorkflowTemplate
        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, document_type="D", code="c", name="T")
        WorkflowStage.objects.create(
            template=template, code="s1", label="S1",
            approver_source="WORKFLOW_GROUP", approver_group=self.group)
        resp = _call(DETAIL, "delete", BASE, self.manager, self.tenant, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=self.group.pk).exists())

    def test_delete_allowed_when_unreferenced(self):
        resp = _call(DETAIL, "delete", BASE, self.manager, self.tenant, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    # ── Live resolution preview ──────────────────────────────────────────────

    def test_resolve_reports_per_member_and_total(self):
        person = make_school_admin(self.branch, email="grp-person@test.com")
        role = make_role(self.tenant, name="Bursar", key="bursar")
        role_holder = make_school_admin(self.branch, email="grp-bursar@test.com")
        make_assignment(self.tenant, role_holder, role)
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="USER", user=person)
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="ROLE", role=role)

        resp = _call(RESOLVE, "get", BASE, self.viewer, self.tenant, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = _body(resp)
        self.assertEqual(body["resolved_count"], 2)
        by_kind = {row["kind"]: row for row in body["members"]}
        self.assertEqual(by_kind["ROLE"]["resolved_count"], 1)
        self.assertEqual(by_kind["ROLE"]["target_code"], "bursar")
        self.assertEqual(by_kind["USER"]["resolved_count"], 1)

    def test_resolve_dedupes_people_reachable_twice(self):
        """Someone who is both a named member and a role holder counts once."""
        both = make_school_admin(self.branch, email="grp-both@test.com")
        role = make_role(self.tenant, name="Bursar", key="bursar")
        make_assignment(self.tenant, both, role)
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="USER", user=both)
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="ROLE", role=role)

        body = _body(_call(RESOLVE, "get", BASE, self.viewer, self.tenant, pk=self.group.pk))
        self.assertEqual(body["resolved_count"], 1)
        self.assertEqual(len(body["members"]), 2)

    def test_resolve_on_empty_group_reports_zero(self):
        body = _body(_call(RESOLVE, "get", BASE, self.viewer, self.tenant, pk=self.group.pk))
        self.assertEqual(body["resolved_count"], 0)
        self.assertEqual(body["members"], [])

    def test_resolve_denied_without_view_permission(self):
        resp = _call(RESOLVE, "get", BASE, self.nobody, self.tenant, pk=self.group.pk)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ── Dynamic role preview ─────────────────────────────────────────────────────

PREVIEW = WorkflowTemplateViewSet.as_view({"post": "preview_approvers"})


class DynamicRolePreviewTests(TestCase):
    """The builder can try rules against a sample document before publishing."""

    def setUp(self):
        self.school = make_school(slug="dyn-prev-school", name="Preview School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant

        self.builder = make_school_admin(self.branch, email="dyn-builder@test.com")
        _grant(self.builder, ["workflow.template.view", "workflow.template.manage"])

        self.officer_role = make_role(self.tenant, name="Finance Officer",
                                      key="finance-officer")
        self.bursar_role = make_role(self.tenant, name="Bursar", key="bursar")
        self.officer = make_school_admin(self.branch, email="dyn-officer@test.com")
        self.bursar = make_school_admin(self.branch, email="dyn-bursar@test.com")
        make_assignment(self.tenant, self.officer, self.officer_role)
        make_assignment(self.tenant, self.bursar, self.bursar_role)

        self.rules = [
            {"role_key": "finance-officer",
             "condition": {"op": "lt", "field": "amount", "value": 100000}},
            {"role_key": "bursar", "condition": None},
        ]

    def _preview(self, document, rules=None, user=None):
        return _call(PREVIEW, "post", "/v1/workflow/templates/preview-approvers/",
                     user or self.builder, self.tenant, {
                         "requester": str(self.builder.pk),
                         "approver_source": "DYNAMIC_ROLE",
                         "approver_scope": "SCHOOL",
                         "dynamic_role_rules": rules if rules is not None else self.rules,
                         "sample_document": document,
                     })

    def test_small_amount_previews_the_officer(self):
        resp = self._preview({"amount": 50000})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = _body(resp)
        self.assertEqual(body["dynamic_role"]["matched_role_key"], "finance-officer")
        self.assertEqual([a["user"]["id"] for a in body["approvers"]], [str(self.officer.pk)])

    def test_large_amount_previews_the_bursar(self):
        body = _body(self._preview({"amount": 250000}))
        self.assertEqual(body["dynamic_role"]["matched_role_key"], "bursar")
        self.assertEqual([a["user"]["id"] for a in body["approvers"]], [str(self.bursar.pk)])

    def test_preview_shows_why_each_rule_did_or_did_not_fire(self):
        body = _body(self._preview({"amount": 250000}))
        evals = body["dynamic_role"]["evaluations"]
        self.assertEqual([e["picked"] for e in evals], [False, True])
        self.assertEqual(evals[0]["trace"]["left"], 250000)
        self.assertFalse(evals[0]["trace"]["result"])

    def test_preview_warns_when_nothing_would_match(self):
        body = _body(self._preview(
            {"amount": 10},
            rules=[{"role_key": "bursar",
                    "condition": {"op": "gte", "field": "amount", "value": 999999}}]))
        self.assertIsNone(body["dynamic_role"]["matched_role_key"])
        self.assertEqual(body["count"], 0)
        self.assertIn("nobody", body["dynamic_role"]["note"])

    def test_bad_operator_is_a_400_not_a_500(self):
        resp = self._preview({"amount": 1}, rules=[
            {"role_key": "bursar",
             "condition": {"op": "greater", "field": "amount", "value": 1}}])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_from_another_tenant_rejected(self):
        other = make_school(slug="dyn-prev-other", name="Other")
        foreign = make_role(other.tenant, name="Foreign", key="foreign-role")
        resp = self._preview({"amount": 1},
                             rules=[{"role_key": foreign.key, "condition": None}])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rules_are_required_for_dynamic_source(self):
        resp = self._preview({"amount": 1}, rules=[])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("dynamic_role_rules", str(resp.data))


# ── Stage approver overrides ─────────────────────────────────────────────────

OVERRIDES = WorkflowStageApproverOverrideViewSet.as_view({"get": "list", "post": "create"})
OVERRIDE  = WorkflowStageApproverOverrideViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
OVR_BASE = "/v1/workflow/stage-approvers/"


class StageApproverOverrideApiTests(TestCase):
    """Tenants repoint a central step without cloning the template."""

    def setUp(self):
        from vs_workflow.models import WorkflowStage, WorkflowTemplate
        self.school = make_school(slug="ovr-api-school", name="Override School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant

        self.admin = make_school_admin(self.branch, email="ovr-admin@test.com")
        _grant(self.admin, ["workflow.template.manage", "workflow.template.view"])
        self.viewer = make_school_admin(self.branch, email="ovr-viewer@test.com")
        _grant(self.viewer, ["workflow.template.view"])

        self.central = WorkflowTemplate.all_objects.create(
            tenant=None, document_type="OVR_API_DOC", code="central", name="Central")
        self.stage = WorkflowStage.objects.create(
            template=self.central, code="approval", label="Approval",
            approver_source="ROLE", approver_role_key="central-approver")

        make_role(self.tenant, name="Our Approver", key="our-approver")

    def _create(self, user=None, **body):
        payload = {"stage": self.stage.pk, "approver_source": "ROLE",
                   "approver_role_key": "our-approver"}
        payload.update(body)
        return _call(OVERRIDES, "post", OVR_BASE, user or self.admin, self.tenant, payload)

    def test_create_override_on_a_central_stage(self):
        resp = self._create()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = _body(resp)
        self.assertEqual(body["approver_role_key"], "our-approver")
        self.assertTrue(body["is_central"])

    def test_override_takes_effect_at_resolution(self):
        from vs_workflow.services.approvers import resolve_approvers
        from vs_workflow.models import WorkflowInstance
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        approver = make_school_admin(self.branch, email="ovr-api-approver@test.com")
        make_assignment(self.tenant, approver,
                        make_role(self.tenant, name="Chosen", key="chosen-role"))
        self._create(approver_role_key="chosen-role")

        requester = make_school_admin(self.branch, email="ovr-api-req@test.com")
        instance = WorkflowInstance.objects.create(
            tenant=self.tenant, template=self.central,
            document_content_type=ContentType.objects.get_for_model(self.central),
            document_object_id="d1", document_type="OVR_API_DOC",
            status="IN_PROGRESS", requested_by=requester, submitted_at=timezone.now())
        result = resolve_approvers(self.stage, instance)
        self.assertEqual([e.user.pk for e in result], [approver.pk])

    def test_view_permission_cannot_create(self):
        resp = self._create(user=self.viewer)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_role_key_rejected(self):
        resp = self._create(approver_role_key="not-a-role")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_tenants_group_rejected(self):
        from vs_workflow.models import WorkflowApproverGroup
        other = make_school(slug="ovr-api-other", name="Other")
        foreign = WorkflowApproverGroup.objects.create(
            tenant=other.tenant, code="theirs", name="Theirs")
        resp = self._create(approver_source="WORKFLOW_GROUP",
                            approver_group=foreign.pk, approver_role_key="")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_override_another_tenants_template_stage(self):
        from vs_workflow.models import WorkflowStage, WorkflowTemplate
        other = make_school(slug="ovr-api-foreign", name="Foreign")
        foreign_tpl = WorkflowTemplate.all_objects.create(
            tenant=other.tenant, document_type="F", code="f", name="F")
        foreign_stage = WorkflowStage.objects.create(
            template=foreign_tpl, code="s", label="S",
            approver_source="ROLE", approver_role_key="x")
        resp = self._create(stage=foreign_stage.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dynamic_role_cannot_be_used_as_an_override(self):
        """Rule-based routing belongs to whoever authored the template."""
        resp = self._create(approver_source="DYNAMIC_ROLE")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_tenants_override_is_invisible(self):
        from vs_workflow.models import WorkflowStageApproverOverride
        other = make_school(slug="ovr-api-hidden", name="Hidden")
        WorkflowStageApproverOverride.objects.create(
            tenant=other.tenant, stage=self.stage,
            approver_source="ROLE", approver_role_key="whatever")
        self._create()
        rows = _body(_call(OVERRIDES, "get", OVR_BASE, self.admin, self.tenant))
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approver_role_key"], "our-approver")

    def test_removing_the_override_restores_the_template_default(self):
        override_id = _body(self._create())["id"]
        resp = _call(OVERRIDE, "delete", OVR_BASE, self.admin, self.tenant, pk=override_id)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        from vs_workflow.models import WorkflowStageApproverOverride
        self.assertFalse(WorkflowStageApproverOverride.all_objects.filter(
            tenant=self.tenant, stage=self.stage).exists())
