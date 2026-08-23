"""Cross-tenant isolation on every vs_workflow view.

These cover a scope guard that never ran. ``SchoolScopedMixin.get_school()``
read ``request._cached_school``, an attribute nothing in the codebase ever
set, so it always returned None and every check written as
``if school is not None`` was dead. Models with a tenant-aware default manager
were still scoped by ambient context; the two that have neither a tenant
column nor a tenant-aware manager (WorkflowStageAction, WorkflowStageInstance)
were not scoped at all.

Two of these cover holes that were reachable in production: reversing another
tenant's approval action, and the team-load board counting every tenant's
work. The list/detail cases were held up in production by the ambient tenant
context the auth layer sets; they are covered here because these views now
scope explicitly and must not silently regress to depending on that context -
which is exactly what this harness, driving the views without the JWT auth
class, does not provide.
"""
import itertools

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_workflow.constants import (
    PERM_ACTION_REVERSE, PERM_INSTANCE_VIEW, PERM_TEMPLATE_VIEW,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowInstance, WorkflowStage, WorkflowStageAction,
    WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.views import (
    ApprovalDelegationViewSet, MySubmissionsView, ReverseActionView, TeamLoadView,
    WorkflowInstanceViewSet, WorkflowTemplateViewSet,
)

_counter = itertools.count(1)
factory = APIRequestFactory()


def _grant(user, keys):
    role = make_role(user.tenant, name=f"scope-grant-{next(_counter)}")
    for k in keys:
        make_role_permission(role, make_permission(k))
    make_assignment(user.tenant, user, role)
    return role


def _call(view, method, user, tenant, data=None, **kwargs):
    request = getattr(factory, method)("/v1/workflow/", data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    force_authenticate(request, user=user)
    return view(request, **kwargs)


def _rows(resp):
    data = resp.data
    if isinstance(data, dict) and "data" in data and "success" in data:
        data = data["data"]
    if isinstance(data, dict):
        return data.get("results", [])
    return data


class _TwoTenants(TestCase):
    """One school we act as, one we must never see."""

    def setUp(self):
        self.mine = make_school(slug="scope-mine", name="Mine")
        self.mine_branch = make_branch(self.mine)
        self.theirs = make_school(slug="scope-theirs", name="Theirs")
        self.theirs_branch = make_branch(self.theirs)

        self.admin = make_school_admin(self.mine_branch, email="scope-admin@test.com")
        _grant(self.admin, [PERM_TEMPLATE_VIEW, PERM_INSTANCE_VIEW, PERM_ACTION_REVERSE])

        self.their_user = make_school_admin(self.theirs_branch, email="scope-theirs@test.com")

    def _template(self, tenant, code):
        return WorkflowTemplate.objects.create(
            tenant=tenant, document_type="SCOPE_DOC", code=code, name=code)

    def _instance(self, tenant, requester, template, status_=None):
        return WorkflowInstance.objects.create(
            tenant=tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id="doc-" + str(next(_counter)),
            document_type="SCOPE_DOC",
            status=status_ or "IN_PROGRESS",
            requested_by=requester, submitted_at=timezone.now())

    def _active_stage_instance(self, instance):
        stage = WorkflowStage.objects.create(
            template=instance.template, code="s" + str(next(_counter)), label="Step")
        return WorkflowStageInstance.objects.create(
            instance=instance, stage=stage, status="ACTIVE",
            activated_at=timezone.now())


class ReverseActionScopingTests(_TwoTenants):
    """The hole with teeth: reversing another tenant's approval."""

    def _foreign_action(self):
        template = self._template(self.theirs.tenant, "theirs")
        instance = self._instance(self.theirs.tenant, self.their_user, template)
        si = self._active_stage_instance(instance)
        return WorkflowStageAction.objects.create(
            stage_instance=si, actor=self.their_user, action="APPROVED")

    def test_cannot_reverse_another_tenants_action(self):
        foreign = self._foreign_action()
        resp = _call(ReverseActionView.as_view(), "post", self.admin, self.mine.tenant,
                     {"reason": "not mine to reverse"}, action_id=foreign.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.reversed_at)
        self.assertFalse(WorkflowStageAction.objects.filter(is_reversal_of=foreign).exists())

    def test_missing_action_is_also_404(self):
        resp = _call(ReverseActionView.as_view(), "post", self.admin, self.mine.tenant,
                     {"reason": "x"}, action_id="nope1234")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TeamLoadScopingTests(_TwoTenants):
    """The quiet hole: another tenant's workload counted into our board."""

    def test_team_load_counts_only_our_tenant(self):
        mine = self._instance(self.mine.tenant, self.admin, self._template(self.mine.tenant, "mine"))
        self._active_stage_instance(mine)
        theirs = self._instance(self.theirs.tenant, self.their_user,
                                self._template(self.theirs.tenant, "theirs"))
        self._active_stage_instance(theirs)
        self._active_stage_instance(theirs)

        resp = _call(TeamLoadView.as_view(), "get", self.admin, self.mine.tenant)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = _rows(resp)
        self.assertEqual(sum(r["active_count"] for r in rows), 1)


class InstanceScopingTests(_TwoTenants):

    def test_instance_list_excludes_other_tenants(self):
        self._instance(self.mine.tenant, self.admin, self._template(self.mine.tenant, "mine"))
        self._instance(self.theirs.tenant, self.their_user,
                       self._template(self.theirs.tenant, "theirs"))
        view = WorkflowInstanceViewSet.as_view({"get": "list"})
        rows = _rows(_call(view, "get", self.admin, self.mine.tenant))
        self.assertEqual(len(rows), 1)

    def test_instance_detail_of_other_tenant_is_404(self):
        foreign = self._instance(self.theirs.tenant, self.their_user,
                                 self._template(self.theirs.tenant, "theirs"))
        view = WorkflowInstanceViewSet.as_view({"get": "retrieve"})
        resp = _call(view, "get", self.admin, self.mine.tenant, pk=foreign.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_my_submissions_excludes_other_tenants(self):
        """A user acting in one tenant sees only that tenant's own submissions."""
        self._instance(self.mine.tenant, self.admin,
                       self._template(self.mine.tenant, "mine"), status_="RETURNED")
        view = MySubmissionsView.as_view()
        rows = _rows(_call(view, "get", self.admin, self.mine.tenant))
        self.assertEqual(len(rows), 1)


class TemplateScopingTests(_TwoTenants):

    def test_template_list_excludes_other_tenants_but_keeps_global(self):
        self._template(self.mine.tenant, "mine")
        self._template(self.theirs.tenant, "theirs")
        WorkflowTemplate.all_objects.create(
            tenant=None, document_type="SCOPE_DOC", code="global", name="Global")
        view = WorkflowTemplateViewSet.as_view({"get": "list"})
        codes = {r["code"] for r in _rows(_call(view, "get", self.admin, self.mine.tenant))}
        self.assertNotIn("theirs", codes)
        # Platform-wide templates stay visible - that is what include_global means.
        self.assertIn("global", codes)

    def test_template_detail_of_other_tenant_is_404(self):
        foreign = self._template(self.theirs.tenant, "theirs")
        view = WorkflowTemplateViewSet.as_view({"get": "retrieve"})
        resp = _call(view, "get", self.admin, self.mine.tenant, pk=foreign.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_branch_user_sees_tenant_wide_templates(self):
        """Branch-pinned templates override the tenant-wide default rather than
        replacing it, so a branch user must see both."""
        self._template(self.mine.tenant, "tenant-wide")
        WorkflowTemplate.objects.create(
            tenant=self.mine.tenant, branch=self.mine_branch,
            document_type="SCOPE_DOC", code="branch-pinned", name="Branch")
        WorkflowTemplate.objects.create(
            tenant=self.mine.tenant,
            branch=make_branch(self.mine, name="Other Branch", is_main=False),
            document_type="SCOPE_DOC", code="other-branch", name="Other")

        view = WorkflowTemplateViewSet.as_view({"get": "list"})
        codes = {r["code"] for r in _rows(_call(view, "get", self.admin, self.mine.tenant))}
        self.assertIn("tenant-wide", codes)
        self.assertIn("branch-pinned", codes)
        self.assertNotIn("other-branch", codes)

class DelegationScopingTests(_TwoTenants):

    def test_delegation_list_excludes_other_tenants(self):
        now = timezone.now()
        other_delegate = make_school_admin(self.theirs_branch, email="scope-deleg@test.com")
        ApprovalDelegation.objects.create(
            tenant=self.theirs.tenant, delegator=self.their_user, delegate=other_delegate,
            starts_at=now, ends_at=now + timezone.timedelta(days=1))
        mine_delegate = make_school_admin(self.mine_branch, email="scope-mine-deleg@test.com")
        ApprovalDelegation.objects.create(
            tenant=self.mine.tenant, delegator=self.admin, delegate=mine_delegate,
            starts_at=now, ends_at=now + timezone.timedelta(days=1))

        view = ApprovalDelegationViewSet.as_view({"get": "list"})
        rows = _rows(_call(view, "get", self.admin, self.mine.tenant))
        self.assertEqual(len(rows), 1)


class DelegationWriteBoundaryTests(_TwoTenants):
    """A delegation may not name a delegate outside the delegation's tenant.

    ``delegate`` is client-supplied - ``perform_create`` pins only the tenant and
    the delegator - so this endpoint was the one place a caller could nominate an
    approver by id, and the id was resolved over the whole user table. Any
    authenticated user could therefore hand their approval authority to somebody
    in another tenant, whom ``resolve_approvers`` would then seat on the document.

    The refusal is only half of what is asserted here. Resolving the delegate
    *inside* the tenant means a foreign id and an id that exists nowhere take the
    same path and come back identical, so the endpoint cannot be walked to learn
    which user ids exist elsewhere. The resolution-time half of the fix, which
    neutralises rows written before this boundary, lives in
    ``test_services.DelegationTenantContainmentTests``.
    """

    def _payload(self, delegate_id):
        now = timezone.now()
        return {
            "delegate": delegate_id,
            "starts_at": now.isoformat(),
            "ends_at": (now + timezone.timedelta(days=1)).isoformat(),
        }

    def _create(self, delegate_id):
        view = ApprovalDelegationViewSet.as_view({"post": "create"})
        return _call(view, "post", self.admin, self.mine.tenant,
                     self._payload(delegate_id))

    def _unused_user_id(self):
        """An id no user holds, derived rather than hard-coded so it stays true."""
        from django.contrib.auth import get_user_model

        highest = (get_user_model().objects.order_by("-pk")
                   .values_list("pk", flat=True).first() or 0)
        return highest + 1000

    @staticmethod
    def _field_errors(resp):
        """The per-field errors, unwrapped from the platform error envelope."""
        data = resp.data
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            data = data["error"].get("detail", data)
        return data if isinstance(data, dict) else {}

    def test_can_delegate_to_a_user_in_our_own_tenant(self):
        """The counterweight: scoping the field must not break delegating at all."""
        colleague = make_school_admin(self.mine_branch, email="deleg-mine@test.com")

        resp = self._create(colleague.pk)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        row = ApprovalDelegation.all_objects.get()
        self.assertEqual(row.delegate_id, colleague.pk)
        self.assertEqual(row.delegator_id, self.admin.pk)
        self.assertEqual(row.tenant_id, self.mine.tenant.pk)

    def test_cannot_delegate_to_a_user_in_another_tenant(self):
        """The hole: approval authority handed across the tenant boundary."""
        self.assertNotEqual(self.their_user.tenant_id, self.mine.tenant.pk)

        resp = self._create(self.their_user.pk)

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("delegate", self._field_errors(resp))
        self.assertFalse(ApprovalDelegation.all_objects.exists())

    def test_a_foreign_delegate_is_reported_exactly_like_an_unknown_one(self):
        """No existence oracle: the two refusals must be indistinguishable.

        Rejecting a foreign id with its own wording would answer "does user 412
        exist somewhere on this platform?" for any authenticated caller, one id at
        a time. The two responses are compared to *each other* rather than to a
        copy of the message, so rewording the refusal cannot split them apart
        without failing here.
        """
        foreign = self._create(self.their_user.pk)
        unknown = self._create(self._unused_user_id())

        # Assert the refusal first: two identical successes would satisfy the
        # comparison below while proving the opposite of what it is for.
        self.assertEqual(foreign.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self._field_errors(foreign).get("delegate"))

        self.assertEqual(foreign.status_code, unknown.status_code)
        self.assertEqual(foreign.data, unknown.data)
        self.assertFalse(ApprovalDelegation.all_objects.exists())
