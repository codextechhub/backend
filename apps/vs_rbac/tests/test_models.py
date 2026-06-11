"""
Tests for vs_rbac models: Permission, SchoolRoleTemplate, SchoolUserRoleAssignment,
SchoolRoleChangeRequest, and all Platform counterparts.
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

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


# =============================================================================
# Permission
# =============================================================================
class PermissionModelTests(TestCase):
    def test_create_permission(self):
        perm = make_permission("finance.invoice.view")
        self.assertEqual(perm.key, "finance.invoice.view")
        self.assertEqual(perm.module_id, "finance")
        self.assertEqual(perm.resource.name, "invoice")
        self.assertEqual(perm.action_id, "view")
        self.assertEqual(perm.sensitivity_level, Permission.Sensitivity.NORMAL)
        self.assertFalse(perm.is_restricted)
        self.assertTrue(perm.is_active)

    def test_permission_str(self):
        perm = make_permission("students.profile.update")
        self.assertEqual(str(perm), "students.profile.update")

    def test_duplicate_key_raises(self):
        first = make_permission("finance.invoice.view")
        with self.assertRaises(IntegrityError):
            Permission.objects.create(
                module=first.module,
                resource=first.resource,
                action=first.action,
            )

    def test_sensitivity_levels(self):
        for level in Permission.Sensitivity:
            perm = make_permission(
                f"test.{level.value.lower()}.action",
                sensitivity_level=level,
            )
            self.assertEqual(perm.sensitivity_level, level)


class PermissionDependencyModelTests(TestCase):
    def setUp(self):
        self.view = make_permission("finance.invoice.view")
        self.approve = make_permission("finance.invoice.approve")

    def test_create_dependency(self):
        dep = make_dependency("finance.invoice.approve", "finance.invoice.view")
        self.assertEqual(dep.permission_id, "finance.invoice.approve")
        self.assertEqual(dep.depends_on_id, "finance.invoice.view")

    def test_str(self):
        dep = make_dependency("finance.invoice.approve", "finance.invoice.view")
        self.assertIn("finance.invoice.approve", str(dep))
        self.assertIn("finance.invoice.view", str(dep))

    def test_duplicate_dependency_raises(self):
        make_dependency("finance.invoice.approve", "finance.invoice.view")
        with self.assertRaises(IntegrityError):
            make_dependency("finance.invoice.approve", "finance.invoice.view")


# =============================================================================
# SchoolRoleTemplate (school-scoped)
# =============================================================================
class SchoolRoleTemplateModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)

    def test_create_role(self):
        role = make_role(self.school, name="Teacher")
        self.assertEqual(role.school, self.school)
        self.assertEqual(role.name, "Teacher")
        self.assertEqual(role.status, SchoolRoleTemplate.Status.ACTIVE)
        self.assertFalse(role.is_system_role)
        self.assertFalse(role.is_locked)
        self.assertEqual(role.version, 1)

    def test_str(self):
        role = make_role(self.school, name="Accountant")
        # B23: __str__ renders the school's surrogate pk, not the slug.
        self.assertEqual(str(role), f"{self.school.pk}:Accountant")

    def test_bump_version(self):
        role = make_role(self.school)
        self.assertEqual(role.version, 1)
        role.bump_version()
        self.assertEqual(role.version, 2)
        role.bump_version()
        self.assertEqual(role.version, 3)

    def test_case_insensitive_unique_name_per_school(self):
        make_role(self.school, name="Teacher")
        with self.assertRaises(IntegrityError):
            make_role(self.school, name="teacher")

    def test_same_name_different_schools(self):
        school2 = make_school(slug="school-2", name="School 2")
        make_role(self.school, name="Teacher")
        role2 = make_role(school2, name="Teacher")
        self.assertEqual(role2.name, "Teacher")


class SchoolRolePermissionModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.role = make_role(self.school)
        self.perm = make_permission("finance.invoice.view")

    def test_create_role_permission(self):
        rp = make_role_permission(self.role, self.perm)
        self.assertTrue(rp.granted)
        self.assertEqual(rp.role, self.role)
        self.assertEqual(rp.permission, self.perm)

    def test_str(self):
        rp = make_role_permission(self.role, self.perm)
        self.assertIn("grant", str(rp))

    def test_deny_permission(self):
        rp = make_role_permission(self.role, self.perm, granted=False)
        self.assertFalse(rp.granted)
        self.assertIn("deny", str(rp))

    def test_unique_role_permission(self):
        make_role_permission(self.role, self.perm)
        with self.assertRaises(IntegrityError):
            make_role_permission(self.role, self.perm)


# =============================================================================
# SchoolUserRoleAssignment
# =============================================================================
class SchoolUserRoleAssignmentModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.user = make_staff_user(self.branch, email="staff1@test.com")
        self.role = make_role(self.school)

    def test_create_assignment(self):
        a = make_assignment(self.school, self.user, self.role)
        self.assertEqual(a.assignment_status, SchoolUserRoleAssignment.AssignmentStatus.ACTIVE)
        self.assertEqual(a.school, self.school)
        self.assertEqual(a.user, self.user)
        self.assertEqual(a.role, self.role)

    def test_str(self):
        a = make_assignment(self.school, self.user, self.role)
        self.assertIn("ACTIVE", str(a))

    def test_revoke(self):
        a = make_assignment(self.school, self.user, self.role)
        a.revoke(by_user=self.admin, reason="No longer needed")
        self.assertEqual(a.assignment_status, SchoolUserRoleAssignment.AssignmentStatus.REVOKED)
        self.assertEqual(a.revoked_by, self.admin)
        self.assertIsNotNone(a.revoked_at)
        self.assertEqual(a.reason_note, "No longer needed")

    def test_clean_cross_school_role_fails(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2)
        a = SchoolUserRoleAssignment(school=self.school, user=self.user, role=role2)
        with self.assertRaises(ValidationError):
            a.clean()

    def test_unique_active_assignment(self):
        make_assignment(self.school, self.user, self.role)
        with self.assertRaises(IntegrityError):
            make_assignment(self.school, self.user, self.role)

    def test_revoked_then_reassign_allowed(self):
        a = make_assignment(self.school, self.user, self.role)
        a.revoke(by_user=self.admin)
        a.save()
        # Should be able to create a new active assignment
        a2 = make_assignment(self.school, self.user, self.role)
        self.assertEqual(a2.assignment_status, SchoolUserRoleAssignment.AssignmentStatus.ACTIVE)


# =============================================================================
# SchoolRoleChangeRequest
# =============================================================================
class SchoolRoleChangeRequestModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.reviewer = make_vision_user()
        self.role = make_role(self.school)

    def test_create_request(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        self.assertEqual(rcr.status, SchoolRoleChangeRequest.Status.PENDING)
        self.assertEqual(rcr.requested_by, self.admin)
        self.assertEqual(rcr.target_role, self.role)

    def test_str(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        self.assertIn("PENDING", str(rcr))

    def test_mark_approved(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_approved(self.reviewer, "Looks good")
        self.assertEqual(rcr.status, SchoolRoleChangeRequest.Status.APPROVED)
        self.assertEqual(rcr.reviewer, self.reviewer)
        self.assertEqual(rcr.reviewer_notes, "Looks good")
        self.assertIsNotNone(rcr.decided_at)

    def test_mark_denied(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_denied(self.reviewer, "Not justified")
        self.assertEqual(rcr.status, SchoolRoleChangeRequest.Status.DENIED)
        self.assertEqual(rcr.reviewer_notes, "Not justified")

    def test_mark_apply_failed(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_apply_failed(self.reviewer, "Dependency error")
        self.assertEqual(rcr.status, SchoolRoleChangeRequest.Status.APPLY_FAILED)

    def test_clean_cross_school_role_fails(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2)
        rcr = SchoolRoleChangeRequest(
            school=self.school,
            requested_by=self.admin,
            target_role=role2,
            justification="Test",
        )
        with self.assertRaises(ValidationError):
            rcr.clean()

    def test_clean_empty_justification_fails(self):
        rcr = SchoolRoleChangeRequest(
            school=self.school,
            requested_by=self.admin,
            target_role=self.role,
            justification="   ",
        )
        with self.assertRaises(ValidationError):
            rcr.clean()


class SchoolRoleChangeDeltaItemModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.role = make_role(self.school)
        self.perm = make_permission("finance.invoice.view")
        self.rcr = make_role_change_request(self.school, self.admin, self.role)

    def test_create_delta_item(self):
        item = SchoolRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )
        self.assertEqual(item.operation, "ADD")

    def test_str(self):
        item = SchoolRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=SchoolRoleChangeDeltaItem.Operation.REMOVE,
        )
        self.assertIn("REMOVE", str(item))

    def test_unique_constraint(self):
        SchoolRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )
        with self.assertRaises(IntegrityError):
            SchoolRoleChangeDeltaItem.objects.create(
                request=self.rcr,
                permission=self.perm,
                operation=SchoolRoleChangeDeltaItem.Operation.ADD,
            )


# =============================================================================
# PlatformRoleTemplate
# =============================================================================
class PlatformRoleTemplateModelTests(TestCase):
    def test_create_platform_role(self):
        role = make_platform_role(name="Super Admin")
        self.assertEqual(role.name, "Super Admin")
        self.assertEqual(role.status, PlatformRoleTemplate.Status.ACTIVE)
        self.assertTrue(role.is_system_role)
        self.assertEqual(role.version, 1)
        self.assertIsNotNone(role.id)  # UUID

    def test_str(self):
        role = make_platform_role(name="Support Officer")
        self.assertEqual(str(role), "Support Officer")

    def test_bump_version(self):
        role = make_platform_role()
        role.bump_version()
        self.assertEqual(role.version, 2)

    def test_case_insensitive_unique_name(self):
        make_platform_role(name="Super Admin")
        with self.assertRaises(IntegrityError):
            make_platform_role(name="super admin")


class PlatformRolePermissionModelTests(TestCase):
    def test_create(self):
        role = make_platform_role()
        perm = make_permission("system.config.view")
        rp = make_platform_role_permission(role, perm)
        self.assertTrue(rp.granted)
        self.assertIsNotNone(rp.id)  # UUID

    def test_unique_constraint(self):
        role = make_platform_role()
        perm = make_permission("system.config.view")
        make_platform_role_permission(role, perm)
        with self.assertRaises(IntegrityError):
            make_platform_role_permission(role, perm)


# =============================================================================
# PlatformUserRoleAssignment
# =============================================================================
class PlatformUserRoleAssignmentModelTests(TestCase):
    def setUp(self):
        self.user = make_vision_user()
        self.role = make_platform_role()

    def test_create(self):
        a = make_platform_assignment(self.user, self.role)
        self.assertEqual(a.assignment_status, "ACTIVE")

    def test_revoke(self):
        a = make_platform_assignment(self.user, self.role)
        reviewer = make_vision_user(email="reviewer@test.com")
        a.revoke(by_user=reviewer, reason="Access revoked")
        self.assertEqual(a.assignment_status, "REVOKED")
        self.assertEqual(a.revoked_by, reviewer)
        self.assertIsNotNone(a.revoked_at)

    def test_unique_active_assignment(self):
        make_platform_assignment(self.user, self.role)
        with self.assertRaises(IntegrityError):
            make_platform_assignment(self.user, self.role)

    def test_revoked_then_reassign(self):
        a = make_platform_assignment(self.user, self.role)
        a.revoke()
        a.save()
        a2 = make_platform_assignment(self.user, self.role)
        self.assertEqual(a2.assignment_status, "ACTIVE")


# =============================================================================
# PlatformRoleChangeRequest
# =============================================================================
class PlatformRoleChangeRequestModelTests(TestCase):
    def setUp(self):
        self.user = make_vision_user()
        self.reviewer = make_vision_user(email="reviewer@test.com")
        self.role = make_platform_role()

    def test_create(self):
        rcr = make_platform_change_request(self.user, self.role)
        self.assertEqual(rcr.status, "PENDING")

    def test_mark_approved(self):
        rcr = make_platform_change_request(self.user, self.role)
        rcr.mark_approved(self.reviewer, "OK")
        self.assertEqual(rcr.status, "APPROVED")
        self.assertIsNotNone(rcr.decided_at)

    def test_mark_denied(self):
        rcr = make_platform_change_request(self.user, self.role)
        rcr.mark_denied(self.reviewer, "Rejected")
        self.assertEqual(rcr.status, "DENIED")

    def test_clean_empty_justification(self):
        rcr = PlatformRoleChangeRequest(
            requested_by=self.user,
            target_role=self.role,
            justification="  ",
        )
        with self.assertRaises(ValidationError):
            rcr.clean()

    def test_str(self):
        rcr = make_platform_change_request(self.user, self.role)
        self.assertIn("PENDING", str(rcr))
