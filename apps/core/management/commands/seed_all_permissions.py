"""
Master permission seed — runs all permission seeds in dependency order.

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
2. seed_prebuilt_role_templates — school_admin / branch_admin prebuilt roles
3. seed_import_permissions   — import pipeline permissions → xvs_super_admin
4. seed_workflow_permissions — workflow engine permissions → both platform roles
5. seed_finance_permissions  — vs_finance permissions → both platform roles
6. seed_procurement_permissions — vs_procurement permissions → both platform roles
7. seed_payments_permissions — vs_payments permissions → both platform roles
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


SEED_STEPS: list[tuple[str, list]] = [
    # (management_command_name, extra_args)
    ("seed_actions",                 []),
    ("seed_prebuilt_role_templates", []),
    ("seed_import_permissions",      []),
    ("seed_workflow_permissions",    []),
    ("seed_finance_permissions",     []),
    ("seed_procurement_permissions", []),
    ("seed_payments_permissions",    []),
]


class Command(BaseCommand):
    help = "Run all permission seeds in dependency order (idempotent)."

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
            self.stdout.write(self.style.SUCCESS(
                "\n  ✔ All permission seeds completed successfully.\n"
            ))

    def _check_platform_roles(self):
        """Warn early if platform roles are missing so the user knows to run create_superuser."""
        try:
            from vs_rbac.models import PlatformRoleTemplate
            missing = [
                role_id for role_id in ("xvs_super_admin", "xvs_platform_admin")
                if not PlatformRoleTemplate.objects.filter(id=role_id).exists()
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
