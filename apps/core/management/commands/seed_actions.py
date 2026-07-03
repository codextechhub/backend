"""
Management command: seed_actions
==================================
Seeds the canonical PermissionAction vocabulary — the global list of action
verbs used as the suffix of every permission key (module.resource.<action>).

This is always the FIRST thing you run when building permissions from scratch.
Nothing depends on actions, but Permission cannot be created without them.

Safe to re-run (uses get_or_create). Existing records are never modified.

Usage
-----
    python manage.py seed_actions
    python manage.py seed_actions --dry-run
"""

from django.core.management.base import BaseCommand


# ── Canonical action list ─────────────────────────────────────────────────────
# Format: (name, description)
# name   → PermissionAction.name (PK, appears verbatim in permission keys)
# Order  → groups are for readability only; insertion order has no effect.

ACTIONS: list[tuple[str, str]] = [

    # ── Core read / write ─────────────────────────────────────────────────────
    ("view",       "Read or list records. Required by virtually every other action as a dependency."),
    ("create",     "Create a new record."),
    ("update",     "Modify an existing record's fields."),
    ("delete",     "Permanently remove a record (hard-delete or irreversible soft-delete)."),
    ("manage",     "Full control over a resource — implies view, create, update, and delete."),

    # ── Approval & lifecycle ──────────────────────────────────────────────────
    ("approve",    "Ratify or authorise a submitted record (scores, invoices, leave requests, etc.)."),
    ("reject",     "Decline or push back a submitted record with a reason."),
    ("submit",     "Submit a record for review or approval by another party."),
    ("cancel",     "Terminate an in-progress record or workflow instance (admin override)."),
    ("publish",    "Make a record visible to its intended audience (results, timetables, notices)."),
    ("archive",    "Move a record to an archived / read-only state without hard deletion."),
    ("suspend",    "Temporarily deactivate an account or entity."),
    ("reactivate", "Restore a previously suspended or deactivated entity."),

    # ── Data transfer & movement ──────────────────────────────────────────────
    ("export",     "Download data to CSV, XLSX, or PDF."),
    ("import",     "Bulk-upload records from a file."),
    ("transfer",   "Move a record between owners, branches, or contexts."),
    ("assign",     "Link a resource to another entity (student → class, user → route, etc.)."),

    # ── Specialised write operations ──────────────────────────────────────────
    ("record",     "Log a transaction or event entry (payments, sick-bay visits, attendance)."),
    ("enter",      "Input data into a form or score sheet (assessment scores, grades)."),
    ("mark",       "Mark attendance, or mark a record as done / reviewed."),
    ("verify",     "Confirm the authenticity of submitted evidence (payment proof, documents)."),
    ("reverse",    "Undo or void a previously recorded financial or operational transaction."),
    ("confirm",    "Finalise a pending action (confirm enrolment, confirm booking)."),
    ("process",    "Move a record through a workflow step (admissions, applications)."),
    ("generate",   "Produce a document or report on demand (ID cards, report cards)."),

    # ── Communication ─────────────────────────────────────────────────────────
    ("send",       "Dispatch a message — SMS, email, or push notification."),
    ("post",       "Publish an announcement or bulletin to a board or feed."),

    # ── Tracking & observation ────────────────────────────────────────────────
    ("track",      "Record attendance or presence at an event or location."),
    ("report",     "Generate a summary or analytics report (distinct from raw data export)."),

    # ── Finance-specific ──────────────────────────────────────────────────────
    ("waive",      "Grant a full or partial exemption from a fee or charge."),
    ("apply",      "Apply for something on behalf of self (leave requests, waivers)."),
    ("view_sensitive", "Read field-level sensitive data (bank account numbers, beneficiary details, salaries)."),
    ("reconcile",  "Match transactions against an external source (bank statements, sub-ledgers)."),
    ("edit",       "Edit a draft record's contents before submission or approval."),
    ("allocate",   "Apply a payment, credit note, or concession against open items."),
    ("settle",     "Settle or disburse an approved liability (expense claims, payables)."),
    ("acquire",    "Record the acquisition of a fixed asset onto the register."),
    ("depreciate", "Run depreciation against the fixed-asset register."),
    ("writeoff",   "Write off an uncollectable balance against a loss account."),
    ("activate",   "Activate a draft record into its live, operative state."),
    ("pay",        "Disburse funds to settle a payable, payroll, or tax liability."),
    ("close",      "Close an accounting period, locking it against further postings."),
    ("reopen",     "Re-open a closed accounting period back to open (audited)."),
    ("lock",       "Permanently seal a closed accounting period against any re-open."),
    ("establish",  "Fund a petty-cash float from a bank account (open or increase it)."),
    ("replenish",  "Replenish a petty-cash float back to its imprest level."),
    ("file",       "File a statutory return (VAT, WHT, PAYE) with the authority."),
    ("approve_senior", "Provide senior-tier approval for high-value records above threshold."),

    # ── Procurement-specific ──────────────────────────────────────────────────
    ("renew",      "Renew a contract or agreement into a successor term."),
    ("terminate",  "Terminate an active contract or agreement before expiry."),
    ("award",      "Award a quotation or tender to the winning vendor."),
    ("issue",      "Issue a document to its counterparty (RFQ, stock issue)."),
    ("match",      "Perform a multi-way match (PO ↔ GRN ↔ invoice)."),
    ("adjust",     "Record a manual adjustment (stock revaluation, corrections)."),

    # ── Library-specific ──────────────────────────────────────────────────────
    ("return",     "Record the return of a borrowed item."),

    # ── Platform / DevOps ────────────────────────────────────────────────────
    ("impersonate","Act as another user for audited support diagnostics (platform staff only)."),
    ("trigger",    "Initiate a deployment, job, or pipeline run."),
    ("run",        "Execute a migration, script, or background task."),
    ("escalate",   "Escalate a support ticket or incident to a higher tier."),
]


class Command(BaseCommand):
    help = "Seed the canonical PermissionAction vocabulary (global action verbs)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the DB.",
        )

    def handle(self, *args, **options):
        from vs_rbac.models import PermissionAction

        dry_run = options["dry_run"]
        created_count = 0
        skipped_count = 0

        self.stdout.write(f"\nSeeding {len(ACTIONS)} permission actions...\n")

        for name, description in ACTIONS:
            if dry_run:
                exists = PermissionAction.objects.filter(name=name).exists()
                status = "skip" if exists else "create"
                self.stdout.write(f"  [{status}]  {name}")
                if not exists:
                    created_count += 1
                else:
                    skipped_count += 1
                continue

            _, created = PermissionAction.objects.get_or_create(
                name=name,
                defaults={"description": description, "is_active": True},
            )
            if created:
                created_count += 1
                self.stdout.write(f"  created  {name}")
            else:
                skipped_count += 1
                self.stdout.write(f"  exists   {name}")

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] would create {created_count}, skip {skipped_count}\n"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. created={created_count}  already_existed={skipped_count}\n"
            ))
