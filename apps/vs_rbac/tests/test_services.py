"""
Tests for vs_rbac.services.apply_role_change_request (unified tenant workflow).
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleChangeRequest,
    TenantRoleChangeDeltaItem,
)
from vs_rbac.services import apply_role_change_request
from .helpers import (
    make_school,
    make_branch,
    make_vision_user,
    make_school_admin,
    make_permission,
    make_dependency,
    make_role,
    make_role_permission,
    make_role_change_request,
    make_platform_role,
    make_platform_role_permission,
    make_platform_change_request,
)


class ApplySchoolTenantRoleChangeRequestTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.reviewer = make_vision_user()
        self.role = make_role(self.school)

        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")
        self.perm_export = make_permission("finance.invoice.export")

        make_role_permission(self.role, self.perm_view)

    def _granted(self):
        return set(
            TenantRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )

    def test_add_permission(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_export,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        apply_role_change_request(rcr, self.reviewer, "Approved")

        rcr.refresh_from_db()
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.APPROVED)
        self.assertEqual(rcr.reviewer, self.reviewer)
        self.assertEqual(self._granted(), {"finance.invoice.view", "finance.invoice.export"})

    def test_remove_permission(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_view,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )

        apply_role_change_request(rcr, self.reviewer)
        self.assertEqual(self._granted(), set())

    def test_version_bumped(self):
        old_version = self.role.version
        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_export,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        apply_role_change_request(rcr, self.reviewer)
        self.role.refresh_from_db()
        self.assertEqual(self.role.version, old_version + 1)

    def test_dependency_violation_raises(self):
        make_dependency("finance.invoice.approve", "finance.invoice.view")

        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_view,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_approve,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        with self.assertRaises(ValidationError):
            apply_role_change_request(rcr, self.reviewer)

    def test_add_and_remove_combined(self):
        make_role_permission(self.role, self.perm_export)

        rcr = make_role_change_request(self.school, self.admin, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_export,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_approve,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        apply_role_change_request(rcr, self.reviewer)
        self.assertEqual(self._granted(), {"finance.invoice.view", "finance.invoice.approve"})


class ApplyPlatformTenantRoleChangeRequestTests(TestCase):
    def setUp(self):
        self.user = make_vision_user()
        self.reviewer = make_vision_user(email="reviewer@test.com")
        self.role = make_platform_role()
        self.perm_view = make_permission("system.config.view")
        self.perm_edit = make_permission("system.config.edit")

        make_platform_role_permission(self.role, self.perm_view)

    def _granted(self):
        return set(
            TenantRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )

    def test_add_permission(self):
        rcr = make_platform_change_request(self.user, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_edit,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        apply_role_change_request(rcr, self.reviewer, "OK")

        rcr.refresh_from_db()
        self.assertEqual(rcr.status, TenantRoleChangeRequest.Status.APPROVED)
        self.assertEqual(self._granted(), {"system.config.view", "system.config.edit"})

    def test_remove_permission(self):
        rcr = make_platform_change_request(self.user, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_view,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )

        apply_role_change_request(rcr, self.reviewer)
        self.assertEqual(self._granted(), set())

    def test_dependency_violation_raises(self):
        make_dependency("system.config.edit", "system.config.view")

        rcr = make_platform_change_request(self.user, self.role)
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_view,
            operation=TenantRoleChangeDeltaItem.Operation.REMOVE,
        )
        TenantRoleChangeDeltaItem.objects.create(
            request=rcr, permission=self.perm_edit,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

        with self.assertRaises(ValidationError):
            apply_role_change_request(rcr, self.reviewer)
