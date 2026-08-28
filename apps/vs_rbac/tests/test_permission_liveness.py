"""Emergency revocation tests for the permission registry's active states."""

from django.test import TestCase

from vs_rbac.evaluator import get_effective_permissions, resolve_users_with_permission
from vs_rbac.models import (
    GroupPermission,
    PermissionGroup,
    PermissionScope,
    TenantRoleGroup,
    UserPermissionOverride,
)

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_staff_user,
)


KEY = "finance.invoice.approve"


class PermissionLivenessTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="permission-liveness", name="Riverbank School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.user = make_staff_user(
            self.branch, email="ada@permission-liveness.test",
        )
        self.permission = make_permission(KEY)
        self.role = make_role(self.tenant, name="Invoice Approver")
        make_role_permission(self.role, self.permission)
        make_assignment(self.tenant, self.user, self.role)

    def _set_active(self, obj, value):
        obj.is_active = value
        obj.save(update_fields=["is_active", "updated_at"])

    def _assert_user_can(self):
        self.assertIn(
            KEY,
            get_effective_permissions(self.user, tenant=self.tenant),
        )

    def _assert_user_cannot(self):
        self.assertNotIn(
            KEY,
            get_effective_permissions(self.user, tenant=self.tenant),
        )

    def test_each_permission_vocabulary_switch_revokes_a_warm_cache(self):
        components = (
            self.permission,
            self.permission.module,
            self.permission.resource,
            self.permission.action,
        )

        for component in components:
            with self.subTest(component=component.__class__.__name__):
                self._assert_user_can()
                self._set_active(component, False)
                self._assert_user_cannot()
                self._set_active(component, True)
                self._assert_user_can()

    def test_group_deactivation_revokes_only_the_group_grant(self):
        group_user = make_staff_user(
            self.branch, email="group@permission-liveness.test",
        )
        group_role = make_role(self.tenant, name="Group Approver")
        group = PermissionGroup.objects.create(
            name="Finance Approvers",
            scope=PermissionScope.TENANT,
        )
        GroupPermission.objects.create(group=group, permission=self.permission)
        TenantRoleGroup.objects.create(role=group_role, group=group)
        make_assignment(self.tenant, group_user, group_role)

        self.assertIn(
            KEY, get_effective_permissions(group_user, tenant=self.tenant),
        )
        self._set_active(group, False)
        self.assertNotIn(
            KEY, get_effective_permissions(group_user, tenant=self.tenant),
        )

        self._assert_user_can()

    def test_inactive_permission_blocks_personal_allow_override(self):
        override_user = make_staff_user(
            self.branch, email="override@permission-liveness.test",
        )
        UserPermissionOverride.objects.create(
            tenant=self.tenant,
            user=override_user,
            permission=self.permission,
            mode=UserPermissionOverride.Mode.ALLOW,
            reason="Temporary cover.",
        )

        self.assertIn(
            KEY, get_effective_permissions(override_user, tenant=self.tenant),
        )
        self._set_active(self.permission, False)
        self.assertNotIn(
            KEY, get_effective_permissions(override_user, tenant=self.tenant),
        )

    def test_routing_applies_every_permission_vocabulary_switch(self):
        components = (
            self.permission,
            self.permission.module,
            self.permission.resource,
            self.permission.action,
        )

        for component in components:
            with self.subTest(component=component.__class__.__name__):
                self.assertIn(
                    self.user,
                    resolve_users_with_permission(self.tenant, None, KEY),
                )
                self._set_active(component, False)
                self.assertNotIn(
                    self.user,
                    resolve_users_with_permission(self.tenant, None, KEY),
                )
                self._set_active(component, True)

    def test_routing_drops_an_inactive_group_and_inactive_role(self):
        group_user = make_staff_user(
            self.branch, email="route-group@permission-liveness.test",
        )
        group_role = make_role(self.tenant, name="Routing Group Approver")
        group = PermissionGroup.objects.create(
            name="Routing Finance Approvers",
            scope=PermissionScope.TENANT,
        )
        GroupPermission.objects.create(group=group, permission=self.permission)
        TenantRoleGroup.objects.create(role=group_role, group=group)
        make_assignment(self.tenant, group_user, group_role)

        self.assertIn(
            group_user,
            resolve_users_with_permission(self.tenant, None, KEY),
        )
        self._set_active(group, False)
        self.assertNotIn(
            group_user,
            resolve_users_with_permission(self.tenant, None, KEY),
        )

        self.role.status = self.role.Status.INACTIVE
        self.role.save(update_fields=["status", "updated_at"])
        self.assertNotIn(
            self.user,
            resolve_users_with_permission(self.tenant, None, KEY),
        )

    def test_routing_ignores_a_legacy_restricted_allow_override(self):
        override_user = make_staff_user(
            self.branch, email="legacy-override@permission-liveness.test",
        )
        UserPermissionOverride.objects.create(
            tenant=self.tenant,
            user=override_user,
            permission=self.permission,
            mode=UserPermissionOverride.Mode.ALLOW,
            reason="Legacy row.",
        )
        self.permission.is_restricted = True
        self.permission.save(update_fields=["is_restricted", "updated_at"])

        self.assertNotIn(
            override_user,
            resolve_users_with_permission(self.tenant, None, KEY),
        )
