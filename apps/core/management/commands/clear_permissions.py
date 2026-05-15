"""
Management command: clear_permissions
======================================
Wipes all permission registry data and role data seeded into the RBAC
tables, in the correct dependency order so no PROTECT constraint fires.

Preserved (not touched):
  PrebuiltRoleTemplate  — Vision-owned role library

Cleared (in dependency order):
  1.  SchoolRoleChangeRequest   → cascades SchoolRoleChangeDeltaItem
  2.  PlatformRoleChangeRequest → cascades PlatformRoleChangeDeltaItem
  3.  SchoolUserRoleAssignment
  4.  PlatformUserRoleAssignment
  5.  SchoolRoleTemplate        → cascades SchoolRolePermission, SchoolRoleGroup
  6.  PlatformRoleTemplate      → cascades PlatformRolePermission, PlatformRoleGroup
  7.  PrebuiltRolePermission
  8.  GroupPermission
  9.  PermissionDependency
  10. Permission
  11. PermissionGroup
  12. PermissionResource
  13. PermissionModule
  14. PermissionAction

Usage
-----
    python manage.py clear_permissions
    python manage.py clear_permissions --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Wipe all RBAC permission/role data (preserves PrebuiltRoleTemplate)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print row counts that would be deleted without touching the DB.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip confirmation prompt (for non-interactive/CI environments).",
        )

    def handle(self, *args, **options):
        from vs_rbac.models import (
            SchoolRoleChangeRequest,
            PlatformRoleChangeRequest,
            SchoolUserRoleAssignment,
            PlatformUserRoleAssignment,
            SchoolRoleTemplate,
            PlatformRoleTemplate,
            PrebuiltRolePermission,
            GroupPermission,
            PermissionDependency,
            Permission,
            PermissionGroup,
            PermissionResource,
            PermissionModule,
            PermissionAction,
        )

        # ── deletion plan ────────────────────────────────────────────────────────
        # Each tuple: (label, queryset)
        # Order matters — children before parents where PROTECT is used.
        steps = [
            # Step 1-2: change-request workflows
            # Deleting SchoolRoleChangeRequest cascades SchoolRoleChangeDeltaItem.
            # Deleting PlatformRoleChangeRequest cascades PlatformRoleChangeDeltaItem.
            # This clears the PROTECT references those delta items hold on Permission.
            ("SchoolRoleChangeRequest",   SchoolRoleChangeRequest.objects.all()),
            ("PlatformRoleChangeRequest", PlatformRoleChangeRequest.objects.all()),

            # Step 3-4: user→role assignments (PROTECT blocks role template deletion)
            ("SchoolUserRoleAssignment",   SchoolUserRoleAssignment.objects.all()),
            ("PlatformUserRoleAssignment", PlatformUserRoleAssignment.objects.all()),

            # Step 5-6: role templates
            # Cascades: SchoolRolePermission, SchoolRoleGroup, PlatformRolePermission, PlatformRoleGroup
            ("SchoolRoleTemplate",   SchoolRoleTemplate.objects.all()),
            ("PlatformRoleTemplate", PlatformRoleTemplate.objects.all()),

            # Step 7-9: remaining permission links (explicit; cascades above may have
            # already cleared some of these, but get_or_create is idempotent)
            ("PrebuiltRolePermission", PrebuiltRolePermission.objects.all()),
            ("GroupPermission",        GroupPermission.objects.all()),
            ("PermissionDependency",   PermissionDependency.objects.all()),

            # Step 10: core registry — Permission is a PROTECT target for delta items,
            # which are now gone, so this is safe.
            ("Permission", Permission.objects.all()),

            # Step 11-14: vocabulary tables
            ("PermissionGroup",   PermissionGroup.objects.all()),
            ("PermissionResource", PermissionResource.objects.all()),
            ("PermissionModule",   PermissionModule.objects.all()),
            ("PermissionAction",   PermissionAction.objects.all()),
        ]

        dry_run = options["dry_run"]
        total = 0

        self.stdout.write("\nPermission data to be cleared:\n")
        self.stdout.write(f"  {'Table':<32} {'Rows':>8}\n")
        self.stdout.write(f"  {'-'*32} {'-'*8}\n")

        for label, qs in steps:
            count = qs.count()
            total += count
            self.stdout.write(f"  {label:<32} {count:>8,}")

        self.stdout.write(f"\n  {'TOTAL':<32} {total:>8,}\n")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[dry-run] No changes made.\n"))
            return

        if total == 0:
            self.stdout.write(self.style.SUCCESS("\nNothing to delete — tables already empty.\n"))
            return

        if not options["yes"]:
            confirm = input("\nDelete all rows listed above? [yes/N] ").strip().lower()
            if confirm != "yes":
                self.stdout.write(self.style.WARNING("Aborted.\n"))
                return

        with transaction.atomic():
            for label, qs in steps:
                deleted, _ = qs.delete()
                self.stdout.write(f"  deleted {deleted:>6,}  {label}")

        self.stdout.write(self.style.SUCCESS("\nDone. PrebuiltRoleTemplate records preserved.\n"))
