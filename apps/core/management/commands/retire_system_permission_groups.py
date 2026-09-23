"""Remove backend-created permission groups without changing role access.

Permission groups are administrator-owned shortcuts. Older releases seeded
system groups, and roles may still reference them. Each effective group grant
is copied to a direct role grant before the system group is deleted. Existing
direct denies remain untouched.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Convert system-group grants to direct grants and delete the groups."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            GroupPermission,
            PermissionGroup,
            TenantRoleGroup,
            TenantRolePermission,
            TenantRoleTemplate,
        )

        system_groups = list(
            PermissionGroup.objects.filter(is_system=True).values_list(
                "id", flat=True,
            )
        )
        if not system_groups:
            self.stdout.write("No backend-created permission groups remain.")
            return

        copied = 0
        changed_roles: set = set()
        attachments = TenantRoleGroup.objects.filter(
            group_id__in=system_groups,
        ).values_list("role_id", "group_id")
        members = defaultdict(set)
        for group_id, permission_id in GroupPermission.objects.filter(
            group_id__in=system_groups,
        ).values_list("group_id", "permission_id"):
            members[group_id].add(permission_id)

        for role_id, group_id in attachments:
            changed_roles.add(role_id)
            existing = {
                row.permission_id: row
                for row in TenantRolePermission.objects.filter(
                    role_id=role_id,
                    permission_id__in=members[group_id],
                )
            }
            for permission_id in members[group_id]:
                if permission_id in existing:
                    continue
                TenantRolePermission.objects.create(
                    role_id=role_id,
                    permission_id=permission_id,
                    granted=True,
                    granted_by=None,
                )
                copied += 1

        deleted_groups = len(system_groups)
        PermissionGroup.objects.filter(id__in=system_groups).delete()
        for role in TenantRoleTemplate.objects.filter(id__in=changed_roles):
            role.version = (role.version or 1) + 1
            role.save(update_fields=["version", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted_groups} backend-created permission group(s), "
            f"copied {copied} direct role grant(s), and refreshed "
            f"{len(changed_roles)} role version(s)."
        ))
