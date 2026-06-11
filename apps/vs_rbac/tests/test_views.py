"""
Tests for vs_rbac API views.

Covers:
- Permission registry CRUD (Vision-only)
- Permission dependency CRUD (Vision-only)
- School role template CRUD (School Admin)
- School user role assignment CRUD (School Admin)
- School role change request workflow
- Vision role change request queue + decision
- Platform role template CRUD (Vision)
- Platform user role assignment CRUD (Vision)
- Platform role change request workflow
- Access control: unauthenticated, wrong user type, cross-school
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.models import (
    Permission,
    PermissionDependency,
    SchoolRoleTemplate,
    SchoolRolePermission,
    SchoolUserRoleAssignment,
    SchoolRoleChangeRequest,
    SchoolRoleChangeDeltaItem,
    PlatformRoleTemplate,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
    PlatformRoleChangeRequest,
    PlatformRoleChangeDeltaItem,
)
from .helpers import (
    make_school,
    make_branch,
    make_vision_user,
    make_school_admin,
    make_staff_user,
    make_permission,
    make_permission_set,
    make_dependency,
    make_role,
    make_role_permission,
    make_assignment,
    make_role_change_request,
    make_platform_role,
    make_platform_role_permission,
    make_platform_assignment,
    make_platform_change_request,
)


class _AuthMixin:
    """Helper to set up authenticated clients."""

    def _vision_client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _admin_client(self):
        client = APIClient()
        client.force_authenticate(user=self.school_admin)
        return client

    def _staff_client(self):
        client = APIClient()
        client.force_authenticate(user=self.staff_user)
        return client

    def _anon_client(self):
        return APIClient()


# =============================================================================
# Permission Registry (Vision-only)
# =============================================================================
class PermissionListCreateViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.url = reverse("rbac-permission-list-create")

    def test_list_permissions_as_vision(self):
        make_permission("finance.invoice.view")
        make_permission("finance.invoice.approve")
        resp = self._vision_client().get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 2)

    def test_create_permission_as_vision(self):
        from vs_rbac.models import PermissionAction, PermissionModule, PermissionResource
        module, _ = PermissionModule.objects.get_or_create(name="hr")
        PermissionResource.objects.get_or_create(module=module, name="leave")
        PermissionAction.objects.get_or_create(name="view")
        data = {
            "module": "hr",
            "resource": "leave",
            "action": "view",
            "description": "View leave requests",
        }
        resp = self._vision_client().post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Permission.objects.filter(key="hr.leave.view").exists())

    def test_school_admin_denied(self):
        resp = self._admin_client().get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anon_denied(self):
        resp = self._anon_client().get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class PermissionDetailViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.perm = make_permission("finance.invoice.view")

    def _url(self, key=None):
        return reverse("rbac-permission-detail", kwargs={"key": key or self.perm.key})

    def test_retrieve(self):
        resp = self._vision_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["key"], "finance.invoice.view")

    def test_update(self):
        resp = self._vision_client().patch(
            self._url(), {"description": "Updated"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.perm.refresh_from_db()
        self.assertEqual(self.perm.description, "Updated")

    def test_delete(self):
        resp = self._vision_client().delete(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Permission.objects.filter(key="finance.invoice.view").exists())

    def test_school_admin_denied(self):
        resp = self._admin_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class PermissionDependencyViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")

    def test_create_dependency(self):
        url = reverse("rbac-permission-dependency-list-create")
        data = {
            "permission_key": "finance.invoice.approve",
            "depends_on_key": "finance.invoice.view",
        }
        resp = self._vision_client().post(url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(PermissionDependency.objects.exists())

    def test_list_dependencies(self):
        make_dependency("finance.invoice.approve", "finance.invoice.view")
        url = reverse("rbac-permission-dependency-list-create")
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_delete_dependency(self):
        dep = make_dependency("finance.invoice.approve", "finance.invoice.view")
        url = reverse("rbac-permission-dependency-detail", kwargs={"id": dep.id})
        resp = self._vision_client().delete(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_school_admin_denied(self):
        url = reverse("rbac-permission-dependency-list-create")
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# School Role Templates
# =============================================================================
class SchoolRoleTemplateListCreateViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)

    def _url(self):
        return reverse("rbac-role-list-create", kwargs={"school_slug": self.school.slug})

    def test_list_roles(self):
        make_role(self.school, name="Teacher")
        make_role(self.school, name="Accountant")
        resp = self._admin_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 2)

    def test_list_includes_counts(self):
        role = make_role(self.school, name="Teacher")
        perm = make_permission("students.profile.view")
        make_role_permission(role, perm)
        make_assignment(self.school, self.staff_user, role)

        resp = self._admin_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"][0]
        self.assertEqual(data["assigned_users_count"], 1)
        self.assertEqual(data["permissions_count"], 1)

    def test_create_role(self):
        make_permission("finance.invoice.view")
        data = {
            "school": self.school.slug,
            "name": "Finance Manager",
            "description": "Manages finances",
            "permission_keys": ["finance.invoice.view"],
        }
        resp = self._admin_client().post(self._url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(SchoolRoleTemplate.objects.filter(name="Finance Manager").exists())
        role = SchoolRoleTemplate.objects.get(name="Finance Manager")
        self.assertEqual(role.school, self.school)
        self.assertEqual(role.created_by, self.school_admin)
        self.assertEqual(role.role_permissions.count(), 1)

    def test_create_role_without_permissions(self):
        data = {"school": self.school.slug, "name": "Empty Role", "description": "No permissions"}
        resp = self._admin_client().post(self._url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_vision_staff_denied(self):
        resp = self._vision_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_staff_denied(self):
        resp = self._staff_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anon_denied(self):
        resp = self._anon_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class SchoolRoleTemplateDetailViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_role(self.school, name="Teacher")
        self.perm = make_permission("students.profile.view")
        make_role_permission(self.role, self.perm)

    def _url(self):
        return reverse(
            "rbac-role-detail",
            kwargs={"school_slug": self.school.slug, "id": self.role.id},
        )

    def test_retrieve(self):
        resp = self._admin_client().get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["name"], "Teacher")
        self.assertEqual(len(resp.data["data"]["role_permissions"]), 1)

    def test_update_name(self):
        resp = self._admin_client().patch(
            self._url(), {"name": "Senior Teacher"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        self.assertEqual(self.role.name, "Senior Teacher")

    def test_update_permissions(self):
        perm2 = make_permission("students.profile.update")
        resp = self._admin_client().patch(
            self._url(),
            {"permission_keys": ["students.profile.update"]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        perm_keys = set(
            self.role.role_permissions.values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, {"students.profile.update"})
        # Version should bump
        self.assertEqual(self.role.version, 2)

    def test_delete(self):
        resp = self._admin_client().delete(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(SchoolRoleTemplate.objects.filter(id=self.role.id).exists())

    def test_cross_school_isolation(self):
        """Roles from one school are not listed under another school's URL."""
        school2 = make_school(slug="school-2", name="School 2")
        branch2 = make_branch(school2, name="Branch 2")
        admin2 = make_school_admin(branch2, email="admin2@test.com")
        client2 = APIClient()
        client2.force_authenticate(user=admin2)

        # admin2 lists roles under school2's scope - should not see school1's roles
        url = reverse(
            "rbac-role-list-create",
            kwargs={"school_slug": school2.slug},
        )
        resp = client2.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 0)


