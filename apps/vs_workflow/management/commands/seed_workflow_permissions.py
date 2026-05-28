"""Seed vs_workflow permission keys into vs_rbac (idempotent)."""
from django.core.management.base import BaseCommand
from vs_workflow.constants import (
    PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW, PERM_INSTANCE_SUBMIT,
    PERM_INSTANCE_VIEW, PERM_INSTANCE_CANCEL, PERM_ACTION_REVERSE,
)

PERMISSIONS = [
    (PERM_TEMPLATE_MANAGE, "Manage workflow templates"),
    (PERM_TEMPLATE_VIEW,   "View workflow templates (read-only)"),
    (PERM_INSTANCE_SUBMIT, "Submit workflow instances"),
    (PERM_INSTANCE_VIEW,   "View workflow instances"),
    (PERM_INSTANCE_CANCEL, "Cancel a workflow instance (admin)"),
    (PERM_ACTION_REVERSE,  "Reverse an approver action (admin)"),
]

class Command(BaseCommand):
    help = "Seed vs_workflow permission keys into vs_rbac (idempotent)."
    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
    def handle(self, *args, **options):
        try:
            from vs_rbac.services.catalogue import register_permission
        except ImportError:
            self.stderr.write(self.style.ERROR("vs_rbac not installed.")); return
        created = skipped = 0
        for key, label in PERMISSIONS:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] {key}"); continue
            result = register_permission(key=key, label=label, module="vs_workflow")
            if isinstance(result, tuple) and len(result)==2 and result[1]:
                created += 1; self.stdout.write(self.style.SUCCESS(f"created {key}"))
            else:
                skipped += 1; self.stdout.write(f"skipped {key}")
        if not options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Done. created={created} skipped={skipped}"))
