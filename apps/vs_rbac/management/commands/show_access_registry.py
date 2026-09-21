"""Print the backend-owned access registry in the shape people reason about it.

Permission definitions remain relational rows because grants, denials, roles and
the evaluator refer to those rows. Their ownership lives in backend seeders. The
command provides one inspection point after ``seed_all_permissions`` has brought
an environment into line with those definitions.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from vs_rbac.models import (
    Permission,
    PermissionGroup,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    display_label,
)


def registry_catalogue(*, module: str = "", include_inactive: bool = False) -> dict:
    """Build the permission, default-group and default-role overview."""
    permissions = Permission.objects.select_related(
        "module", "resource", "action",
    ).order_by("module_id", "resource__name", "action_id")
    grouped_permissions = Permission.objects.select_related(
        "module", "resource", "action",
    ).order_by("module_id", "resource__name", "action_id")
    role_permissions = PrebuiltRolePermission.objects.select_related(
        "permission", "permission__module", "permission__resource",
        "permission__action",
    ).order_by(
        "permission__module_id", "permission__resource__name",
        "permission__action_id",
    )

    if not include_inactive:
        active_definition = {
            "is_active": True,
            "module__is_active": True,
            "resource__is_active": True,
            "action__is_active": True,
        }
        permissions = permissions.filter(**active_definition)
        grouped_permissions = grouped_permissions.filter(**active_definition)
        role_permissions = role_permissions.filter(
            **{
                f"permission__{field}": value
                for field, value in active_definition.items()
            }
        )

    groups = PermissionGroup.objects.prefetch_related(
        Prefetch(
            "permissions",
            queryset=grouped_permissions,
        )
    ).order_by("name")
    roles = PrebuiltRoleTemplate.objects.prefetch_related(
        Prefetch(
            "default_permissions",
            queryset=role_permissions,
        )
    ).order_by("tier", "name")

    if not include_inactive:
        groups = groups.filter(is_active=True)
        roles = roles.filter(is_active=True)
    if module:
        permissions = permissions.filter(module_id=module)

    modules: list[dict] = []
    module_rows: dict[str, dict] = {}
    resource_rows: dict[tuple[str, int], dict] = {}
    for permission in permissions:
        module_row = module_rows.get(permission.module_id)
        if module_row is None:
            module_row = {
                "key": permission.module_id,
                "label": display_label(
                    permission.module.label, permission.module.name,
                ),
                "resources": [],
            }
            module_rows[permission.module_id] = module_row
            modules.append(module_row)

        resource_key = (permission.module_id, permission.resource_id)
        resource_row = resource_rows.get(resource_key)
        if resource_row is None:
            resource_row = {
                "key": permission.resource.name,
                "label": display_label(
                    permission.resource.label, permission.resource.name,
                ),
                "permissions": [],
            }
            resource_rows[resource_key] = resource_row
            module_row["resources"].append(resource_row)

        resource_row["permissions"].append({
            "key": permission.key,
            "action": permission.action_id,
            "label": permission.readable_label,
            "scope": permission.scope,
            "sensitivity": permission.sensitivity_level,
            "restricted": permission.is_restricted,
        })

    return {
        "modules": modules,
        "default_groups": [
            {
                "name": group.name,
                "description": group.description,
                "scope": group.scope,
                "permissions": [row.key for row in group.permissions.all()],
            }
            for group in groups
        ],
        "default_roles": [
            {
                "key": role.key,
                "name": role.name,
                "description": role.description,
                "scope": role.scope,
                "tier": role.tier,
                "permissions": [
                    row.permission_id for row in role.default_permissions.all()
                ],
            }
            for role in roles
        ],
    }


class Command(BaseCommand):
    help = (
        "Show permissions by module, resource and action, followed by the "
        "backend-owned default permission groups and role library."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--module",
            default="",
            help="Show permission definitions from one module only.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include inactive definitions.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print machine-readable JSON instead of the text overview.",
        )

    def handle(self, *args, **options):
        catalogue = registry_catalogue(
            module=options["module"],
            include_inactive=options["include_inactive"],
        )
        if options["json"]:
            self.stdout.write(json.dumps(catalogue, indent=2, sort_keys=True))
            return

        self.stdout.write("Permissions")
        for module in catalogue["modules"]:
            self.stdout.write(f"{module['label']} [{module['key']}]")
            for resource in module["resources"]:
                self.stdout.write(f"  {resource['label']} [{resource['key']}]")
                for permission in resource["permissions"]:
                    flags = [permission["scope"], permission["sensitivity"]]
                    if permission["restricted"]:
                        flags.append("RESTRICTED")
                    self.stdout.write(
                        f"    {permission['label']} [{permission['key']}] "
                        f"({' | '.join(flags)})"
                    )

        self.stdout.write("\nDefault permission groups")
        for group in catalogue["default_groups"]:
            self.stdout.write(f"{group['name']} ({group['scope']})")
            for key in group["permissions"]:
                self.stdout.write(f"  {key}")

        self.stdout.write("\nDefault role library")
        for role in catalogue["default_roles"]:
            self.stdout.write(
                f"{role['name']} [{role['key']}] "
                f"(tier {role['tier']}, {role['scope']})"
            )
            for key in role["permissions"]:
                self.stdout.write(f"  {key}")
