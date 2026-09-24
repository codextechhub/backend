"""The broad action is removed without discarding existing access decisions."""

from importlib import import_module

from django.apps import apps as global_apps
from django.test import TestCase

from vs_rbac.models import (
    GroupPermission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    Permission,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    UserPermissionOverride,
)

from .helpers import (
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class BroadActionRetirementTests(TestCase):
    def test_existing_decisions_move_to_each_concrete_leave_action(self):
        tenant = make_school().tenant
        actor = make_school_admin(None, tenant=tenant)
        role = make_role(tenant, name="Leave Officer")
        old = make_permission("school.leave.manage")
        view = make_permission("school.leave.view")
        approve = make_permission("school.leave.approve")
        make_role_permission(role, old, granted=False)

        prebuilt = PrebuiltRoleTemplate.objects.create(
            key="leave_officer",
            name="Leave Officer",
            scope="institution",
        )
        PrebuiltRolePermission.objects.create(prebuilt_role=prebuilt, permission=old)
        UserPermissionOverride.objects.create(
            tenant=tenant,
            user=actor,
            permission=old,
            mode=UserPermissionOverride.Mode.DENY,
            reason="Temporary separation of duties",
        )
        PermissionDependency.objects.create(permission=old, depends_on=view)
        PermissionDependency.objects.create(permission=approve, depends_on=old)

        migration = import_module("vs_rbac.migrations.0027_manage_is_not_an_action")
        migration.replace_manage_permissions(global_apps, None)

        replacements = {"school.leave.update", "school.leave.cancel"}
        self.assertFalse(PermissionAction.objects.filter(name="manage").exists())
        self.assertFalse(type(old).objects.filter(key=old.key).exists())
        self.assertEqual(
            set(
                TenantRolePermission.objects.filter(role=role, granted=False)
                .values_list("permission_id", flat=True)
            ),
            replacements,
        )
        self.assertEqual(
            set(
                PrebuiltRolePermission.objects.filter(prebuilt_role=prebuilt)
                .values_list("permission_id", flat=True)
            ),
            replacements,
        )
        self.assertEqual(
            set(
                UserPermissionOverride.objects.filter(user=actor)
                .values_list("permission_id", flat=True)
            ),
            replacements,
        )
        for key in replacements:
            self.assertTrue(
                PermissionDependency.objects.filter(
                    permission_id=key,
                    depends_on=view,
                ).exists(),
            )
            self.assertTrue(
                PermissionDependency.objects.filter(
                    permission=approve,
                    depends_on_id=key,
                ).exists(),
            )

    def test_historical_manage_grants_reach_current_student_and_import_actions(self):
        tenant = make_school().tenant
        role = make_role(tenant, name="Data Administrator")
        student = make_permission("school.students.manage")
        template = make_permission("import.templates.manage")
        make_role_permission(role, student)
        make_role_permission(role, template)

        migration = import_module("vs_rbac.migrations.0027_manage_is_not_an_action")
        migration.replace_manage_permissions(global_apps, None)

        self.assertEqual(
            set(
                TenantRolePermission.objects.filter(role=role)
                .values_list("permission_id", flat=True)
            ),
            {
                "school.students.transition",
                "school.students.transfer",
                "school.students.suspend",
                "school.students.reactivate",
                "import.templates.update",
            },
        )

    def test_replacements_receive_their_concrete_sensitivity(self):
        old_group_permission = make_permission("platform.permission_groups.manage")
        make_permission("platform.health.manage")
        make_permission("config.entitlement.manage")
        group = PermissionGroup.objects.create(
            name="Permission group operators",
            scope="PLATFORM",
        )
        GroupPermission.objects.create(
            group=group,
            permission=old_group_permission,
        )
        existing = make_permission("platform.permission_groups.create")
        existing.sensitivity_level = "CRITICAL"
        existing.is_restricted = True
        existing.save(update_fields=["sensitivity_level", "is_restricted"])

        migration = import_module("vs_rbac.migrations.0027_manage_is_not_an_action")
        migration.replace_manage_permissions(global_apps, None)

        expected = {
            "platform.permission_groups.create": ("NORMAL", False),
            "platform.permission_groups.update": ("NORMAL", False),
            "platform.permission_groups.delete": ("SENSITIVE", True),
            "platform.health.create": ("SENSITIVE", False),
            "platform.health.update": ("SENSITIVE", False),
            "config.entitlement.update": ("CRITICAL", True),
            "config.entitlement.delete": ("CRITICAL", True),
        }
        self.assertEqual(
            {
                permission.key: (
                    permission.sensitivity_level,
                    permission.is_restricted,
                )
                for permission in Permission.objects.filter(key__in=expected)
            },
            expected,
        )
        self.assertEqual(
            set(
                GroupPermission.objects.filter(group=group)
                .values_list("permission_id", flat=True)
            ),
            {
                "platform.permission_groups.create",
                "platform.permission_groups.update",
            },
        )
