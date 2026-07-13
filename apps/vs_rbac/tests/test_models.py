"""
Tests for vs_rbac models: Permission, and the canonical tenant RBAC models
(TenantRoleTemplate, TenantRolePermission, TenantUserRoleAssignment,
TenantRoleChangeRequest, TenantRoleChangeDeltaItem).
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from vs_rbac.models import (
    Permission,
    TenantRoleTemplate,
    TenantRolePermission,
    TenantUserRoleAssignment,
    TenantRoleChangeRequest,
    TenantRoleChangeDeltaItem,
)
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
    make_platform_role,
    make_platform_role_permission,
    make_platform_assignment,
    make_platform_change_request,
)


# =============================================================================
# Permission (shared registry)
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
# TenantRoleTemplate
# =============================================================================
class TenantRoleTemplateModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.tenant = self.school.tenant

    def test_create_role(self):
        role = make_role(self.school, name="Teacher")
        self.assertEqual(role.tenant, self.tenant)
        self.assertEqual(role.name, "Teacher")
        self.assertEqual(role.status, TenantRoleTemplate.Status.ACTIVE)
        self.assertFalse(role.is_system_role)
        self.assertFalse(role.is_locked)
        self.assertEqual(role.version, 1)
        self.assertTrue(role.key)

    def test_str(self):
        role = make_role(self.school, name="Accountant")
        self.assertEqual(str(role), f"{self.tenant.pk}:Accountant")

    def test_duplicate_name_per_tenant_raises(self):
        make_role(self.school, name="Teacher")
        with self.assertRaises(IntegrityError):
            make_role(self.school, name="Teacher")

    def test_same_name_different_tenants(self):
        school2 = make_school(slug="school-2", name="School 2")
        make_role(self.school, name="Teacher")
        role2 = make_role(school2, name="Teacher")
        self.assertEqual(role2.name, "Teacher")

    def test_accepts_tenant_directly(self):
        role = make_role(self.tenant, name="Registrar")
        self.assertEqual(role.tenant, self.tenant)


class TenantRolePermissionModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.role = make_role(self.school)
        self.perm = make_permission("finance.invoice.view")

    def test_create_role_permission(self):
        rp = make_role_permission(self.role, self.perm)
        self.assertTrue(rp.granted)
        self.assertEqual(rp.role, self.role)
        self.assertEqual(rp.permission, self.perm)

    def test_deny_permission(self):
        rp = make_role_permission(self.role, self.perm, granted=False)
        self.assertFalse(rp.granted)

    def test_unique_role_permission(self):
        make_role_permission(self.role, self.perm)
        with self.assertRaises(IntegrityError):
            make_role_permission(self.role, self.perm)


# =============================================================================
# TenantUserRoleAssignment
# =============================================================================
class TenantUserRoleAssignmentModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.user = make_staff_user(self.branch, email="staff1@test.com")
        self.role = make_role(self.school)

    def test_create_assignment(self):
        a = make_assignment(self.school, self.user, self.role)
        self.assertEqual(a.assignment_status, TenantUserRoleAssignment.AssignmentStatus.ACTIVE)
        self.assertEqual(a.tenant, self.school.tenant)
        self.assertEqual(a.user, self.user)
        self.assertEqual(a.role, self.role)

    def test_revoke(self):
        a = make_assignment(self.school, self.user, self.role)
        a.revoke(by_user=self.admin, reason="No longer needed")
        self.assertEqual(a.assignment_status, TenantUserRoleAssignment.AssignmentStatus.REVOKED)
        self.assertEqual(a.revoked_by, self.admin)
        self.assertIsNotNone(a.revoked_at)
        self.assertEqual(a.reason_note, "No longer needed")

    def test_clean_cross_tenant_role_fails(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2)
        a = TenantUserRoleAssignment(
            tenant=self.school.tenant, user=self.user, role=role2,
        )
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
        a2 = make_assignment(self.school, self.user, self.role)
        self.assertEqual(a2.assignment_status, TenantUserRoleAssignment.AssignmentStatus.ACTIVE)


# =============================================================================
# TenantRoleChangeRequest
# =============================================================================
class TenantRoleChangeRequestModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.reviewer = make_vision_user()
        self.role = make_role(self.school)

    def test_create_request(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.PENDING)
        self.assertEqual(rcr.requested_by, self.admin)
        self.assertEqual(rcr.target_role, self.role)
        self.assertEqual(rcr.tenant, self.school.tenant)

    def test_str(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        self.assertIn("PENDING", str(rcr))

    def test_mark_approved(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_approved(self.reviewer, "Looks good")
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.APPROVED)
        self.assertEqual(rcr.reviewer, self.reviewer)
        self.assertEqual(rcr.reviewer_notes, "Looks good")
        self.assertIsNotNone(rcr.decided_at)

    def test_mark_denied(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_denied(self.reviewer, "Not justified")
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.DENIED)
        self.assertEqual(rcr.reviewer_notes, "Not justified")

    def test_mark_apply_failed(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        rcr.mark_apply_failed(self.reviewer, "Dependency error")
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.APPLY_FAILED)

    def test_clean_cross_tenant_role_fails(self):
        school2 = make_school(slug="school-2", name="School 2")
        role2 = make_role(school2)
        rcr = TenantRoleChangeRequest(
            tenant=self.school.tenant,
            requested_by=self.admin,
            target_role=role2,
            justification="Test",
        )
        with self.assertRaises(ValidationError):
            rcr.clean()

    def test_clean_empty_justification_fails(self):
        rcr = TenantRoleChangeRequest(
            tenant=self.school.tenant,
            requested_by=self.admin,
            target_role=self.role,
            justification="   ",
        )
        with self.assertRaises(ValidationError):
            rcr.clean()


class TenantRoleChangeDeltaItemModelTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.role = make_role(self.school)
        self.perm = make_permission("finance.invoice.view")
        self.rcr = make_role_change_request(self.school, self.admin, self.role)

    def test_create_delta_item(self):
        item = TenantRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )
        self.assertEqual(item.operation, "ADD")

    def test_str(self):
        item = TenantRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )
        self.assertIn("REMOVE", str(item))

    def test_unique_constraint(self):
        TenantRoleChangeDeltaItem.objects.create(
            request=self.rcr,
            permission=self.perm,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )
        with self.assertRaises(IntegrityError):
            TenantRoleChangeDeltaItem.objects.create(
                request=self.rcr,
                permission=self.perm,
                operation=TenantRoleChangeDeltaItem.Operation.ADD,
            )


# =============================================================================
# Platform roles are ordinary tenant roles on the codex tenant
# =============================================================================
class CodexTenantRoleModelTests(TestCase):
    def test_platform_role_is_codex_tenant_role(self):
        role = make_platform_role(name="Super Admin")
        self.assertEqual(role.tenant.slug, "codex")
        self.assertEqual(role.status, TenantRoleTemplate.Status.ACTIVE)
        self.assertTrue(role.is_system_role)

    def test_platform_role_permission(self):
        role = make_platform_role()
        perm = make_permission("system.config.view")
        rp = make_platform_role_permission(role, perm)
        self.assertTrue(rp.granted)

    def test_platform_assignment_and_revoke(self):
        user = make_vision_user()
        role = make_platform_role()
        a = make_platform_assignment(user, role)
        self.assertEqual(a.assignment_status, "ACTIVE")
        reviewer = make_vision_user(email="reviewer@test.com")
        a.revoke(by_user=reviewer, reason="Access revoked")
        self.assertEqual(a.assignment_status, "REVOKED")
        self.assertEqual(a.revoked_by, reviewer)

    def test_platform_change_request(self):
        user = make_vision_user()
        role = make_platform_role()
        rcr = make_platform_change_request(user, role)
        self.assertEqual(rcr.status, "PENDING")
        self.assertEqual(rcr.tenant.slug, "codex")
