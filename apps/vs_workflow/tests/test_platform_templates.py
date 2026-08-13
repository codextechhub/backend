"""Platform templates, tenant versions, and the fallback between them.

The model under test: one shared definition published by the platform, which
every tenant runs until it adjusts its own. Security first (only the platform
may publish the shared one, and no tenant may touch another's), then the
cascade, then the state a screen reads to explain which version is running.
"""
import itertools

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    codex_tenant, make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin, make_vision_user,
)
from vs_workflow.constants import PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW
from vs_workflow.models import WorkflowTemplate
from vs_workflow.services.submission import WorkflowTemplate as _T  # noqa: F401
from vs_workflow.views import WorkflowTemplateViewSet

_counter = itertools.count(1)
BASE = "/v1/workflow/templates/"

LIST = WorkflowTemplateViewSet.as_view({"get": "list"})
PUBLISH = WorkflowTemplateViewSet.as_view({"post": "publish"})
USE_PLATFORM = WorkflowTemplateViewSet.as_view({"post": "use_platform_version"})

factory = APIRequestFactory()


def _grant(user, keys, tenant=None):
    role = make_role(tenant or user.tenant, name=f"tpl-grant-{next(_counter)}")
    for k in keys:
        make_role_permission(role, make_permission(k))
    make_assignment(tenant or user.tenant, user, role)
    return role


def _call(view, method, user, tenant, data=None, **kwargs):
    request = getattr(factory, method)(BASE, data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    if user is not None:
        force_authenticate(request, user=user)
    return view(request, **kwargs)


def _body(resp):
    data = resp.data
    if isinstance(data, dict) and "data" in data and "success" in data:
        return data["data"]
    return data


def _stages(role_key="approver"):
    return [{
        "code": "sign-off", "label": "Sign off", "kind": "APPROVAL", "order": 1,
        "approver_source": "ROLE", "approver_role_key": role_key,
        "approver_scope": "SCHOOL", "advance_rule": "ANY",
        "on_rejection": "TERMINAL", "skip_if_no_approvers": True,
    }]


class PlatformTemplateTests(TestCase):

    def setUp(self):
        self.codex = codex_tenant()
        self.platform_admin = make_vision_user(email=f"plat-{next(_counter)}@codex.com")
        _grant(self.platform_admin, [PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW], self.codex)

        self.school = make_school(slug=f"tpl-school-{next(_counter)}", name="Tpl School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.tenant_admin = make_school_admin(self.branch, email=f"tadm-{next(_counter)}@test.com")
        _grant(self.tenant_admin, [PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW])
        # The role every stage in these tests names, in both publishing tenants.
        make_role(self.tenant, name="Approver", key="approver")
        make_role(self.codex, name="Approver", key="approver")

    def _publish(self, user, tenant, *, scope=None, name="Spend Approval",
                 code="standard", role_key="approver"):
        payload = {"document_type": "probe.request", "code": code, "name": name,
                   "stages": _stages(role_key)}
        if scope:
            payload["scope"] = scope
        return _call(PUBLISH, "post", user, tenant, payload)

    # ── Authorization ────────────────────────────────────────────────────────

    def test_tenant_cannot_publish_a_platform_template(self):
        resp = self._publish(self.tenant_admin, self.tenant, scope="PLATFORM")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(WorkflowTemplate.all_objects.filter(tenant__isnull=True).exists())

    def test_platform_actor_can_publish_a_platform_template(self):
        resp = self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        template = WorkflowTemplate.all_objects.get(document_type="probe.request",
                                                    code="standard", tenant__isnull=True)
        self.assertIsNone(template.branch_id)
        self.assertTrue(_body(resp)["is_platform"])

    def test_platform_actor_without_scope_publishes_its_own(self):
        """Omitting scope must not silently publish the shared definition."""
        resp = self._publish(self.platform_admin, self.codex)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(WorkflowTemplate.all_objects.filter(tenant__isnull=True).exists())

    def test_other_tenant_cannot_reset_this_tenants_template(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        mine = WorkflowTemplate.all_objects.create(
            tenant=self.tenant, document_type="probe.request", code="standard", name="Mine")

        other = make_school(slug=f"other-{next(_counter)}", name="Other")
        other_admin = make_school_admin(make_branch(other), email=f"oadm-{next(_counter)}@t.com")
        _grant(other_admin, [PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW])
        resp = _call(USE_PLATFORM, "post", other_admin, other.tenant, pk=mine.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mine.refresh_from_db()
        self.assertTrue(mine.is_active)

    # ── The fallback ─────────────────────────────────────────────────────────

    def test_tenant_version_wins_then_falls_back_when_switched_off(self):
        from vs_workflow.services.submission import WorkflowTemplate as Model  # noqa: F811

        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform")
        self._publish(self.tenant_admin, self.tenant, name="Ours")

        platform = Model.all_objects.get(tenant__isnull=True, code="standard")
        mine = Model.all_objects.get(tenant=self.tenant, code="standard")
        self.assertNotEqual(platform.pk, mine.pk)

        # Their own version is what the cascade picks while it is active.
        resp = _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        mine.refresh_from_db()
        self.assertFalse(mine.is_active)
        # And the response hands back the platform version now in force.
        self.assertEqual(_body(resp)["id"], platform.pk)

    def test_reset_refuses_when_there_is_no_platform_version(self):
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        mine = WorkflowTemplate.all_objects.get(tenant=self.tenant, code="standard")
        resp = _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        mine.refresh_from_db()
        self.assertTrue(mine.is_active)

    def test_reset_refuses_on_the_platform_template_itself(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        platform = WorkflowTemplate.all_objects.get(tenant__isnull=True)
        resp = _call(USE_PLATFORM, "post", self.platform_admin, self.codex, pk=platform.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_publishing_again_brings_a_switched_off_version_back(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        mine = WorkflowTemplate.all_objects.get(tenant=self.tenant)
        _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)

        self._publish(self.tenant_admin, self.tenant, name="Ours again")
        mine.refresh_from_db()
        self.assertTrue(mine.is_active)
        self.assertEqual(mine.name, "Ours again")

    # ── What the screen reads ────────────────────────────────────────────────

    def test_list_says_which_version_this_tenant_is_running(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform")
        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        rows = {r["id"]: r for r in _body(resp)}
        platform_row = next(r for r in rows.values() if r["is_platform"])
        self.assertFalse(platform_row["tenant_has_own"])

        self._publish(self.tenant_admin, self.tenant, name="Ours")
        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        rows = list(_body(resp))
        platform_row = next(r for r in rows if r["is_platform"])
        own_row = next(r for r in rows if not r["is_platform"])
        self.assertTrue(platform_row["tenant_has_own"])
        self.assertIsNotNone(own_row["platform_updated_at"])
        self.assertFalse(own_row["platform_changed_since"])

    def test_platform_change_after_a_tenant_adjusted_is_flagged(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform")
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        # The platform revises the shared definition afterwards.
        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform v2")

        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        own_row = next(r for r in _body(resp) if not r["is_platform"])
        self.assertTrue(own_row["platform_changed_since"])

    def test_switched_off_version_is_not_reported_as_their_own(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        mine = WorkflowTemplate.all_objects.get(tenant=self.tenant)
        _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)

        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        platform_row = next(r for r in _body(resp) if r["is_platform"])
        self.assertFalse(platform_row["tenant_has_own"])
