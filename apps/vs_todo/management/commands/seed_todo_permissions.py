"""Seed vs_todo permission keys and grant them to platform roles (idempotent).

Run order:
    python manage.py seed_actions             # adds view, manage, assign verbs
    python manage.py create_superuser         # ensures the platform roles exist
    python manage.py seed_todo_permissions

Safe to re-run — all operations use get_or_create.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


# (resource_name, resource_description, [(action_name, description, is_restricted), ...])
TODO_RESOURCES = [
    (
        "task",
        "ToDo accountability tasks",
        [
            ("view",   "View ToDo tasks and dashboards",                 False),
            ("manage", "Create, edit, complete, and delete ToDo tasks",  False),
            ("assign", "Assign a task down the organogram to a report",  False),
        ],
    ),
]

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]


class Command(BaseCommand):
    help = "Seed vs_todo permission keys and grant them to platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PlatformRolePermission,
            PlatformRoleTemplate,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding todo permissions...\n"))

        module, created = PermissionModule.objects.get_or_create(
            name="todo",
            defaults={"description": "ToDo — org accountability permissions", "is_active": True},
        )
        if created:
            self.stdout.write("  Created module: todo")

        created_count = 0
        all_perms = []

        for resource_name, resource_desc, actions in TODO_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={"description": resource_desc, "is_active": True},
            )

            for action_name, description, is_restricted in actions:
                action = PermissionAction.objects.filter(name=action_name).first()
                if not action:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠  Action '{action_name}' not found — run seed_actions first."
                    ))
                    continue

                expected_key = f"todo.{resource_name}.{action_name}"
                perm = Permission.objects.filter(key=expected_key).first()
                if perm:
                    self.stdout.write(f"    {expected_key} (exists)")
                else:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=description,
                        is_restricted=is_restricted,
                        sensitivity_level="SENSITIVE" if is_restricted else "NORMAL",
                        is_active=True,
                    )
                    perm.save()
                    created_count += 1
                    self.stdout.write(f"  + {perm.key}")

                all_perms.append(perm)

        # ── Grant to platform roles ────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Granting to platform roles...\n"))

        for role_id in PLATFORM_ROLE_IDS:
            try:
                role = PlatformRoleTemplate.objects.get(id=role_id)
            except PlatformRoleTemplate.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Role '{role_id}' not found — run create_superuser first."
                ))
                continue

            granted = 0
            for perm in all_perms:
                _, link_created = PlatformRolePermission.objects.get_or_create(
                    role=role,
                    permission=perm,
                    defaults={"granted": True, "granted_by": None},
                )
                if link_created:
                    granted += 1

            self.stdout.write(
                self.style.SUCCESS(f"  {role_id}: granted {granted} new permission(s).")
                if granted else
                f"  {role_id}: all permissions already assigned."
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, "
            f"{len(all_perms)} total todo keys registered.\n"
        ))
