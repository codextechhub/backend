"""Cross-tenant references must not be an existence oracle.

The tenant RBAC write endpoints take ids for a user, a role and a branch. If
one of those is resolved globally and only *then* checked against the caller's
tenant, the refusal itself carries information: "not yours" reads differently
from "no such thing", and a caller can walk the id space of other tenants by
reading which refusal comes back.

Every test below therefore compares the two **response bodies** rather than
asserting a message. A message assertion would pass just as happily on two
messages that differ in some other field, and the whole point is that nothing
in the response - not the message, not the field it is keyed on, not the status
- may differ between a foreign id and one that does not exist anywhere.

Two shapes of school are used throughout, per the multi-tenancy rule: one with
several branches and one with none at all.
"""
import itertools
from urllib.parse import urlencode

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.models import (
    TenantRoleChangeRequest,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.serializers.tenant import TenantUserRoleAssignmentSerializer
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
    make_assignment,
)


_grant_counter = itertools.count(1)

ROLE_KEYS = [
    "school.roles.view",
    "school.roles.create",
    "school.roles.update",
    "school.roles.delete",
    "school.roles.assign",
]


def _grant(user, keys, tenant=None):
    tenant = tenant or user.tenant
    role = make_role(tenant, name=f"scope-grant-{next(_grant_counter)}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)
    return role


def _token_client(user):
    client = APIClient()
    token = str(CodeXRefreshToken.for_user(user).access_token)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _q(url, tenant_slug):
    return f"{url}?{urlencode({'tenant': tenant_slug})}"


class _TwoShapedTenants(TestCase):
    """A multi-branch school, a branchless school, and a rival to steal from."""

    @classmethod
    def setUpTestData(cls):
        # Shape 1: a school with more than one branch.
        cls.multi = make_school(slug="scope-multi", name="Scope Multi")
        cls.lekki = make_branch(cls.multi, name="Lekki")
        cls.yaba = make_branch(cls.multi, name="Yaba", is_main=False)

        # Shape 2: a school with no branches at all.
        cls.solo = make_school(slug="scope-solo", name="Scope Solo")

        # The other tenant whose rows must stay invisible.
        cls.rival = make_school(slug="scope-rival", name="Scope Rival")
        cls.rival_branch = make_branch(cls.rival, name="Rival Campus")
        cls.rival_role = make_role(cls.rival, name="Rival Role")
        cls.rival_user = make_staff_user(
            cls.rival_branch, email="rival.staff@scope.test",
        )

        cls.admin = make_school_admin(cls.lekki, email="scope.admin@scope.test")
        _grant(cls.admin, ROLE_KEYS)
        cls.staff = make_staff_user(cls.yaba, email="scope.staff@scope.test")
        cls.role = make_role(cls.multi, name="Scope Role")

        # A user with no role grants at all, for the 403 checks.
        cls.plain = make_staff_user(cls.lekki, email="scope.plain@scope.test")

        cls.solo_admin = make_school_admin(
            None, email="solo.admin@scope.test", tenant=cls.solo.tenant,
        )
        _grant(cls.solo_admin, ROLE_KEYS, tenant=cls.solo.tenant)
        cls.solo_staff = make_school_admin(
            None, email="solo.staff@scope.test", tenant=cls.solo.tenant,
        )
        cls.solo_role = make_role(cls.solo, name="Solo Role")

        # Ids that exist nowhere. Far enough past the real rows to stay unused.
        cls.absent_branch = cls.rival_branch.pk + 10_000
        cls.absent_user = cls.rival_user.pk + 10_000
        cls.absent_role = cls.rival_role.pk + 10_000

    def assertIndistinguishable(self, foreign, absent):
        """The two refusals must be the same refusal, byte for byte."""
        self.assertEqual(foreign.status_code, status.HTTP_400_BAD_REQUEST, foreign.content)
        self.assertEqual(absent.status_code, status.HTTP_400_BAD_REQUEST, absent.content)
        self.assertEqual(
            foreign.content,
            absent.content,
            "a foreign id is distinguishable from one that does not exist",
        )


# =============================================================================
# Role templates: the branch reference
# =============================================================================
class RoleTemplateBranchScopingTests(_TwoShapedTenants):
    def _url(self, slug=None):
        slug = slug or self.multi.slug
        return _q(reverse("rbac-role-list-create", kwargs={"tenant_slug": slug}), slug)

    def _post(self, actor, slug=None, **body):
        payload = {"name": "Branchy Role"}
        payload.update(body)
        return _token_client(actor).post(self._url(slug), payload, format="json")

    def test_foreign_branch_is_refused_exactly_like_an_absent_one(self):
        foreign = self._post(self.admin, branch=self.rival_branch.pk)
        absent = self._post(self.admin, branch=self.absent_branch)

        self.assertIndistinguishable(foreign, absent)
        self.assertFalse(
            TenantRoleTemplate.objects.filter(name="Branchy Role").exists()
        )

    def test_foreign_branch_is_never_accepted(self):
        """The oracle is the bug; accepting the row would be the catastrophe."""
        resp = self._post(self.admin, branch=self.rival_branch.pk)

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            TenantRoleTemplate.objects.filter(branch=self.rival_branch).exists()
        )

    def test_a_branch_in_the_callers_own_tenant_still_works(self):
        resp = self._post(self.admin, branch=self.yaba.pk, name="Yaba Role")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        role = TenantRoleTemplate.objects.get(name="Yaba Role")
        self.assertEqual(role.branch_id, self.yaba.pk)
        self.assertEqual(role.tenant_id, self.multi.tenant_id)

    def test_a_role_without_a_branch_still_works(self):
        resp = self._post(self.admin, name="Tenant Wide Role")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIsNone(
            TenantRoleTemplate.objects.get(name="Tenant Wide Role").branch_id
        )

    def test_the_branchless_school_can_still_create_roles(self):
        """Where a school has no branches the dimension simply recedes."""
        resp = self._post(self.solo_admin, slug=self.solo.slug, name="Solo Wide Role")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        role = TenantRoleTemplate.objects.get(name="Solo Wide Role")
        self.assertIsNone(role.branch_id)
        self.assertEqual(role.tenant_id, self.solo.tenant_id)

    def test_a_branchless_school_cannot_borrow_another_schools_branch(self):
        foreign = self._post(self.solo_admin, slug=self.solo.slug, branch=self.lekki.pk)
        absent = self._post(
            self.solo_admin, slug=self.solo.slug, branch=self.absent_branch,
        )

        self.assertIndistinguishable(foreign, absent)

    def test_an_unusable_branch_id_is_a_refusal_not_a_server_error(self):
        """A non-numeric or oversized id must never reach the database."""
        for bad in ("not-an-id", "9" * 40, "-3", "1.5"):
            with self.subTest(branch=bad):
                resp = self._post(self.admin, branch=bad)
                self.assertEqual(
                    resp.status_code, status.HTTP_400_BAD_REQUEST, f"{bad}: {resp.content}"
                )

    def test_permission_denied_without_the_role_grant(self):
        resp = self._post(self.plain, branch=self.yaba.pk)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Assignments: the user, role and branch references
