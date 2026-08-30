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


#: The document type these tests publish under. Every query below is narrowed
#: to it, because the platform template space is NOT this test's to own: the
#: platform carries real seeded fallbacks - vs_payments migration 0006 seeds
#: one for payments.payout_batch under the code "standard", the same code these
#: tests use - and a bare filter on tenant__isnull=True picks it up. That is
#: what turned three assertions false and made two get() calls return two rows.
DOC = "probe.request"


def _platform(**kwargs):
    """Platform-scoped templates belonging to THIS test, never the seeded ones."""
    return WorkflowTemplate.all_objects.filter(tenant__isnull=True, **kwargs)


def _row(rows, *, platform):
    """The one row for this test's document type, from a page that has others."""
    return next(
        r for r in rows
        if r["document_type"] == DOC and bool(r["is_platform"]) is platform
    )


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
        payload = {"document_type": DOC, "code": code, "name": name,
                   "stages": _stages(role_key)}
        if scope:
            payload["scope"] = scope
        return _call(PUBLISH, "post", user, tenant, payload)

    # ── Authorization ────────────────────────────────────────────────────────

    def test_tenant_cannot_publish_a_platform_template(self):
        resp = self._publish(self.tenant_admin, self.tenant, scope="PLATFORM")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(_platform(document_type=DOC).exists())

    def test_platform_actor_can_publish_a_platform_template(self):
        resp = self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        template = _platform(document_type=DOC, code="standard").get()
        self.assertIsNone(template.branch_id)
        self.assertTrue(_body(resp)["is_platform"])

    def test_platform_actor_without_scope_publishes_its_own(self):
        """Omitting scope must not silently publish the shared definition."""
        resp = self._publish(self.platform_admin, self.codex)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(_platform(document_type=DOC).exists())

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
        Model = WorkflowTemplate

        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform")
        self._publish(self.tenant_admin, self.tenant, name="Ours")

        platform = _platform(document_type=DOC, code="standard").get()
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
        platform = _platform(document_type=DOC).get()
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
        platform_row = _row(_body(resp), platform=True)
        self.assertFalse(platform_row["tenant_has_own"])

        self._publish(self.tenant_admin, self.tenant, name="Ours")
        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        rows = list(_body(resp))
        platform_row = _row(rows, platform=True)
        own_row = _row(rows, platform=False)
        self.assertTrue(platform_row["tenant_has_own"])
        self.assertIsNotNone(own_row["platform_updated_at"])
        self.assertFalse(own_row["platform_changed_since"])

    def test_platform_change_after_a_tenant_adjusted_is_flagged(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform")
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        # The platform revises the shared definition afterwards.
        self._publish(self.platform_admin, self.codex, scope="PLATFORM", name="Platform v2")

        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        own_row = _row(_body(resp), platform=False)
        self.assertTrue(own_row["platform_changed_since"])

    def test_switched_off_version_is_not_reported_as_their_own(self):
        self._publish(self.platform_admin, self.codex, scope="PLATFORM")
        self._publish(self.tenant_admin, self.tenant, name="Ours")
        mine = WorkflowTemplate.all_objects.get(tenant=self.tenant)
        _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)

        resp = _call(LIST, "get", self.tenant_admin, self.tenant)
        platform_row = next(r for r in _body(resp) if r["is_platform"])
        self.assertFalse(platform_row["tenant_has_own"])


ADOPTION = WorkflowTemplateViewSet.as_view({"get": "adoption"})
COMPARE = WorkflowTemplateViewSet.as_view({"get": "compare"})


