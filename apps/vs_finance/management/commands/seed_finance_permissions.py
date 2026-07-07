"""Seed vs_finance permission keys and grant them to platform roles (idempotent).

Registers every ``finance.<resource>.<action>`` key enforced by the vs_finance
views into the RBAC Permission registry, tagged with a sensitivity level, and
grants them to the platform admin roles.

Run order::

    python manage.py seed_actions                 # canonical action verbs
    python manage.py create_superuser             # platform roles
    python manage.py seed_finance_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "finance"
MODULE_DESCRIPTION = "General ledger, receivables, banking, payroll, tax and reporting."
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]

# sensitivity → whether the permission must flow through approvals / audit
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

# (resource_name, resource_label, [(action, sensitivity), ...])
# sensitivity: NORMAL (reads / master data) | SENSITIVE (state change) | CRITICAL (money / ledger-irreversible)
FINANCE_RESOURCES = [
    ("entity",       "ledger entities",        [("view", "NORMAL"), ("create", "SENSITIVE")]),
    ("account",      "chart-of-accounts",      [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE")]),
    ("costcenter",   "cost centers",           [("view", "NORMAL"), ("create", "NORMAL")]),
    ("dimension",    "reporting dimensions",   [("view", "NORMAL"), ("create", "NORMAL")]),
    ("currency",     "currencies",             [("view", "NORMAL"), ("create", "NORMAL")]),
    ("fxrate",       "FX rates",               [("view", "NORMAL"), ("create", "NORMAL")]),
    ("taxcode",      "tax codes",              [("view", "NORMAL"), ("create", "NORMAL")]),
    ("period",       "accounting periods",     [("view", "NORMAL"), ("close", "CRITICAL"),
                                                ("reopen", "CRITICAL"), ("lock", "CRITICAL")]),
    ("journal",      "journal entries",        [("view", "NORMAL"), ("post", "CRITICAL"), ("reverse", "CRITICAL"),
                                                # Approval-workflow keys: submit a draft for approval, and the
                                                # checker / high-value-controller approver keys the templates gate on.
                                                ("submit", "SENSITIVE"), ("approve", "CRITICAL"),
                                                ("approve_high_value", "CRITICAL")]),
    ("directentry",  "direct entries",         [("view", "NORMAL"), ("post", "CRITICAL")]),
    ("customer",     "customers / payers",     [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE")]),
    ("feestructure", "fee structures",         [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("edit", "SENSITIVE"), ("generate", "CRITICAL")]),
    ("invoice",      "customer invoices",      [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("writeoff", "SENSITIVE")]),
    ("payment",      "customer receipts",      [("view", "NORMAL"), ("create", "CRITICAL"),
                                                ("allocate", "SENSITIVE")]),
    ("report",       "financial reports",      [("view", "NORMAL")]),
    ("audit",        "finance audit logs",     [("view", "SENSITIVE")]),
    ("bankaccount",  "bank accounts",          [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("import", "SENSITIVE"),
                                                ("reconcile", "SENSITIVE"), ("view_sensitive", "SENSITIVE")]),
    ("budget",       "budgets",                [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("edit", "SENSITIVE"), ("approve", "SENSITIVE"),
                                                ("delete", "SENSITIVE")]),
    ("concession",   "concessions",            [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "SENSITIVE")]),
    ("creditnote",   "credit/debit notes",     [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("allocate", "SENSITIVE")]),
    ("dunning",      "dunning notices",        [("view", "NORMAL"), ("generate", "SENSITIVE"),
                                                ("send", "SENSITIVE"), ("manage", "SENSITIVE")]),
    ("expenseclaim", "expense claims",         [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("post", "SENSITIVE"), ("settle", "CRITICAL")]),
    ("fixedasset",   "fixed assets",           [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("acquire", "SENSITIVE"), ("depreciate", "SENSITIVE"),
                                                ("dispose", "CRITICAL")]),
    ("paymentplan",  "payment plans",          [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("activate", "SENSITIVE"), ("cancel", "SENSITIVE")]),
    ("payrollrun",   "payroll runs",           [("view", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("pay", "CRITICAL"),
                                                ("view_sensitive", "SENSITIVE")]),
    # The salary roster / structures (master data behind a run) get their own resource so
    # editing them is not conflated with running payroll. Every verb is SENSITIVE — even
    # listing exposes who earns what.
    ("salary",       "employee salaries & structures", [("view", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("delete", "SENSITIVE")]),
    # The fund/float (master data) and the voucher (a spend document) are distinct
    # resources — mirroring how every other finance document (invoice, expenseclaim …)
    # gets its own resource — so each verb is unambiguous.
    ("pettycash",        "petty cash funds",    [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("establish", "SENSITIVE"),
                                                ("replenish", "SENSITIVE")]),
    ("pettycashvoucher", "petty cash vouchers", [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("post", "SENSITIVE")]),
    ("refund",       "customer refunds",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "CRITICAL"),
                                                # Approval-workflow keys: submit a draft for approval, and the
                                                # checker / high-value-controller approver keys the templates gate on.
                                                ("submit", "SENSITIVE"), ("approve", "CRITICAL"),
                                                ("approve_high_value", "CRITICAL")]),
    ("tax",          "tax filings",            [("view", "NORMAL"), ("file", "SENSITIVE"),
                                                ("pay", "CRITICAL"), ("manage", "SENSITIVE")]),
]


class Command(BaseCommand):
    help = "Seed vs_finance permission keys and grant them to platform admin roles."

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
        needed_actions = {a for _, _, acts in FINANCE_RESOURCES for a, _ in acts}
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
        for resource_name, resource_label, actions in FINANCE_RESOURCES:
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