# =============================================================================
class AssignmentReferenceScopingTests(_TwoShapedTenants):
    def _url(self, slug=None):
        slug = slug or self.multi.slug
        return _q(
            reverse("rbac-assignment-list-create", kwargs={"tenant_slug": slug}), slug,
        )

    def _post(self, actor, slug=None, **body):
        return _token_client(actor).post(self._url(slug), body, format="json")

    def test_foreign_user_is_refused_exactly_like_an_absent_one(self):
        foreign = self._post(self.admin, user=self.rival_user.pk, role=self.role.pk)
        absent = self._post(self.admin, user=self.absent_user, role=self.role.pk)

        self.assertIndistinguishable(foreign, absent)

    def test_foreign_role_is_refused_exactly_like_an_absent_one(self):
        foreign = self._post(self.admin, user=self.staff.pk, role=self.rival_role.pk)
        absent = self._post(self.admin, user=self.staff.pk, role=self.absent_role)

        self.assertIndistinguishable(foreign, absent)

    def test_foreign_branch_is_refused_exactly_like_an_absent_one(self):
        foreign = self._post(
            self.admin, user=self.staff.pk, role=self.role.pk,
            branch=self.rival_branch.pk,
        )
        absent = self._post(
            self.admin, user=self.staff.pk, role=self.role.pk,
            branch=self.absent_branch,
        )

        self.assertIndistinguishable(foreign, absent)

    def test_no_cross_tenant_reference_is_ever_accepted(self):
        cases = {
            "user": {"user": self.rival_user.pk, "role": self.role.pk},
            "role": {"user": self.staff.pk, "role": self.rival_role.pk},
            "branch": {
                "user": self.staff.pk, "role": self.role.pk,
                "branch": self.rival_branch.pk,
            },
        }
        for field, body in cases.items():
            with self.subTest(field=field):
                resp = self._post(self.admin, **body)
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(tenant=self.multi.tenant)
            .filter(user=self.rival_user)
            .exists()
        )
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(role=self.rival_role).exists()
        )
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(branch=self.rival_branch).exists()
        )

    def test_a_legitimate_same_tenant_assignment_still_succeeds(self):
        resp = self._post(
            self.admin, user=self.staff.pk, role=self.role.pk, branch=self.yaba.pk,
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        assignment = TenantUserRoleAssignment.objects.get(
            user=self.staff, role=self.role,
        )
        self.assertEqual(assignment.tenant_id, self.multi.tenant_id)
        self.assertEqual(assignment.branch_id, self.yaba.pk)

    def test_a_branchless_assignment_still_succeeds(self):
        """Branch is optional, and omitting it must not trip the scoped lookup."""
        resp = self._post(self.solo_admin, slug=self.solo.slug,
                          user=self.solo_staff.pk, role=self.solo_role.pk)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        assignment = TenantUserRoleAssignment.objects.get(
            user=self.solo_staff, role=self.solo_role,
        )
        self.assertIsNone(assignment.branch_id)
        self.assertEqual(assignment.tenant_id, self.solo.tenant_id)

    def test_an_explicit_null_branch_is_still_accepted(self):
        resp = self._post(
            self.admin, user=self.staff.pk, role=self.role.pk, branch=None,
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIsNone(
            TenantUserRoleAssignment.objects.get(
                user=self.staff, role=self.role,
            ).branch_id
        )

    def test_an_unusable_reference_is_a_refusal_not_a_server_error(self):
        for bad in ("not-an-id", "9" * 40, "-3"):
            with self.subTest(user=bad):
                resp = self._post(self.admin, user=bad, role=self.role.pk)
                self.assertEqual(
                    resp.status_code, status.HTTP_400_BAD_REQUEST, f"{bad}: {resp.content}"
                )

    def test_permission_denied_without_the_assign_grant(self):
        resp = self._post(self.plain, user=self.staff.pk, role=self.role.pk)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Change requests: the target_role reference
# =============================================================================
class ChangeRequestRoleScopingTests(_TwoShapedTenants):
    def _url(self, slug=None):
        slug = slug or self.multi.slug
        return _q(
            reverse(
                "rbac-role-change-request-list-create", kwargs={"tenant_slug": slug},
            ),
            slug,
        )

    def _post(self, actor, target_role, slug=None):
        make_permission("finance.invoice.view")
        return _token_client(actor).post(
            self._url(slug),
            {
                "target_role": target_role,
                "justification": "Audit compliance needs invoice visibility",
                "delta_items": [
                    {"permission_key": "finance.invoice.view", "operation": "ADD"},
                ],
            },
            format="json",
        )

    def test_foreign_target_role_is_refused_exactly_like_an_absent_one(self):
        foreign = self._post(self.admin, self.rival_role.pk)
        absent = self._post(self.admin, self.absent_role)

        self.assertIndistinguishable(foreign, absent)
        self.assertFalse(
            TenantRoleChangeRequest.objects.filter(target_role=self.rival_role).exists()
        )

    def test_a_role_in_the_callers_own_tenant_still_works(self):
        resp = self._post(self.admin, self.role.pk)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        rcr = TenantRoleChangeRequest.objects.get(target_role=self.role)
        self.assertEqual(rcr.tenant_id, self.multi.tenant_id)

    def test_the_branchless_school_can_still_raise_a_change_request(self):
        resp = self._post(self.solo_admin, self.solo_role.pk, slug=self.solo.slug)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

    def test_permission_denied_without_the_update_grant(self):
        resp = self._post(self.plain, self.role.pk)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# The path where no tenant is known at all
# =============================================================================
class NoTenantContextTests(_TwoShapedTenants):
    """A serializer built with neither context nor instance resolves nothing.

    This state is unreachable over HTTP - the view always injects the tenant -
    but it is the one place a scoped queryset could silently become empty, so
    it is pinned: the refusal is the same for a foreign id, an absent id and a
    perfectly good one, which is to say it carries no information at all.
    """

    def _errors(self, **body):
        serializer = TenantUserRoleAssignmentSerializer(data=body)
        self.assertFalse(serializer.is_valid())
        return serializer.errors

    def test_every_reference_is_refused_identically_without_a_tenant(self):
        own = self._errors(user=self.staff.pk, role=self.role.pk)
        foreign = self._errors(user=self.rival_user.pk, role=self.rival_role.pk)
        absent = self._errors(user=self.absent_user, role=self.absent_role)

        self.assertEqual(foreign, absent)
        self.assertEqual(foreign, own)
        self.assertIn("tenant", own)

    def test_nothing_is_created_without_a_tenant(self):
        before = TenantUserRoleAssignment.objects.count()
        self._errors(user=self.staff.pk, role=self.role.pk)
        self.assertEqual(TenantUserRoleAssignment.objects.count(), before)
