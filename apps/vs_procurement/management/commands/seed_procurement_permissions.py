"""Seed vs_procurement permission keys and grant them to platform roles (idempotent).

Registers every ``procurement.<resource>.<action>`` key enforced by the
vs_procurement views into the RBAC Permission registry and grants them to the
platform admin roles.

Run order::

    python manage.py seed_actions
    python manage.py create_superuser
    python manage.py seed_procurement_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "procurement"
MODULE_DESCRIPTION = "Procure-to-pay: requisitions, sourcing, receipts, vendor invoicing and payments."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}

# sensitivity → whether the permission must flow through approvals / audit
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

# (resource_name, resource_label, [(action, sensitivity), ...])
PROCUREMENT_RESOURCES = [
    ("approval",       "spend approvals",       [("approve", "SENSITIVE"), ("approve_senior", "CRITICAL"), ("manage", "SENSITIVE")]),
    ("catalog_item",   "catalog items",         [("view", "NORMAL"), ("create", "NORMAL"), ("update", "NORMAL")]),
    ("category",       "vendor categories",     [("view", "NORMAL"), ("create", "NORMAL"), ("update", "SENSITIVE")]),
    ("contract",       "vendor contracts",      [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"),
                                                 ("activate", "SENSITIVE"), ("renew", "SENSITIVE"), ("terminate", "SENSITIVE")]),
    ("goods_receipt",  "goods-received notes",  [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"), ("post", "CRITICAL")]),
    ("purchase_order", "purchase orders",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"), ("submit", "SENSITIVE")]),
    ("quotation",      "vendor quotations",     [("view", "NORMAL"), ("create", "NORMAL"), ("update", "NORMAL"), ("submit", "SENSITIVE"), ("award", "SENSITIVE")]),
    ("report",         "procurement reports",   [("view", "NORMAL")]),
    ("requisition",    "purchase requisitions", [("view", "NORMAL"), ("create", "NORMAL"), ("update", "NORMAL"), ("submit", "SENSITIVE")]),
    ("rfq",            "requests for quotation", [("view", "NORMAL"), ("create", "NORMAL"), ("update", "NORMAL"), ("issue", "SENSITIVE")]),
    ("stock",          "stock items",           [("view", "NORMAL"), ("manage", "SENSITIVE"), ("issue", "SENSITIVE"), ("adjust", "SENSITIVE")]),
    ("vendor",         "vendors",               [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"),
                                                 ("view_sensitive", "SENSITIVE")]),
    ("vendor_invoice", "vendor invoices",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"), ("submit", "SENSITIVE"),
                                                 ("match", "SENSITIVE"), ("post", "CRITICAL")]),
    ("vendor_payment", "vendor payments",       [("view", "NORMAL"), ("create", "CRITICAL"), ("update", "CRITICAL"),
                                                 ("submit", "CRITICAL"), ("post", "CRITICAL"), ("cancel", "CRITICAL"),
                                                 ("reverse", "CRITICAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_procurement permission keys and grant them to platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            TenantRolePermission,
            TenantRoleTemplate,
        )
        from vs_tenants.models import Tenant

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        # ── Defensively ensure every action verb the spec needs exists ────────
        # seed_actions owns the canonical descriptions and normally runs first;
        # get_or_create never overwrites an existing row, so this is just a
        # safety net for standalone invocation.
        needed_actions = {a for _, _, acts in PROCUREMENT_RESOURCES for a, _ in acts}
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
        for resource_name, resource_label, actions in PROCUREMENT_RESOURCES:
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

        # ── Grant every key to the platform admin roles (codex tenant) ────────
        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  ⚠  Codex platform tenant not found — run migrations first; grants skipped."
            ))
        else:
            for role_id in PLATFORM_ROLE_IDS:
                role, _ = TenantRoleTemplate.objects.get_or_create(
                    tenant=codex,
                    key=role_id,
                    defaults={
                        "name": _PLATFORM_ROLE_NAMES.get(role_id, role_id),
                        "status": "ACTIVE",
                        "is_system_role": True,
                        "is_locked": True,
                    },
                )
                granted = 0
                for perm in all_perms:
                    _, link_created = TenantRolePermission.objects.get_or_create(
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
