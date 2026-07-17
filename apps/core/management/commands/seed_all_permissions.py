"""
Master foundation seed — runs permission and required reference seeds in dependency order.

Run this on any environment (local or cloud) after migrations to ensure
all permission keys, module/resource definitions, and role grants are in sync.

    python manage.py seed_all_permissions

Prerequisites (must exist before running):
    - Database is migrated  (python manage.py migrate)
    - Platform roles exist  (python manage.py create_superuser  — first-time only)

All individual seeds are idempotent, so this command is safe to re-run.

Seed order
----------
1. seed_actions              — global PermissionAction vocabulary (verbs)
2. seed_prebuilt_role_templates — school_admin / branch_admin / teacher prebuilt roles
2b. seed_school_permissions  — school + academics modules → prebuilt-role defaults
                               + backfill existing school role templates
3. seed_platform_permissions — platform module (registry, roles, team, staff,
                               organogram, schools, branches, audit, dashboard)
                               → both platform roles
4. seed_import_permissions   — all import permissions → super-admin;
                               template management only → platform-admin
4b. seed_import              — canonical school + branch bulk-upload templates
5. seed_workflow_permissions — workflow engine permissions → both platform roles
6. seed_config_permissions   — vs_config permissions → both platform roles
7. seed_finance_permissions  — vs_finance permissions → both platform roles
8. seed_procurement_permissions — vs_procurement permissions → both platform roles
9. seed_payments_permissions — vs_payments permissions → both platform roles
10. seed_todo_permissions    — vs_todo permissions → both platform roles
11. seed_ticket_permissions  — vs_tickets permissions → platform and school roles
12. seed_notification_permissions — communication keys enforced by vs_notifications
                               → platform roles + school admin defaults
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


SEED_STEPS: list[tuple[str, list]] = [
    # (management_command_name, extra_args)
    ("seed_actions",                 []),
    ("seed_prebuilt_role_templates", []),
    ("seed_school_permissions",      []),
    ("seed_platform_permissions",    []),
    ("seed_import_permissions",      []),
    # Import templates are required reference data, not optional demo data.
    # Keep them in the master bootstrap so a migrated environment cannot expose
    # working import endpoints backed by an empty template catalogue.
    ("seed_import",                  []),
    ("seed_workflow_permissions",    []),
    ("seed_config_permissions",      []),
    ("seed_finance_permissions",     []),
    ("seed_procurement_permissions", []),
    ("seed_payments_permissions",    []),
    ("seed_todo_permissions",        []),
    ("seed_ticket_permissions",      []),
    ("seed_notification_permissions", []),
    ("seed_health", []),
]


class Command(BaseCommand):
    help = "Run all permission and required reference seeds in dependency order (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would run without executing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  ╔══════════════════════════════════════╗\n"
            "  ║      seed_all_permissions            ║\n"
            "  ╚══════════════════════════════════════╝\n"
        ))

        self._check_platform_roles()

        for i, (cmd, extra) in enumerate(SEED_STEPS, 1):
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n  [{i}/{len(SEED_STEPS)}] {cmd}\n  {'─' * 40}"
            ))
            if dry_run:
                self.stdout.write(f"  [dry-run] would call: python manage.py {cmd}")
                continue
            try:
                call_command(cmd, *extra, stdout=self.stdout, stderr=self.stderr)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"\n  ✗ {cmd} failed: {exc}\n"))
                raise

        if not dry_run:
            self._ensure_super_admin_has_every_permission()
            self.stdout.write(self.style.SUCCESS(
                "\n  ✔ All permission seeds completed successfully.\n"
            ))

    @transaction.atomic
    def _ensure_super_admin_has_every_permission(self):
        """Make the active permission registry explicit on xvs_super_admin.

        The evaluator already gives this role a runtime authorization bypass.
        Explicit rows are still required because the console receives and uses
        the effective permission-key list for navigation and action visibility.
        """
        from vs_rbac.models import Permission, TenantRolePermission, TenantRoleTemplate

        role = TenantRoleTemplate.objects.filter(
            key="xvs_super_admin",
            tenant__slug="codex",
            tenant__kind="PLATFORM",
        ).first()
        if role is None:
            self.stdout.write(self.style.WARNING(
                "\n  ⚠  xvs_super_admin role not found; full permission reconciliation skipped."
            ))
            return

        active_keys = set(
            Permission.objects.filter(is_active=True).values_list("key", flat=True)
        )
        role_rows = TenantRolePermission.objects.filter(
            role=role,
            permission_id__in=active_keys,
        )
        existing_keys = set(role_rows.values_list("permission_id", flat=True))
        role_rows.filter(granted=False).update(granted=True, updated_at=timezone.now())
        TenantRolePermission.objects.bulk_create(
            [
                TenantRolePermission(
                    role=role,
                    permission_id=key,
                    granted=True,
                    granted_by=None,
                )
                for key in active_keys - existing_keys
            ]
        )
        self.stdout.write(
            f"\n  ✔ Super Admin reconciled with all {len(active_keys)} active permissions."
        )

    def _check_platform_roles(self):
        """Warn early if platform roles are missing so the user knows to run create_superuser."""
        try:
            from vs_rbac.models import TenantRoleTemplate
            missing = [
                role_id for role_id in ("xvs_super_admin", "xvs_platform_admin")
                if not TenantRoleTemplate.objects.filter(
                    key=role_id, tenant__kind="PLATFORM"
                ).exists()
            ]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f"\n  ⚠  Platform role(s) not found: {', '.join(missing)}\n"
                    "     Permission grants will be skipped for missing roles.\n"
                    "     Run: python manage.py create_superuser\n"
                ))
            else:
                self.stdout.write(
                    "  ✔ Platform roles found: xvs_super_admin, xvs_platform_admin\n"
                )
        except Exception:
            pass
