"""Seed vs_payments permission keys and grant them to platform roles (idempotent).

Registers every ``payments.<resource>.<action>`` key enforced by the
vs_payments views into the RBAC Permission registry and grants them to the
platform admin roles.

Run order::

    python manage.py seed_actions
    python manage.py create_superuser
    python manage.py seed_payments_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "payments"
MODULE_DESCRIPTION = "Payment gateway collections, payouts and settlement reconciliation."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]

# sensitivity → whether the permission must flow through approvals / audit
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

# (resource_name, resource_label, [(action, sensitivity), ...])
PAYMENTS_RESOURCES = [
    ("collection",      "gateway collections", [("view", "NORMAL"), ("create", "CRITICAL")]),
    ("payout",          "gateway payouts",     [("view", "NORMAL"), ("create", "CRITICAL"),
                                               ("view_sensitive", "SENSITIVE")]),
    ("report",          "settlement reports",  [("view", "NORMAL")]),
    ("virtual_account", "virtual accounts",    [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("manage", "SENSITIVE"), ("view_sensitive", "SENSITIVE")]),
    # Bulk-payout-batch approval (maker-checker over the highest-risk cash-out path).
    ("payout_batch",    "bulk payout batches", [("submit", "SENSITIVE"), ("approve", "CRITICAL"),
                                                ("approve_high_value", "CRITICAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_payments permission keys and grant them to platform admin roles."

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

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        # ── Defensively ensure every action verb the spec needs exists ────────
        # seed_actions owns the canonical descriptions and normally runs first;
        # get_or_create never overwrites an existing row, so this is just a
        # safety net for standalone invocation.
        needed_actions = {a for _, _, acts in PAYMENTS_RESOURCES for a, _ in acts}
        for name in sorted(needed_actions):
            _, created = PermissionAction.objects.get_or_create(
                name=name,
                defaults={
                    "description": f"Auto-registered action verb '{name}'.",
                    "is_active": True,
                },
            )
            if created:
                self.stdout.write(
                    f"  + action '{name}' (auto-registered — run seed_actions for full description)"
                )

        # ── Module bucket ─────────────────────────────────────────────────────
        module, created = PermissionModule.objects.get_or_create(
            name=MODULE_NAME,
            defaults={"description": MODULE_DESCRIPTION, "is_active": True},
        )
        self.stdout.write(f"  module '{MODULE_NAME}' " + ("created" if created else "exists"))

        # ── Resources + permission keys ───────────────────────────────────────
        created_perms = 0
        all_perms = []
        for resource_name, resource_label, actions in PAYMENTS_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={
                    "description": f"{resource_label.capitalize()} ({MODULE_NAME}).",
                    "is_active": True,
                },
            )
            for action_name, sensitivity in actions:
                action = PermissionAction.objects.get(name=action_name)
                expected_key = f"{MODULE_NAME}.{resource_name}.{action_name}"
                verb = action_name.replace("_", " ")

                perm = Permission.objects.filter(key=expected_key).first()
                if perm is None:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=f"{verb.capitalize()} {resource_label}.",
                        sensitivity_level=sensitivity,
                        is_restricted=sensitivity in _RESTRICTED,
                        is_active=True,
                    )
                    perm.save()
                    created_perms += 1
                    self.stdout.write(f"  + {perm.key}  [{sensitivity}]")
                all_perms.append(perm)

        # ── Grant every key to the platform admin roles ───────────────────────
        for role_id in PLATFORM_ROLE_IDS:
            try:
                role = PlatformRoleTemplate.objects.get(id=role_id)
            except PlatformRoleTemplate.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  role '{role_id}' not found — run create_superuser first; grants skipped."
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
                f"  {role_id}: granted {granted} new key(s)." if granted
                else f"  {role_id}: all keys already assigned."
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total "
            f"'{MODULE_NAME}' keys registered.\n"
        ))