# =============================================================================
# School User Role Assignments
# =============================================================================
class SchoolUserRoleAssignmentViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_role(self.school, name="Teacher")

    def _list_url(self):
        return reverse(
            "rbac-assignment-list-create",
            kwargs={"school_slug": self.school.slug},
        )

    def _detail_url(self, assignment_id):
        return reverse(
            "rbac-assignment-detail",
            kwargs={"school_slug": self.school.slug, "id": assignment_id},
        )

    def test_create_assignment(self):
        data = {
            "school": self.school.slug,
            "user": self.staff_user.id,
            "role": self.role.id,
            "reason_note": "Assign teacher role",
        }
        resp = self._admin_client().post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            SchoolUserRoleAssignment.objects.filter(
                user=self.staff_user, role=self.role
            ).exists()
        )
        assignment = SchoolUserRoleAssignment.objects.get(
            user=self.staff_user, role=self.role
        )
        self.assertEqual(assignment.assigned_by, self.school_admin)

    def test_list_assignments(self):
        make_assignment(self.school, self.staff_user, self.role)
        resp = self._admin_client().get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_filter_by_status(self):
        a = make_assignment(self.school, self.staff_user, self.role)
        resp = self._admin_client().get(
            self._list_url() + "?assignment_status=ACTIVE"
        )
        self.assertEqual(len(resp.data["data"]), 1)

        resp = self._admin_client().get(
            self._list_url() + "?assignment_status=REVOKED"
        )
        self.assertEqual(len(resp.data["data"]), 0)

    def test_revoke_assignment(self):
        a = make_assignment(self.school, self.staff_user, self.role)
        data = {
            "assignment_status": "REVOKED",
            "reason_note": "No longer needed",
        }
        resp = self._admin_client().patch(
            self._detail_url(a.id), data, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.assignment_status, "REVOKED")
        self.assertIsNotNone(a.revoked_at)
        self.assertEqual(a.revoked_by, self.school_admin)

    def test_cross_school_role_rejected(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2, name="Foreign Role")
        data = {
            "school": self.school.slug,
            "user": self.staff_user.id,
            "role": role2.id,
        }
        resp = self._admin_client().post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anon_denied(self):
        resp = self._anon_client().get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_denied(self):
        resp = self._staff_client().get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# School Role Change Requests
