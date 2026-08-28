"""Tests for the tenant RBAC role mutation services."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.exceptions import ValidationError as APIValidationError

from vs_rbac.models import (
    GroupPermission,
    PermissionGroup,
    PermissionScope,
    RBACAuditLog,
    TenantRoleGroup,
    TenantRolePermission,
    TenantRoleChangeRequest,
    TenantRoleChangeDeltaItem,
)
from vs_rbac.services import apply_role_change_request, set_role_access
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


class SetRoleAccessTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.actor = make_school_admin(self.branch)
        self.role = make_role(self.school, name="Accounts Officer")
        self.view_permission = make_permission("finance.invoice.view")
        self.update_permission = make_permission("finance.invoice.update")
        self.report_permission = make_permission("finance.report.view")
        make_role_permission(
            self.role, self.view_permission, granted_by=self.actor,
        )
        self.denied_permission = make_permission("payments.payout.approve")
        TenantRolePermission.objects.create(
            role=self.role,
            permission=self.denied_permission,
            granted=False,
            granted_by=self.actor,
        )
        self.group = PermissionGroup.objects.create(
            name="Finance Reporting", scope=PermissionScope.TENANT,
        )
        GroupPermission.objects.create(
            group=self.group, permission=self.report_permission,
        )

    def _granted(self):
        return set(
            TenantRolePermission.objects.filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )

    def test_records_actor_before_after_reason_and_group_derived_access(self):
        before_count = RBACAuditLog.objects.count()

        set_role_access(
            role=self.role,
            actor=self.actor,
            reason="Accounts officers now correct invoice coding errors.",
            permission_keys=[
                self.view_permission.key,
                self.update_permission.key,
            ],
            group_ids=[self.group.pk],
        )

        self.assertEqual(RBACAuditLog.objects.count(), before_count + 1)
        log = RBACAuditLog.objects.latest("created_at")
        self.assertEqual(log.actor, self.actor)
        self.assertEqual(
            log.before_data,
            {
                "direct_permission_keys": [self.view_permission.key],
                "denied_permission_keys": [self.denied_permission.key],
                "group_ids": [],
                "combined_permission_keys": [self.view_permission.key],
            },
        )
        self.assertEqual(
            log.diff_data["direct_permission_keys"]["after"],
            [self.update_permission.key, self.view_permission.key],
        )
        self.assertEqual(
            log.diff_data["group_ids"]["after"], [str(self.group.pk)],
        )
        self.assertEqual(
            log.diff_data["combined_permission_keys"]["after"],
            sorted([
                self.report_permission.key,
                self.update_permission.key,
                self.view_permission.key,
            ]),
        )
        self.assertEqual(
            log.metadata["reason"],
            "Accounts officers now correct invoice coding errors.",
        )
        self.assertIsNone(log.metadata["approval_reference"])
        self.assertEqual(
            set(TenantRoleGroup.objects.filter(role=self.role).values_list(
                "group_id", flat=True,
            )),
            {self.group.pk},
        )
        self.assertFalse(
            TenantRolePermission.objects.filter(
                role=self.role,
                permission=self.denied_permission,
            ).exists()
        )

    def test_group_only_change_preserves_explicit_denies(self):
        set_role_access(
            role=self.role,
            actor=self.actor,
            reason="Attach reporting without changing direct access.",
            group_ids=[self.group.pk],
        )

        denied = TenantRolePermission.objects.get(
            role=self.role,
            permission=self.denied_permission,
        )
        self.assertFalse(denied.granted)
        log = RBACAuditLog.objects.latest("created_at")
        self.assertEqual(
            log.before_data["denied_permission_keys"],
            [self.denied_permission.key],
        )
        self.assertEqual(
            log.diff_data["denied_permission_keys"]["after"],
            [self.denied_permission.key],
        )

    def test_explicit_deny_flips_an_existing_grant(self):
        set_role_access(
            role=self.role,
            actor=self.actor,
            reason="Invoice viewing is explicitly denied for this role.",
            permission_keys=[],
            denied_permission_keys=[self.view_permission.key],
        )

        row = TenantRolePermission.objects.get(
            role=self.role,
            permission=self.view_permission,
        )
        self.assertFalse(row.granted)
        log = RBACAuditLog.objects.latest("created_at")
        self.assertEqual(
            log.diff_data["denied_permission_keys"]["after"],
            [self.view_permission.key],
        )
        self.assertEqual(log.diff_data["combined_permission_keys"]["after"], [])

    def test_audit_failure_rolls_back_permissions_groups_and_version(self):
        original_version = self.role.version

        with patch(
            "vs_rbac.services.emit_audit_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                set_role_access(
                    role=self.role,
                    actor=self.actor,
                    reason="Replace invoice access.",
                    permission_keys=[self.update_permission.key],
                    group_ids=[self.group.pk],
                )

        self.role.refresh_from_db()
        self.assertEqual(self._granted(), {self.view_permission.key})
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=self.role,
                permission=self.denied_permission,
                granted=False,
            ).exists()
        )
        self.assertFalse(TenantRoleGroup.objects.filter(role=self.role).exists())
        self.assertEqual(self.role.version, original_version)

    def test_reason_is_required(self):
        with self.assertRaises(APIValidationError):
            set_role_access(
                role=self.role,
                actor=self.actor,
                reason=" ",
                permission_keys=[self.update_permission.key],
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

        log = RBACAuditLog.objects.filter(
            entity_type="TenantRoleTemplate",
            entity_id=str(self.role.pk),
            action_type="PERMISSION_CHANGED",
        ).latest("created_at")
        self.assertEqual(log.actor, self.reviewer)
        self.assertEqual(log.metadata["reason"], rcr.justification)
        self.assertEqual(log.metadata["approval_reference"], str(rcr.pk))
        self.assertEqual(log.metadata["source"], "approved_change_request")

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
