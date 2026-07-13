"""
Tests for vs_rbac permission classes.
"""
from unittest.mock import MagicMock
from django.test import TestCase

from vs_rbac.permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsSchoolAdmin,
    HasRBACPermission,
    ReadOnly,
    user_has_rbac_permission,
)
from .helpers import (
    make_school,
    make_branch,
    make_vision_user,
    make_school_admin,
    make_staff_user,
    make_permission,
    make_role,
    make_role_permission,
    make_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_platform_assignment,
)


def _make_request(user=None, method="GET"):
    request = MagicMock()
    request.user = user
    request.method = method
    return request


class IsAuthenticatedAndActiveTests(TestCase):
    def setUp(self):
        self.perm = IsAuthenticatedAndActive()
        self.view = MagicMock()

    def test_anonymous_denied(self):
        request = _make_request(user=MagicMock(is_authenticated=False))
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_none_user_denied(self):
        request = _make_request(user=None)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_active_user_allowed(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_suspended_user_denied(self):
        from rest_framework.exceptions import PermissionDenied

        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="SUSPENDED")
        request = _make_request(user=user)
        with self.assertRaises(PermissionDenied):
            self.perm.has_permission(request, self.view)

    def test_locked_user_denied(self):
        from rest_framework.exceptions import PermissionDenied

        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="LOCKED")
        request = _make_request(user=user)
        with self.assertRaises(PermissionDenied):
            self.perm.has_permission(request, self.view)

    def test_deactivated_user_denied(self):
        from rest_framework.exceptions import PermissionDenied

        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="DEACTIVATED")
        request = _make_request(user=user)
        with self.assertRaises(PermissionDenied):
            self.perm.has_permission(request, self.view)

    def test_vision_staff_allowed(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))


class IsVisionStaffTests(TestCase):
    def setUp(self):
        self.perm = IsVisionStaff()
        self.view = MagicMock()

    def test_vision_staff_allowed(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_school_admin_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_staff_user_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_staff_user(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_anonymous_denied(self):
        request = _make_request(user=MagicMock(is_authenticated=False))
        self.assertFalse(self.perm.has_permission(request, self.view))


class IsSchoolAdminTests(TestCase):
    def setUp(self):
        self.perm = IsSchoolAdmin()
        self.view = MagicMock()

    def test_school_admin_allowed(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_vision_staff_denied(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_regular_staff_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_staff_user(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))


class ReadOnlyTests(TestCase):
    def setUp(self):
        self.perm = ReadOnly()
        self.view = MagicMock()

    def test_get_allowed(self):
        request = _make_request(method="GET")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_head_allowed(self):
        request = _make_request(method="HEAD")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_options_allowed(self):
        request = _make_request(method="OPTIONS")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_post_denied(self):
        request = _make_request(method="POST")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_put_denied(self):
        request = _make_request(method="PUT")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_delete_denied(self):
        request = _make_request(method="DELETE")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_patch_denied(self):
        request = _make_request(method="PATCH")
        self.assertFalse(self.perm.has_permission(request, self.view))


# =============================================================================
# user_has_rbac_permission (utility function)
# =============================================================================
class UserHasRBACPermissionTests(TestCase):
    """Tests for the user_has_rbac_permission helper."""

    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")

    # -- School-scoped users --------------------------------------------------

    def test_school_user_with_granted_permission(self):
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        self.assertTrue(
            user_has_rbac_permission(user, "finance.invoice.view", school=self.school)
        )

    def test_school_user_without_permission(self):
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        self.assertFalse(
            user_has_rbac_permission(user, "finance.invoice.approve", school=self.school)
        )

    def test_school_user_revoked_assignment_denied(self):
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view)
        assignment = make_assignment(self.school, user, role)
        assignment.revoke()
        assignment.save()

        self.assertFalse(
            user_has_rbac_permission(user, "finance.invoice.view", school=self.school)
        )

    def test_school_user_denied_permission_not_granted(self):
        """A SchoolRolePermission with granted=False should not count."""
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view, granted=False)
        make_assignment(self.school, user, role)

        self.assertFalse(
            user_has_rbac_permission(user, "finance.invoice.view", school=self.school)
        )

    def test_school_user_multiple_roles_any_grants(self):
        """If any of the user's active roles grants the permission, it passes."""
        user = make_staff_user(self.branch)
        role1 = make_role(self.school, name="Viewer")
        role2 = make_role(self.school, name="Approver")
        make_role_permission(role1, self.perm_view)
        make_role_permission(role2, self.perm_approve)
        make_assignment(self.school, user, role1)
        make_assignment(self.school, user, role2)

        self.assertTrue(
            user_has_rbac_permission(user, "finance.invoice.approve", school=self.school)
        )

    def test_school_user_cross_school_denied(self):
        """Permission from school-A role should not apply in school-B context."""
        school2 = make_school(slug="school-2", name="School 2")
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        self.assertFalse(
            user_has_rbac_permission(user, "finance.invoice.view", school=school2)
        )

    def test_school_user_no_school_filter_checks_all(self):
        """When school=None, any school's assignment is considered."""
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Accountant")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        self.assertTrue(
            user_has_rbac_permission(user, "finance.invoice.view", school=None)
        )

    def test_school_user_no_assignments(self):
        user = make_staff_user(self.branch)
        self.assertFalse(
            user_has_rbac_permission(user, "finance.invoice.view", school=self.school)
        )

    # -- Vision / platform users ----------------------------------------------

    def test_vision_user_with_platform_permission(self):
        user = make_vision_user()
        role = make_platform_role(name="Super Admin")
        make_platform_role_permission(role, self.perm_view)
        make_platform_assignment(user, role)

        self.assertTrue(user_has_rbac_permission(user, "finance.invoice.view"))

    def test_vision_user_without_platform_permission(self):
        user = make_vision_user()
        role = make_platform_role(name="Super Admin")
        make_platform_role_permission(role, self.perm_view)
        make_platform_assignment(user, role)

        self.assertFalse(user_has_rbac_permission(user, "finance.invoice.approve"))

    def test_vision_user_revoked_assignment_denied(self):
        user = make_vision_user()
        role = make_platform_role(name="Super Admin")
        make_platform_role_permission(role, self.perm_view)
        assignment = make_platform_assignment(user, role)
        assignment.revoke()
        assignment.save()

        self.assertFalse(user_has_rbac_permission(user, "finance.invoice.view"))

    def test_vision_user_no_assignments(self):
        user = make_vision_user()
        self.assertFalse(user_has_rbac_permission(user, "finance.invoice.view"))

    # -- Edge cases -----------------------------------------------------------

    def test_unauthenticated_user_denied(self):
        self.assertFalse(user_has_rbac_permission(None, "finance.invoice.view"))

    def test_anonymous_mock_user_denied(self):
        anon = MagicMock(is_authenticated=False)
        self.assertFalse(user_has_rbac_permission(anon, "finance.invoice.view"))


