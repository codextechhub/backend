from django.core.management.base import BaseCommand
from django.db import transaction


RESOURCES = [
    ("definition", [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"), ("archive", "SENSITIVE")]),
    ("value", [("view", "NORMAL"), ("update", "SENSITIVE")]),
    ("capability", [("view", "NORMAL"), ("manage", "SENSITIVE")]),
    ("entitlement", [("view", "SENSITIVE"), ("manage", "CRITICAL")]),
    ("override", [("view", "NORMAL"), ("manage", "SENSITIVE")]),
    ("audit", [("view", "SENSITIVE")]),
    ("export", [("create", "SENSITIVE")]),
]
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]


class Command(BaseCommand):
    help = "Seed configuration permissions and grant them to platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission, PermissionAction, PermissionModule, PermissionResource,
            PlatformRolePermission, PlatformRoleTemplate,
        )

        module, _ = PermissionModule.objects.get_or_create(
            name="config",
            defaults={"description": "Configuration, capability and entitlement management.", "is_active": True},
        )
        permissions = []
        for resource_name, actions in RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module, name=resource_name,
                defaults={"description": f"Configuration {resource_name} operations.", "is_active": True},
            )
            for action_name, sensitivity in actions:
                action, _ = PermissionAction.objects.get_or_create(
                    name=action_name,
                    defaults={"description": f"{action_name.title()} records.", "is_active": True},
                )
                key = f"config.{resource_name}.{action_name}"
                permission = Permission.objects.filter(key=key).first()
                if permission is None:
                    permission = Permission(
                        module=module, resource=resource, action=action,
                        description=f"{action_name.title()} configuration {resource_name} records.",
                        sensitivity_level=sensitivity,
                        is_restricted=sensitivity in {"SENSITIVE", "CRITICAL"},
                        is_active=True,
                    )
                    permission.save()
                permissions.append(permission)

        for role_id in PLATFORM_ROLE_IDS:
            role = PlatformRoleTemplate.objects.filter(pk=role_id).first()
            if role is None:
                self.stdout.write(self.style.WARNING(f"Role '{role_id}' does not exist; grants skipped."))
                continue
            for permission in permissions:
                PlatformRolePermission.objects.get_or_create(
                    role=role, permission=permission,
                    defaults={"granted": True, "granted_by": None},
                )
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(permissions)} configuration permissions."))
