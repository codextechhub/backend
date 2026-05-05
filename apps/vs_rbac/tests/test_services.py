"""
Tests for vs_rbac.services: apply_school_role_change_request, apply_platform_role_change_request.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.models import (
    SchoolRolePermission,
    SchoolRoleChangeRequest,
    SchoolRoleChangeDeltaItem,
    PlatformRolePermission,
    PlatformRoleChangeRequest,
    PlatformRoleChangeDeltaItem,
)
from vs_rbac.services import (
    apply_school_role_change_request,
    apply_platform_role_change_request,
)
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


class ApplySchoolRoleChangeRequestTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(self.branch)
        self.reviewer = make_vision_user()
        self.role = make_role(self.school)

        # Permissions
        self.perm_view = make_permission("finance.invoice.view")
        self.perm_approve = make_permission("finance.invoice.approve")
        self.perm_export = make_permission("finance.invoice.export")

        # Role starts with view permission
        make_role_permission(self.role, self.perm_view)

    def test_add_permission(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_export,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )

        apply_school_role_change_request(rcr, self.reviewer, "Approved")

        rcr.refresh_from_db()
        self.assertEqual(rcr.status, SchoolRoleChangeRequest.Status.APPROVED)
        self.assertEqual(rcr.reviewer, self.reviewer)

        perm_keys = set(
            SchoolRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, {"finance.invoice.view", "finance.invoice.export"})

    def test_remove_permission(self):
        rcr = make_role_change_request(self.school, self.admin, self.role)
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_view,
            operation=SchoolRoleChangeDeltaItem.Operation.REMOVE,
        )

        apply_school_role_change_request(rcr, self.reviewer)

        perm_keys = set(
            SchoolRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, set())

    def test_version_bumped(self):
        old_version = self.role.version
        rcr = make_role_change_request(self.school, self.admin, self.role)
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_export,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )

        apply_school_role_change_request(rcr, self.reviewer)

        self.role.refresh_from_db()
        self.assertEqual(self.role.version, old_version + 1)

    def test_dependency_violation_raises(self):
        make_dependency("finance.invoice.approve", "finance.invoice.view")

        rcr = make_role_change_request(self.school, self.admin, self.role)
        # Remove view, which approve depends on, and add approve
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_view,
            operation=SchoolRoleChangeDeltaItem.Operation.REMOVE,
        )
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_approve,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )

        with self.assertRaises(ValidationError):
            apply_school_role_change_request(rcr, self.reviewer)

    def test_add_and_remove_combined(self):
        make_role_permission(self.role, self.perm_export)

        rcr = make_role_change_request(self.school, self.admin, self.role)
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_export,
            operation=SchoolRoleChangeDeltaItem.Operation.REMOVE,
        )
        SchoolRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_approve,
            operation=SchoolRoleChangeDeltaItem.Operation.ADD,
        )

        apply_school_role_change_request(rcr, self.reviewer)

        perm_keys = set(
            SchoolRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, {"finance.invoice.view", "finance.invoice.approve"})


class ApplyPlatformRoleChangeRequestTests(TestCase):
    def setUp(self):
        self.user = make_vision_user()
        self.reviewer = make_vision_user(email="reviewer@test.com")
        self.role = make_platform_role()
        self.perm_view = make_permission("system.config.view")
        self.perm_edit = make_permission("system.config.edit")

        make_platform_role_permission(self.role, self.perm_view)

    def test_add_permission(self):
        rcr = make_platform_change_request(self.user, self.role)
        PlatformRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_edit,
            operation=PlatformRoleChangeDeltaItem.Operation.ADD,
        )

        apply_platform_role_change_request(rcr, self.reviewer, "OK")

        rcr.refresh_from_db()
        self.assertEqual(rcr.status, PlatformRoleChangeRequest.Status.APPROVED)

        perm_keys = set(
            PlatformRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, {"system.config.view", "system.config.edit"})

    def test_remove_permission(self):
        rcr = make_platform_change_request(self.user, self.role)
        PlatformRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_view,
            operation=PlatformRoleChangeDeltaItem.Operation.REMOVE,
        )

        apply_platform_role_change_request(rcr, self.reviewer)

        perm_keys = set(
            PlatformRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(perm_keys, set())

    def test_version_bumped(self):
        old_version = self.role.version
        rcr = make_platform_change_request(self.user, self.role)
        PlatformRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_edit,
            operation=PlatformRoleChangeDeltaItem.Operation.ADD,
        )

        apply_platform_role_change_request(rcr, self.reviewer)

        self.role.refresh_from_db()
        self.assertEqual(self.role.version, old_version + 1)

    def test_dependency_violation_raises(self):
        make_dependency("system.config.edit", "system.config.view")

        rcr = make_platform_change_request(self.user, self.role)
        PlatformRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_view,
            operation=PlatformRoleChangeDeltaItem.Operation.REMOVE,
        )
        PlatformRoleChangeDeltaItem.objects.create(
            request=rcr,
            permission=self.perm_edit,
            operation=PlatformRoleChangeDeltaItem.Operation.ADD,
        )

        with self.assertRaises(ValidationError):
            apply_platform_role_change_request(rcr, self.reviewer)