# =============================================================================
# HasRBACPermission (DRF permission class)
# =============================================================================
class HasRBACPermissionTests(TestCase):
    """Tests for the HasRBACPermission DRF permission class."""

    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.perm_class = HasRBACPermission()
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")

    def _make_view(self, rbac_permission=None):
        view = MagicMock()
        view.rbac_permission = rbac_permission
        return view

    def _make_request(self, user, school=None):
        request = MagicMock()
        request.user = user
        request.school = school
        # HasRBACPermission resolves the tenant from request.rbac_tenant first;
        # bind it to the real tenant context (a bare MagicMock would masquerade
        # as a truthy-but-invalid tenant and short-circuit the evaluator).
        request.rbac_tenant = school.tenant if school else getattr(user, "tenant", None)
        request.tenant = request.rbac_tenant
        request.branch = None
        return request

    def test_granted_single_permission(self):
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Viewer")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        request = self._make_request(user, school=self.school)
        view = self._make_view(rbac_permission="finance.invoice.view")

        self.assertTrue(self.perm_class.has_permission(request, view))

    def test_denied_missing_permission(self):
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Viewer")
        make_role_permission(role, self.perm_view)
        make_assignment(self.school, user, role)

        request = self._make_request(user, school=self.school)
        view = self._make_view(rbac_permission="finance.invoice.approve")

        self.assertFalse(self.perm_class.has_permission(request, view))

    def test_granted_any_of_multiple_permissions(self):
        """View declares a list of permission keys — user needs any one."""
        user = make_staff_user(self.branch)
        role = make_role(self.school, name="Approver")
        make_role_permission(role, self.perm_approve)
        make_assignment(self.school, user, role)

        request = self._make_request(user, school=self.school)
        view = self._make_view(
            rbac_permission=["finance.invoice.view", "finance.invoice.approve"]
        )

        self.assertTrue(self.perm_class.has_permission(request, view))

    def test_denied_none_of_multiple_permissions(self):
        user = make_staff_user(self.branch)
        # User has no roles

        request = self._make_request(user, school=self.school)
        view = self._make_view(
            rbac_permission=["finance.invoice.view", "finance.invoice.approve"]
        )

        self.assertFalse(self.perm_class.has_permission(request, view))

    def test_no_rbac_permission_declared_passes(self):
        """If the view has no rbac_permission attr, the check is a pass-through."""
        user = make_staff_user(self.branch)
        request = self._make_request(user, school=self.school)
        view = self._make_view(rbac_permission=None)

        self.assertTrue(self.perm_class.has_permission(request, view))

    def test_unauthenticated_denied(self):
        anon = MagicMock(is_authenticated=False)
        request = self._make_request(anon)
        view = self._make_view(rbac_permission="finance.invoice.view")

        self.assertFalse(self.perm_class.has_permission(request, view))

    def test_vision_user_with_platform_role(self):
        user = make_vision_user()
        role = make_platform_role(name="Compliance Officer")
        make_platform_role_permission(role, self.perm_view)
        make_platform_assignment(user, role)

        request = self._make_request(user, school=None)
        view = self._make_view(rbac_permission="finance.invoice.view")

        self.assertTrue(self.perm_class.has_permission(request, view))

    def test_vision_user_without_platform_role_denied(self):
        user = make_vision_user()

        request = self._make_request(user, school=None)
        view = self._make_view(rbac_permission="finance.invoice.view")

        self.assertFalse(self.perm_class.has_permission(request, view))
