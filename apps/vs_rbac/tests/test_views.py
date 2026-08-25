"""
Tests for vs_rbac API views.

Covers:
- Permission registry CRUD (Vision-only, vision/* routes - unchanged)
- Tenant role template CRUD (tenant-scoped)
- Tenant user role assignment CRUD + revoke
- Tenant role change request workflow + decision
- Access control: permission-denied (403), cross-tenant (404),
  mass-assignment rejection, revoke reflected in the evaluator.

Tenant-scoped endpoints go through the real auth layer: a JWT minted with
``CodeXRefreshToken.for_user`` plus the mandatory ``?tenant=<slug>`` assertion.
"""
import itertools
from urllib.parse import urlencode

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.evaluator import get_effective_permissions, has_permission
from vs_rbac.models import (
    Permission,
    PermissionDependency,
    TenantRoleTemplate,
    TenantRolePermission,
    TenantUserRoleAssignment,
    TenantRoleChangeRequest,
    TenantRoleChangeDeltaItem,
)
from vs_user.tokens import CodeXRefreshToken
from .helpers import (
    make_school,
    make_branch,
    make_vision_user,
    make_school_admin,
    make_staff_user,
    make_permission,
    make_dependency,
    make_role,
    make_role_permission,
    make_assignment,
    make_role_change_request,
)


_grant_counter = itertools.count(1)

# Permission keys the tenant-scoped views accept (school-side namespace; the
# views also accept the platform.* equivalents as any-of).
ROLE_KEYS = [
    "school.roles.view",
    "school.roles.create",
    "school.roles.update",
    "school.roles.delete",
    "school.roles.assign",
]
ROLE_APPROVE_KEY = "school.roles.approve"


def _grant(user, keys, tenant=None):
    """Grant *user* a fresh tenant role carrying *keys* on their tenant."""
    tenant = tenant or user.tenant
    role = make_role(tenant, name=f"grant-role-{next(_grant_counter)}")
    for k in keys:
        make_role_permission(role, make_permission(k))
    make_assignment(tenant, user, role)
    return role


def _token_client(user):
    client = APIClient()
    token = str(CodeXRefreshToken.for_user(user).access_token)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _q(url, tenant_slug, **params):
    query = {"tenant": tenant_slug, **params}
    return f"{url}?{urlencode(query)}"


# =============================================================================
# Permission Registry (Vision-only) - vision/* routes are unchanged
# =============================================================================
class _AuthMixin:
    def _vision_client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _admin_client(self):
        client = APIClient()
        client.force_authenticate(user=self.school_admin)
        return client

    def _anon_client(self):
        return APIClient()


class PermissionListCreateViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.url = reverse("rbac-permission-list-create")

    def test_create_permission_as_vision(self):
        from vs_rbac.models import PermissionAction, PermissionModule, PermissionResource
        module, _ = PermissionModule.objects.get_or_create(name="hr")
        PermissionResource.objects.get_or_create(module=module, name="leave")
        PermissionAction.objects.get_or_create(name="view")
        data = {"module": "hr", "resource": "leave", "action": "view", "description": "View"}
        resp = self._vision_client().post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Permission.objects.filter(key="hr.leave.view").exists())

    def test_search_permissions_across_related_fields(self):
        make_permission("zdummy.health.view")
        make_permission("zfinance.invoice.approve")
        resp = self._vision_client().get(self.url, {"search": "zdummy"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["key"] for row in resp.data["data"]], ["zdummy.health.view"])

    def test_school_admin_denied(self):
        resp = self._admin_client().get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anon_denied(self):
        resp = self._anon_client().get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class PermissionDependencyViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")

    def test_create_dependency(self):
        url = reverse("rbac-permission-dependency-list-create")
        data = {"permission_key": "finance.invoice.approve", "depends_on_key": "finance.invoice.view"}
        resp = self._vision_client().post(url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(PermissionDependency.objects.exists())

    def test_school_admin_denied(self):
        url = reverse("rbac-permission-dependency-list-create")
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Tenant Role Templates
# =============================================================================
class TenantRoleTemplateViewTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.slug = self.school.slug
        # Granted admin can manage roles; ungranted user cannot.
        self.admin = make_school_admin(self.branch, email="rt-admin@test.com")
        _grant(self.admin, ROLE_KEYS)
        self.plain = make_staff_user(self.branch, email="rt-plain@test.com")

    def _list_url(self, slug=None):
        return _q(reverse("rbac-role-list-create", kwargs={"tenant_slug": slug or self.slug}), slug or self.slug)

    def _detail_url(self, key, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-role-detail", kwargs={"tenant_slug": slug, "key": key}), slug)

    def test_list_roles(self):
        make_role(self.school, name="Teacher")
        make_role(self.school, name="Accountant")
        resp = _token_client(self.admin).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Includes the two roles plus the grant role assigned to the admin.
        names = {r["name"] for r in resp.data["data"]}
        self.assertIn("Teacher", names)
        self.assertIn("Accountant", names)

    def test_list_includes_counts(self):
        role = make_role(self.school, name="Teacher")
        perm = make_permission("students.profile.view")
        make_role_permission(role, perm)
        staff = make_staff_user(self.branch, email="counted@test.com")
        make_assignment(self.school, staff, role)

        resp = _token_client(self.admin).get(self._list_url())
        data = next(r for r in resp.data["data"] if r["name"] == "Teacher")
        self.assertEqual(data["assigned_users_count"], 1)
        self.assertEqual(data["permissions_count"], 1)

    def test_create_role(self):
        make_permission("finance.invoice.view")
        data = {
            "name": "Finance Manager",
            "description": "Manages finances",
            "permission_keys": ["finance.invoice.view"],
        }
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        role = TenantRoleTemplate.objects.get(tenant=self.school.tenant, name="Finance Manager")
        self.assertEqual(role.created_by, self.admin)
        self.assertEqual(role.role_permissions.count(), 1)
        self.assertTrue(role.key)

    def test_create_role_rejects_restricted_permission(self):
        make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        resp = _token_client(self.admin).post(
            self._list_url(),
            {
                "name": "Temporary Payments Officer",
                "permission_keys": ["payments.payout.create"],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            TenantRoleTemplate.objects.filter(
                tenant=self.school.tenant, name="Temporary Payments Officer",
            ).exists()
        )
        payout = _token_client(self.admin).post(
            _q(reverse("payments-payouts"), self.slug), {}, format="json",
        )
        self.assertEqual(payout.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_role_ignores_body_tenant_mass_assignment(self):
        other = make_school(slug="mass-other", name="Mass Other")
        data = {"name": "Scoped Role", "tenant": other.slug}
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        role = TenantRoleTemplate.objects.get(name="Scoped Role")
        # Scope always comes from the URL, never the body.
        self.assertEqual(role.tenant, self.school.tenant)

    def test_create_role_rejects_foreign_branch(self):
        other = make_school(slug="mass-branch", name="Mass Branch")
        other_branch = make_branch(other, name="Foreign Branch")
        data = {"name": "Branchy Role", "branch": other_branch.id}
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_retrieve_by_key(self):
        role = make_role(self.school, name="Teacher")
        make_role_permission(role, make_permission("students.profile.view"))
        resp = _token_client(self.admin).get(self._detail_url(role.key))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["name"], "Teacher")
        self.assertEqual(len(resp.data["data"]["role_permissions"]), 1)

    def test_update_permissions_bumps_version(self):
        role = make_role(self.school, name="Teacher")
        make_role_permission(role, make_permission("students.profile.view"))
        make_permission("students.profile.update")
        resp = _token_client(self.admin).patch(
            self._detail_url(role.key),
            {"permission_keys": ["students.profile.update"]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        role.refresh_from_db()
        self.assertEqual(
            set(role.role_permissions.values_list("permission_id", flat=True)),
            {"students.profile.update"},
        )
        self.assertEqual(role.version, 2)

    def test_update_role_rejects_new_restricted_permission(self):
        role = make_role(self.school, name="Payments Officer")
        make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )

        resp = _token_client(self.admin).patch(
            self._detail_url(role.key),
            {"permission_keys": ["payments.payout.create"]},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(role.role_permissions.exists())

    def test_delete_role(self):
        role = make_role(self.school, name="Teacher")
        resp = _token_client(self.admin).delete(self._detail_url(role.key))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(TenantRoleTemplate.objects.filter(pk=role.pk).exists())

    def test_system_role_update_blocked(self):
        role = make_role(self.school, name="Locked", is_system_role=True)
        resp = _token_client(self.admin).patch(
            self._detail_url(role.key), {"name": "New"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_super_admin_can_update_locked_system_role(self):
        super_admin = make_vision_user(
            email="system-role-super-admin@test.com",
            super_admin=True,
        )
        role = TenantRoleTemplate.objects.get(
            tenant=super_admin.tenant,
            key="xvs_super_admin",
        )
        role.is_system_role = True
        role.is_locked = True
        role.save(update_fields=["is_system_role", "is_locked", "updated_at"])

        url = self._detail_url(role.key, slug=super_admin.tenant.slug)
        resp = _token_client(super_admin).patch(
            url,
            {"description": "Updated by the active super admin."},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        role.refresh_from_db()
        self.assertEqual(role.description, "Updated by the active super admin.")

    def test_permission_denied_without_grant(self):
        resp = _token_client(self.plain).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cross_tenant_path_404(self):
        other = make_school(slug="cross-school", name="Cross School")
        # admin belongs to self.school; hitting another tenant's path 404s.
        resp = _token_client(self.admin).get(self._list_url(slug=other.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_anon_denied(self):
        resp = APIClient().get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# =============================================================================
# Tenant User Role Assignments
# =============================================================================
class TenantUserRoleAssignmentViewTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.slug = self.school.slug
        self.admin = make_school_admin(self.branch, email="asg-admin@test.com")
        _grant(self.admin, ROLE_KEYS)
        self.plain = make_staff_user(self.branch, email="asg-plain@test.com")
        self.staff = make_staff_user(self.branch, email="asg-staff@test.com")
        self.role = make_role(self.school, name="Teacher")

    def _list_url(self, slug=None, **params):
        slug = slug or self.slug
        return _q(reverse("rbac-assignment-list-create", kwargs={"tenant_slug": slug}), slug, **params)

    def _detail_url(self, aid, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-assignment-detail", kwargs={"tenant_slug": slug, "id": aid}), slug)

    def _revoke_url(self, aid, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-assignment-revoke", kwargs={"tenant_slug": slug, "id": aid}), slug)

    def _replace_url(self, aid, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-assignment-replace", kwargs={"tenant_slug": slug, "id": aid}), slug)

    def test_create_assignment(self):
        data = {"user": self.staff.id, "role": self.role.id, "reason_note": "Assign teacher"}
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        a = TenantUserRoleAssignment.objects.get(user=self.staff, role=self.role)
        self.assertEqual(a.assigned_by, self.admin)
        self.assertEqual(a.tenant, self.school.tenant)
        self.assertEqual(resp.data["data"]["role_key"], self.role.key)
        self.assertEqual(resp.data["data"]["assigned_by_id"], str(self.admin.id))
        self.assertEqual(resp.data["data"]["assigned_by_name"], self.admin.full_name)

    def test_cannot_assign_restricted_role_above_grant_ceiling(self):
        restricted = make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        make_role_permission(self.role, restricted)

        resp = _token_client(self.admin).post(
            self._list_url(),
            {"user": self.admin.id, "role": self.role.id},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user=self.admin, role=self.role,
            ).exists()
        )

    def test_list_and_filter(self):
        make_assignment(self.school, self.staff, self.role)
        resp = _token_client(self.admin).get(self._list_url(assignment_status="ACTIVE"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(any(r["user_id"] == str(self.staff.id) for r in resp.data["data"]))
        resp = _token_client(self.admin).get(self._list_url(assignment_status="REVOKED"))
        self.assertEqual(len(resp.data["data"]), 0)

    def test_revoke_via_patch(self):
        a = make_assignment(self.school, self.staff, self.role)
        resp = _token_client(self.admin).patch(
            self._detail_url(a.id),
            {"assignment_status": "REVOKED", "reason_note": "No longer needed"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.assignment_status, "REVOKED")
        self.assertEqual(a.revoked_by, self.admin)

    def test_revoke_endpoint_reflected_in_evaluator(self):
        perm = make_permission("students.profile.view")
        make_role_permission(self.role, perm)
        a = make_assignment(self.school, self.staff, self.role)
        # Granted before revoke.
        self.assertTrue(
            has_permission(self.staff, "students.profile.view", tenant=self.school.tenant)
        )
        resp = _token_client(self.admin).post(
            self._revoke_url(a.id), {"reason_note": "Rotated off"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.assignment_status, "REVOKED")
        # Denied after revoke (fresh user fetch to avoid the request-local cache).
        from vs_user.models import User
        fresh = User.objects.get(pk=self.staff.pk)
        self.assertNotIn(
            "students.profile.view",
            get_effective_permissions(fresh, tenant=self.school.tenant),
        )

    def test_revoke_requires_reason(self):
        a = make_assignment(self.school, self.staff, self.role)
        resp = _token_client(self.admin).post(self._revoke_url(a.id), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_revoke_already_revoked_conflict(self):
        a = make_assignment(self.school, self.staff, self.role, assignment_status="REVOKED")
        resp = _token_client(self.admin).post(
            self._revoke_url(a.id), {"reason_note": "again"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_super_admin_assignment_cannot_be_revoked(self):
        super_admin = make_vision_user(
            email="protected-super-admin@test.com",
            super_admin=True,
        )
        assignment = TenantUserRoleAssignment.objects.get(
            tenant=super_admin.tenant,
            user=super_admin,
            role__key="xvs_super_admin",
            assignment_status="ACTIVE",
        )

        resp = _token_client(super_admin).post(
            self._revoke_url(assignment.id, slug=super_admin.tenant.slug),
            {"reason_note": "Direct revoke attempt"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "SUPER_ADMIN_TRANSFER_REQUIRED")
        assignment.refresh_from_db()
        self.assertEqual(assignment.assignment_status, "ACTIVE")

    def test_super_admin_assignment_cannot_be_revoked_via_patch(self):
        super_admin = make_vision_user(
            email="protected-super-admin-patch@test.com",
            super_admin=True,
        )
        assignment = TenantUserRoleAssignment.objects.get(
            tenant=super_admin.tenant,
            user=super_admin,
            role__key="xvs_super_admin",
            assignment_status="ACTIVE",
        )

        resp = _token_client(super_admin).patch(
            self._detail_url(assignment.id, slug=super_admin.tenant.slug),
            {"assignment_status": "REVOKED", "reason_note": "Direct patch attempt"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        assignment.refresh_from_db()
        self.assertEqual(assignment.assignment_status, "ACTIVE")

    def test_super_admin_assignment_cannot_be_replaced(self):
        super_admin = make_vision_user(
            email="protected-super-admin-replace@test.com",
            super_admin=True,
        )
        assignment = TenantUserRoleAssignment.objects.get(
            tenant=super_admin.tenant,
            user=super_admin,
            role__key="xvs_super_admin",
            assignment_status="ACTIVE",
        )
        replacement_role = make_role(super_admin.tenant, name="Platform Admin")

        resp = _token_client(super_admin).post(
            self._replace_url(assignment.id, slug=super_admin.tenant.slug),
            {"role": replacement_role.id},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["code"], "SUPER_ADMIN_TRANSFER_REQUIRED")
        assignment.refresh_from_db()
        self.assertEqual(assignment.assignment_status, "ACTIVE")

    def test_replace_assignment_revokes_old_and_creates_new_atomically(self):
        old = make_assignment(self.school, self.staff, self.role)
        new_role = make_role(self.school, name="Head Teacher")

        resp = _token_client(self.admin).post(
            self._replace_url(old.id),
            {"role": new_role.id, "reason_note": "Promotion"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        old.refresh_from_db()
        self.assertEqual(old.assignment_status, "REVOKED")
        self.assertEqual(old.revoked_by, self.admin)
        self.assertIn("Head Teacher", old.reason_note)
        replacement = TenantUserRoleAssignment.objects.get(
            user=self.staff,
            role=new_role,
            assignment_status="ACTIVE",
        )
        self.assertEqual(replacement.assigned_by, self.admin)
        self.assertEqual(replacement.reason_note, "Promotion")
        self.assertEqual(resp.data["data"]["assigned_by_name"], self.admin.full_name)

    def test_replace_rejects_restricted_role_above_grant_ceiling(self):
        old = make_assignment(self.school, self.staff, self.role)
        restricted_role = make_role(self.school, name="Payout Officer")
        restricted = make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        make_role_permission(restricted_role, restricted)

        resp = _token_client(self.admin).post(
            self._replace_url(old.id),
            {"role": restricted_role.id, "reason_note": "Temporary cover"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        old.refresh_from_db()
        self.assertEqual(old.assignment_status, "ACTIVE")
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user=self.staff, role=restricted_role,
            ).exists()
        )

    def test_replace_assignment_preserves_other_active_roles(self):
        old = make_assignment(self.school, self.staff, self.role)
        retained_role = make_role(self.school, name="Exam Officer")
        retained = make_assignment(self.school, self.staff, retained_role)
        new_role = make_role(self.school, name="Head Teacher")

        resp = _token_client(self.admin).post(
            self._replace_url(old.id),
            {"role": new_role.id},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        retained.refresh_from_db()
        self.assertEqual(retained.assignment_status, "ACTIVE")

    def test_replace_assignment_rejects_cross_tenant_role(self):
        old = make_assignment(self.school, self.staff, self.role)
        other = make_school(slug="asg-replace-other", name="Asg Replace Other")
        foreign_role = make_role(other, name="Foreign")

        resp = _token_client(self.admin).post(
            self._replace_url(old.id),
            {"role": foreign_role.id},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        old.refresh_from_db()
        self.assertEqual(old.assignment_status, "ACTIVE")

    def test_replace_assignment_permission_denied_without_grant(self):
        old = make_assignment(self.school, self.staff, self.role)
        new_role = make_role(self.school, name="Head Teacher")

        resp = _token_client(self.plain).post(
            self._replace_url(old.id),
            {"role": new_role.id},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cross_tenant_role_rejected(self):
        other = make_school(slug="asg-other", name="Asg Other")
        foreign_role = make_role(other, name="Foreign")
        data = {"user": self.staff.id, "role": foreign_role.id}
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_foreign_user_rejected_mass_assignment(self):
        other = make_school(slug="asg-fuser", name="Asg FUser")
        other_branch = make_branch(other)
        foreign_user = make_staff_user(other_branch, email="foreign@test.com")
        data = {"user": foreign_user.id, "role": self.role.id}
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_permission_denied_without_grant(self):
        resp = _token_client(self.plain).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cross_tenant_path_404(self):
        other = make_school(slug="asg-cross", name="Asg Cross")
        resp = _token_client(self.admin).get(self._list_url(slug=other.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# =============================================================================
# Tenant Role Change Requests
# =============================================================================
class TenantRoleChangeRequestViewTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.slug = self.school.slug
        self.admin = make_school_admin(self.branch, email="rcr-admin@test.com")
        _grant(self.admin, ROLE_KEYS)
        self.reviewer = make_school_admin(
            self.branch, email="rcr-reviewer@test.com",
        )
        _grant(self.reviewer, [ROLE_APPROVE_KEY])
        self.plain = make_staff_user(self.branch, email="rcr-plain@test.com")
        self.role = make_role(self.school, name="Finance Manager")
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_export = make_permission("finance.invoice.export")

    def _list_url(self, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-role-change-request-list-create", kwargs={"tenant_slug": slug}), slug)

    def _queue_url(self, slug=None, **params):
        slug = slug or self.slug
        return _q(reverse("rbac-role-change-approval-queue", kwargs={"tenant_slug": slug}), slug, **params)

    def _detail_url(self, rid, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-role-change-approval-detail", kwargs={"tenant_slug": slug, "id": rid}), slug)

    def _decide_url(self, rid, slug=None):
        slug = slug or self.slug
        return _q(reverse("rbac-role-change-decide", kwargs={"tenant_slug": slug, "request_id": rid}), slug)

    def test_create_change_request(self):
        data = {
            "target_role": self.role.id,
            "justification": "Need invoice viewing for audit compliance",
            "delta_items": [{"permission_key": "finance.invoice.view", "operation": "ADD"}],
        }
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        rcr = TenantRoleChangeRequest.objects.get(target_role=self.role)
        self.assertEqual(rcr.status, "PENDING")
        self.assertEqual(rcr.requested_by, self.admin)
        self.assertEqual(rcr.delta_items.count(), 1)
        self.assertEqual(rcr.tenant, self.school.tenant)

    def test_cross_tenant_role_rejected(self):
        other = make_school(slug="rcr-other", name="Rcr Other")
        foreign_role = make_role(other, name="Foreign")
        data = {
            "target_role": foreign_role.id,
            "justification": "cross",
            "delta_items": [{"permission_key": "finance.invoice.view", "operation": "ADD"}],
        }
        resp = _token_client(self.admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_change_requests(self):
        make_role_change_request(self.school, self.admin, self.role)
        resp = _token_client(self.admin).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_permission_denied_without_grant(self):
        resp = _token_client(self.plain).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_queue_and_detail(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        resp = _token_client(self.admin).get(self._queue_url(status="PENDING"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        resp = _token_client(self.admin).get(self._detail_url(rcr.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["id"], rcr.id)

    def test_approve_applies_deltas(self):
        make_role_permission(self.role, self.perm_view)
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_export,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )
        resp = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "APPROVE", "notes": "ok"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "APPROVED")
        self.assertEqual(rcr.reviewer, self.reviewer)
        self.assertEqual(
            set(TenantRolePermission.objects.filter(role=self.role, granted=True)
                .values_list("permission_id", flat=True)),
            {"finance.invoice.view", "finance.invoice.export"},
        )

    def test_deny(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        resp = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "DENY", "notes": "no"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "DENIED")

    def test_deny_without_notes_rejected(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        resp = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "DENY", "notes": ""}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_decide_already_decided_conflict(self):
        rcr = make_role_change_request(self.school, self.admin, self.role, status="APPROVED")
        resp = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "DENY", "notes": "late"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_decide_not_found(self):
        resp = _token_client(self.reviewer).post(
            self._decide_url(999999), {"action": "APPROVE"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_requester_cannot_approve_own_request(self):
        _grant(self.admin, [ROLE_APPROVE_KEY])
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_view,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        resp = _token_client(self.admin).post(
            self._decide_url(rcr.id), {"action": "APPROVE"}, format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.PENDING)

    def test_restricted_approval_enforces_reviewer_grant_ceiling(self):
        restricted = make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=restricted,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        refused = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "APPROVE"}, format="json",
        )
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.PENDING)
        self.assertFalse(self.role.role_permissions.filter(permission=restricted).exists())

        _grant(self.reviewer, [restricted.key])
        approved = _token_client(self.reviewer).post(
            self._decide_url(rcr.id), {"action": "APPROVE"}, format="json",
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.content)
        self.assertTrue(self.role.role_permissions.filter(permission=restricted).exists())

    def test_platform_super_admin_can_bootstrap_first_restricted_holder(self):
        restricted = make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=restricted,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )
        platform_reviewer = make_vision_user(
            email="rcr-platform-reviewer@test.com", super_admin=True,
        )

        resp = _token_client(platform_reviewer).post(
            self._decide_url(rcr.id),
            {"action": "APPROVE", "notes": "Initial tenant bootstrap."},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertTrue(self.role.role_permissions.filter(permission=restricted).exists())

    def test_update_permission_cannot_decide_without_approval_permission(self):
        updater = make_staff_user(self.branch, email="rcr-updater@test.com")
        _grant(updater, ["school.roles.update"])
        rcr = make_role_change_request(self.school, self.admin, self.role)

        resp = _token_client(updater).post(
            self._decide_url(rcr.id), {"action": "DENY", "notes": "No."},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cross_tenant_path_404(self):
        other = make_school(slug="rcr-cross", name="Rcr Cross")
        resp = _token_client(self.admin).get(self._list_url(slug=other.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# =============================================================================
# Codex (platform) tenant - Vision super admin manages codex roles
# =============================================================================
class CodexTenantRoleViewTests(TestCase):
    def setUp(self):
        self.super_admin = make_vision_user(super_admin=True)
        self.plain_vision = make_vision_user(email="plain-vision@test.com")

    def _list_url(self):
        return _q(reverse("rbac-role-list-create", kwargs={"tenant_slug": "codex"}), "codex")

    def test_super_admin_creates_codex_role(self):
        make_permission("system.config.view")
        data = {"name": "Support Officer", "permission_keys": ["system.config.view"]}
        resp = _token_client(self.super_admin).post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        role = TenantRoleTemplate.objects.get(tenant__slug="codex", name="Support Officer")
        self.assertEqual(role.role_permissions.count(), 1)

    def test_plain_vision_denied(self):
        resp = _token_client(self.plain_vision).get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class PermissionGroupCreationScopeTests(TestCase):
    """A bundle built through the API has to be usable inside a tenant.

    ``PermissionGroup.scope`` has no default and the group serializer does not
    expose the field, so every group created through the endpoint was written
    unclassified - and ``TenantRoleGroup`` refuses to attach an unclassified
    bundle to a role inside a tenant. Vision could build a bundle that no
    school could ever be given.

    Serializer-level rather than over HTTP: the endpoint is gated by
    ``platform.permissions.manage`` and the question here is what the write
    produces, not who may reach it.
    """

    def test_group_created_through_the_serializer_is_tenant_scoped(self):
        from vs_rbac.models import PermissionGroup, PermissionScope
        from vs_rbac.serializers.registry import PermissionGroupDetailSerializer

        make_permission("academics.calendar.view")
        make_permission("academics.calendar.update")

        serializer = PermissionGroupDetailSerializer(data={
            "name": "Calendar Officer bundle",
            "description": "Calendar reads and edits.",
            "permission_keys": [
                "academics.calendar.view",
                "academics.calendar.update",
            ],
        })
        serializer.is_valid(raise_exception=True)
        group = serializer.save()

        self.assertEqual(group.scope, PermissionScope.TENANT)
        self.assertEqual(
            set(group.permissions.values_list("key", flat=True)),
            {"academics.calendar.view", "academics.calendar.update"},
        )
        self.assertTrue(PermissionGroup.objects.filter(pk=group.pk).exists())

    def test_that_group_can_then_be_attached_to_a_school_role(self):
        from vs_rbac.models import TenantRoleGroup
        from vs_rbac.serializers.registry import PermissionGroupDetailSerializer

        make_permission("academics.calendar.view")
        serializer = PermissionGroupDetailSerializer(data={
            "name": "Calendar reads",
            "permission_keys": ["academics.calendar.view"],
        })
        serializer.is_valid(raise_exception=True)
        group = serializer.save()

        school = make_school(slug="lakeside", name="Lakeside School")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="calendar-reader", name="Calendar Reader",
        )

        link = TenantRoleGroup(role=role, group=group)
        link.full_clean()
        link.save()

        self.assertTrue(
            TenantRoleGroup.objects.filter(role=role, group=group).exists(),
        )

    def test_group_rejects_restricted_permission_membership(self):
        from vs_rbac.serializers.registry import PermissionGroupDetailSerializer

        make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        serializer = PermissionGroupDetailSerializer(data={
            "name": "Payout shortcut",
            "permission_keys": ["payments.payout.create"],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("permission_keys", serializer.errors)

    def test_legacy_restricted_group_member_is_not_effective(self):
        from vs_rbac.models import GroupPermission, PermissionGroup, TenantRoleGroup
        from vs_user.models import User

        permission = make_permission("payments.payout.create")
        group = PermissionGroup.objects.create(
            name="Legacy payouts", scope="TENANT",
        )
        GroupPermission.objects.create(group=group, permission=permission)
        school = make_school(slug="legacy-group", name="Legacy Group School")
        branch = make_branch(school)
        role = make_role(school, name="Legacy Payout Officer")
        TenantRoleGroup.objects.create(role=role, group=group)
        holder = make_staff_user(branch, email="legacy-group@test.com")
        make_assignment(school, holder, role)

        permission.is_restricted = True
        permission.save(update_fields=["is_restricted", "updated_at"])

        fresh = User.objects.get(pk=holder.pk)
        self.assertNotIn(
            permission.key,
            get_effective_permissions(fresh, tenant=school.tenant),
        )