# =============================================================================
class SchoolRoleChangeRequestViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_role(self.school, name="Finance Manager")
        self.perm = make_permission("finance.invoice.view")

    def _list_url(self):
        return reverse(
            "rbac-role-change-request-list-create",
            kwargs={"school_slug": self.school.slug},
        )

    def test_create_change_request(self):
        data = {
            "school": self.school.slug,
            "target_role": self.role.id,
            "justification": "Need invoice viewing for audit compliance",
            "delta_items": [
                {
                    "permission_key": "finance.invoice.view",
                    "operation": "ADD",
                }
            ],
        }
        resp = self._admin_client().post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        rcr = SchoolRoleChangeRequest.objects.first()
        self.assertEqual(rcr.status, "PENDING")
        self.assertEqual(rcr.requested_by, self.school_admin)
        self.assertEqual(rcr.delta_items.count(), 1)

    def test_list_change_requests(self):
        make_role_change_request(self.school, self.school_admin, self.role)
        resp = self._admin_client().get(self._list_url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_cross_school_role_rejected(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2, name="Foreign Role")
        data = {
            "school": self.school.slug,
            "target_role": role2.id,
            "justification": "Cross-school attempt",
            "delta_items": [],
        }
        resp = self._admin_client().post(self._list_url(), data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class SchoolRoleChangeRequestApprovalViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_role(self.school, name="Finance Manager")
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_export = make_permission("finance.invoice.export")

    def test_queue_lists_all_requests(self):
        make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-approval-queue", kwargs={"school_slug": self.school.slug})
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_queue_filter_by_status(self):
        make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-approval-queue", kwargs={"school_slug": self.school.slug})
        resp = self._admin_client().get(url + "?status=PENDING")
        self.assertEqual(len(resp.data["data"]), 1)
        resp = self._admin_client().get(url + "?status=APPROVED")
        self.assertEqual(len(resp.data["data"]), 0)

    def test_detail_view(self):
        rcr = make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-approval-detail", kwargs={"school_slug": self.school.slug, "id": rcr.id})
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["id"], rcr.id)

    def test_approve_request(self):
        rcr = make_role_change_request(self.school, self.school_admin, self.role)
        # Add a delta item so apply has something to do
        make_role_permission(self.role, self.perm_view)
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_export,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )

        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": rcr.id})
        resp = self._admin_client().post(
            url, {"action": "APPROVE", "notes": "Looks good"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "APPROVED")
        self.assertEqual(rcr.reviewer, self.school_admin)

    def test_deny_request(self):
        rcr = make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": rcr.id})
        resp = self._admin_client().post(
            url, {"action": "DENY", "notes": "Not justified"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "DENIED")

    def test_deny_without_notes_rejected(self):
        rcr = make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": rcr.id})
        resp = self._admin_client().post(
            url, {"action": "DENY", "notes": ""}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_action_rejected(self):
        rcr = make_role_change_request(self.school, self.school_admin, self.role)
        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": rcr.id})
        resp = self._admin_client().post(
            url, {"action": "INVALID"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_decide_already_decided_conflict(self):
        rcr = make_role_change_request(
            self.school, self.school_admin, self.role, status="APPROVED"
        )
        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": rcr.id})
        resp = self._admin_client().post(
            url, {"action": "DENY", "notes": "Too late"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_decide_not_found(self):
        url = reverse("rbac-role-change-decide", kwargs={"school_slug": self.school.slug, "request_id": 99999})
        resp = self._admin_client().post(
            url, {"action": "APPROVE"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_denied_queue(self):
        url = reverse("rbac-role-change-approval-queue", kwargs={"school_slug": self.school.slug})
        resp = self._staff_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Platform Role Templates
# =============================================================================
class PlatformRoleTemplateViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)

    def test_list(self):
        make_platform_role(name="Super Admin")
        url = reverse("platform-rbac-role-list-create")
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in resp.data["data"]}
        self.assertIn("Super Admin", names)

    def test_create(self):
        make_permission("system.config.view")
        url = reverse("platform-rbac-role-list-create")
        data = {
            "name": "Support Officer",
            "description": "Handles support",
            "permission_keys": ["system.config.view"],
        }
        resp = self._vision_client().post(url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            PlatformRoleTemplate.objects.filter(name="Support Officer").exists()
        )

    def test_detail(self):
        role = make_platform_role(name="Super Admin")
        url = reverse("platform-rbac-role-detail", kwargs={"id": role.id})
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["name"], "Super Admin")

    def test_update(self):
        role = make_platform_role(name="Old Name")
        url = reverse("platform-rbac-role-detail", kwargs={"id": role.id})
        resp = self._vision_client().patch(
            url, {"name": "New Name"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        role.refresh_from_db()
        self.assertEqual(role.name, "New Name")

    def test_delete(self):
        role = make_platform_role(name="To Delete")
        url = reverse("platform-rbac-role-detail", kwargs={"id": role.id})
        resp = self._vision_client().delete(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_filter_by_status(self):
        make_platform_role(name="Active Role", status="ACTIVE")
        make_platform_role(name="Inactive Role", status="INACTIVE")
        url = reverse("platform-rbac-role-list-create")
        resp = self._vision_client().get(url + "?status=ACTIVE")
        names = {r["name"] for r in resp.data["data"]}
        self.assertIn("Active Role", names)
        self.assertNotIn("Inactive Role", names)

    def test_school_admin_denied(self):
        url = reverse("platform-rbac-role-list-create")
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Platform User Role Assignments
# =============================================================================
class PlatformUserRoleAssignmentViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_platform_role(name="Super Admin")
        self.target_user = make_vision_user(email="target@test.com")

    def test_create_assignment(self):
        url = reverse("platform-rbac-assignment-list-create")
        data = {"user": self.target_user.id, "role": str(self.role.id)}
        resp = self._vision_client().post(url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            PlatformUserRoleAssignment.objects.filter(
                user=self.target_user, role=self.role
            ).exists()
        )

    def test_list_assignments(self):
        make_platform_assignment(self.target_user, self.role)
        url = reverse("platform-rbac-assignment-list-create")
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        assigned = {(str(a["user_id"]), str(a["role_id"])) for a in resp.data["data"]}
        self.assertIn((str(self.target_user.id), str(self.role.id)), assigned)

    def test_revoke_assignment(self):
        a = make_platform_assignment(self.target_user, self.role)
        url = reverse("platform-rbac-assignment-detail", kwargs={"id": a.id})
        resp = self._vision_client().patch(
            url,
            {"assignment_status": "REVOKED", "reason_note": "Revoked"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.assignment_status, "REVOKED")

    def test_school_admin_denied(self):
        url = reverse("platform-rbac-assignment-list-create")
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# =============================================================================
# Platform Role Change Requests
# =============================================================================
class PlatformRoleChangeRequestViewTests(_AuthMixin, TestCase):
    def setUp(self):
        self.vision_user = make_vision_user(super_admin=True)
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.school_admin = make_school_admin(self.branch)
        self.staff_user = make_staff_user(self.branch)
        self.role = make_platform_role(name="Compliance Reviewer")
        self.perm = make_permission("system.audit.view")

    def test_create_request(self):
        url = reverse("platform-rbac-role-change-request-list-create")
        data = {
            "target_role": str(self.role.id),
            "justification": "Need audit viewing capability",
            "delta_items": [
                {"permission_key": "system.audit.view", "operation": "ADD"}
            ],
        }
        resp = self._vision_client().post(url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        rcr = PlatformRoleChangeRequest.objects.first()
        self.assertEqual(rcr.status, "PENDING")
        self.assertEqual(rcr.requested_by, self.vision_user)

    def test_list_requests(self):
        make_platform_change_request(self.vision_user, self.role)
        url = reverse("platform-rbac-role-change-request-list-create")
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_detail(self):
        rcr = make_platform_change_request(self.vision_user, self.role)
        url = reverse("platform-rbac-role-change-detail", kwargs={"id": rcr.id})
        resp = self._vision_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_approve(self):
        rcr = make_platform_change_request(self.vision_user, self.role)
        url = reverse(
            "platform-rbac-role-change-decide", kwargs={"request_id": rcr.id}
        )
        resp = self._vision_client().post(
            url, {"action": "APPROVE", "notes": "OK"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "APPROVED")

    def test_deny(self):
        rcr = make_platform_change_request(self.vision_user, self.role)
        url = reverse(
            "platform-rbac-role-change-decide", kwargs={"request_id": rcr.id}
        )
        resp = self._vision_client().post(
            url, {"action": "DENY", "notes": "Not approved"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rcr.refresh_from_db()
        self.assertEqual(rcr.status, "DENIED")

    def test_deny_without_notes(self):
        rcr = make_platform_change_request(self.vision_user, self.role)
        url = reverse(
            "platform-rbac-role-change-decide", kwargs={"request_id": rcr.id}
        )
        resp = self._vision_client().post(
            url, {"action": "DENY", "notes": ""}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_already_decided_conflict(self):
        rcr = make_platform_change_request(
            self.vision_user, self.role, status="DENIED"
        )
        url = reverse(
            "platform-rbac-role-change-decide", kwargs={"request_id": rcr.id}
        )
        resp = self._vision_client().post(
            url, {"action": "APPROVE"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_school_admin_denied(self):
        url = reverse("platform-rbac-role-change-request-list-create")
        resp = self._admin_client().get(url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