class PlatformOversightTests(TestCase):
    """Reading across tenants: who runs the shared template, and how theirs differs.

    This is the only place the console crosses a tenant boundary, so the denial
    cases come first and are exhaustive: a school must not reach it at all, and
    a platform actor must not reach an arbitrary template through it.
    """

    def setUp(self):
        self.codex = codex_tenant()
        self.platform_admin = make_vision_user(email=f"ovr-{next(_counter)}@codex.com")
        _grant(self.platform_admin, [PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW], self.codex)
        make_role(self.codex, name="Approver", key="approver")

        self.school = make_school(slug=f"ovr-school-{next(_counter)}", name="Ovr School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.tenant_admin = make_school_admin(self.branch, email=f"ovr-adm-{next(_counter)}@t.com")
        _grant(self.tenant_admin, [PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW])
        make_role(self.tenant, name="Approver", key="approver")
        make_role(self.tenant, name="Second", key="second")

        payload = {"document_type": "probe.request", "code": "standard",
                   "name": "Shared", "stages": _stages(), "scope": "PLATFORM"}
        _call(PUBLISH, "post", self.platform_admin, self.codex, payload)
        self.shared = WorkflowTemplate.all_objects.get(tenant__isnull=True,
                                                       document_type="probe.request")

    def _adjust(self, stages=None, name="Ours"):
        _call(PUBLISH, "post", self.tenant_admin, self.tenant,
              {"document_type": "probe.request", "code": "standard", "name": name,
               "stages": stages or _stages(role_key="second")})
        return WorkflowTemplate.all_objects.get(tenant=self.tenant,
                                                document_type="probe.request")

    # ── Denials ──────────────────────────────────────────────────────────────

    def test_school_cannot_see_adoption(self):
        resp = _call(ADOPTION, "get", self.tenant_admin, self.tenant, pk=self.shared.pk)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_school_cannot_compare(self):
        mine = self._adjust()
        resp = _call(COMPARE, "get", self.tenant_admin, self.tenant, pk=self.shared.pk)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(mine.pk)

    def test_adoption_refuses_on_a_tenant_template(self):
        mine = self._adjust()
        resp = _call(ADOPTION, "get", self.platform_admin, self.codex, pk=mine.pk)
        # Not found or refused, but never a listing: a tenant template is not
        # in the platform actor's own queryset.
        self.assertIn(resp.status_code,
                      (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND))

    def test_compare_refuses_a_template_from_another_path(self):
        """The pairing is checked, so an id from elsewhere cannot be read."""
        other_school = make_school(slug=f"ovr-other-{next(_counter)}", name="Other")
        stray = WorkflowTemplate.all_objects.create(
            tenant=other_school.tenant, document_type="unrelated.doc",
            code="standard", name="Stray")
        request = factory.get(f"{BASE}?with={stray.pk}")
        request.tenant = self.codex
        request.rbac_tenant = self.codex
        force_authenticate(request, user=self.platform_admin)
        resp = COMPARE(request, pk=self.shared.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # ── The answers ──────────────────────────────────────────────────────────

    def test_adoption_counts_followers_and_names_the_adjusted(self):
        before = _body(_call(ADOPTION, "get", self.platform_admin, self.codex,
                             pk=self.shared.pk))
        self.assertEqual(before["adjusted_count"], 0)
        self.assertEqual(before["following_count"], before["customer_count"])

        self._adjust()
        after = _body(_call(ADOPTION, "get", self.platform_admin, self.codex,
                            pk=self.shared.pk))
        self.assertEqual(after["adjusted_count"], 1)
        self.assertEqual(after["following_count"], after["customer_count"] - 1)
        self.assertEqual(after["adjusted"][0]["tenant_slug"], self.tenant.slug)

    def test_a_switched_off_version_counts_as_following_again(self):
        mine = self._adjust()
        _call(USE_PLATFORM, "post", self.tenant_admin, self.tenant, pk=mine.pk)
        body = _body(_call(ADOPTION, "get", self.platform_admin, self.codex,
                           pk=self.shared.pk))
        self.assertEqual(body["adjusted_count"], 0)

    def test_compare_reports_the_changed_field(self):
        mine = self._adjust()
        request = factory.get(f"{BASE}?with={mine.pk}")
        request.tenant = self.codex
        request.rbac_tenant = self.codex
        force_authenticate(request, user=self.platform_admin)
        body = _body(COMPARE(request, pk=self.shared.pk))

        self.assertFalse(body["identical"])
        changed = body["stages"]["changed"]
        self.assertEqual(len(changed), 1)
        fields = {f["field"]: f for f in changed[0]["fields"]}
        self.assertIn("approver_role_key", fields)
        self.assertEqual(fields["approver_role_key"]["base"], "approver")
        self.assertEqual(fields["approver_role_key"]["other"], "second")

    def test_compare_reports_an_added_stage(self):
        extra = _stages() + [{
            "code": "second-look", "label": "Second look", "kind": "APPROVAL", "order": 2,
            "approver_source": "ROLE", "approver_role_key": "second",
            "approver_scope": "SCHOOL", "advance_rule": "ANY",
            "on_rejection": "TERMINAL", "skip_if_no_approvers": True,
        }]
        mine = self._adjust(stages=extra)
        request = factory.get(f"{BASE}?with={mine.pk}")
        request.tenant = self.codex
        request.rbac_tenant = self.codex
        force_authenticate(request, user=self.platform_admin)
        body = _body(COMPARE(request, pk=self.shared.pk))
        self.assertEqual([s["code"] for s in body["stages"]["added"]], ["second-look"])

    def test_identical_copy_reports_no_differences(self):
        mine = self._adjust(stages=_stages(), name="Shared")
        request = factory.get(f"{BASE}?with={mine.pk}")
        request.tenant = self.codex
        request.rbac_tenant = self.codex
        force_authenticate(request, user=self.platform_admin)
        body = _body(COMPARE(request, pk=self.shared.pk))
        self.assertTrue(body["identical"], body)
