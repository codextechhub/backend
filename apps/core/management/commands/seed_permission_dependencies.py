"""Reconcile the backend-owned dependency graph between permission keys.

Every non-view action on a resource requires that resource's view permission
when one exists. Composite actions add the narrower operations they claim to
contain. The command also backfills existing and prebuilt roles so introducing
the graph does not remove working access from an established installation.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction


COMPOSITE_ACTION_REQUIREMENTS = {"approve_senior": ("approve",)}

EXPLICIT_DEPENDENCIES = {
    ("platform.permission_groups.create", "platform.permissions.view"),
    ("platform.permission_groups.update", "platform.permissions.view"),
    ("platform.permission_groups.delete", "platform.permissions.view"),
}


def expected_dependencies(permissions) -> set[tuple[str, str]]:
    """Return dependency pairs for the active permission rows supplied."""
    by_resource = defaultdict(dict)
    for permission in permissions:
        by_resource[(permission.module_id, permission.resource_id)][
            permission.action_id
        ] = permission.key

    available = {permission.key for permission in permissions}
    expected: set[tuple[str, str]] = {
        pair for pair in EXPLICIT_DEPENDENCIES if set(pair) <= available
    }
    for actions in by_resource.values():
        view_key = actions.get("view")
        if view_key:
            expected.update(
                (key, view_key)
                for action, key in actions.items()
                if action != "view"
            )

        for action, requirements in COMPOSITE_ACTION_REQUIREMENTS.items():
            key = actions.get(action)
            if key is None:
                continue
            expected.update(
                (key, actions[required])
                for required in requirements
                if required in actions
            )
    return expected


def dependency_closure(keys: set[str], graph: dict[str, set[str]]) -> set[str]:
    """Resolve every direct and transitive requirement for a key set."""
    required: set[str] = set()
    pending = list(keys)
    while pending:
        key = pending.pop()
        for dependency in graph.get(key, set()):
            if dependency in required:
                continue
            required.add(dependency)
            pending.append(dependency)
    return required


class Command(BaseCommand):
    help = "Reconcile permission dependencies and backfill existing role grants."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            GroupPermission,
            Permission,
            PermissionDependency,
            PrebuiltRolePermission,
            PrebuiltRoleTemplate,
            TenantRoleGroup,
            TenantRolePermission,
            TenantRoleTemplate,
        )

        permissions = list(
            Permission.objects.filter(
                is_active=True,
                module__is_active=True,
                resource__is_active=True,
                action__is_active=True,
            ).select_related("module", "resource", "action")
        )
        expected = expected_dependencies(permissions)
        existing_rows = list(PermissionDependency.objects.all())
        existing = {
            (row.permission_id, row.depends_on_id): row for row in existing_rows
        }

        stale_ids = [
            row.pk
            for pair, row in existing.items()
            if pair not in expected
        ]
        if stale_ids:
            PermissionDependency.objects.filter(pk__in=stale_ids).delete()

        missing = expected - set(existing)
        PermissionDependency.objects.bulk_create([
            PermissionDependency(
                permission_id=permission_key,
                depends_on_id=required_key,
            )
            for permission_key, required_key in sorted(missing)
        ])

        graph: dict[str, set[str]] = defaultdict(set)
        for permission_key, required_key in expected:
            graph[permission_key].add(required_key)

        role_grants = 0
        role_versions = 0
        for role in TenantRoleTemplate.objects.all().iterator():
            rows = {
                row.permission_id: row
                for row in TenantRolePermission.objects.filter(role=role)
            }
            direct = {key for key, row in rows.items() if row.granted}
            group_ids = TenantRoleGroup.objects.filter(role=role).values_list(
                "group_id", flat=True,
            )
            grouped = set(
                GroupPermission.objects.filter(
                    group_id__in=group_ids,
                ).values_list("permission_id", flat=True)
            )
            effective = direct | grouped
            required = dependency_closure(effective, graph)
            created_for_role = 0
            for key in sorted(required - effective):
                existing_row = rows.get(key)
                if existing_row is not None:
                    continue
                TenantRolePermission.objects.create(
                    role=role,
                    permission_id=key,
                    granted=True,
                    granted_by=None,
                )
                created_for_role += 1
            if created_for_role:
                role.version = (role.version or 1) + 1
                role.save(update_fields=["version", "updated_at"])
                role_grants += created_for_role
                role_versions += 1

        prebuilt_grants = 0
        for role in PrebuiltRoleTemplate.objects.all().iterator():
            direct = set(
                PrebuiltRolePermission.objects.filter(
                    prebuilt_role=role,
                ).values_list("permission_id", flat=True)
            )
            required = dependency_closure(direct, graph)
            for key in sorted(required - direct):
                _, created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=role,
                    permission_id=key,
                )
                prebuilt_grants += int(created)

        self.stdout.write(self.style.SUCCESS(
            "Permission dependencies reconciled: "
            f"{len(missing)} created, {len(stale_ids)} removed; "
            f"{role_grants} existing-role prerequisite grant(s) across "
            f"{role_versions} role(s); {prebuilt_grants} prebuilt-role "
            "prerequisite grant(s)."
        ))
