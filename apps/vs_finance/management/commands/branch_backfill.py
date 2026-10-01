"""Give unbranched transactions the branch the books say they belong to.

    python manage.py branch_backfill                           # dry run, every tenant
    python manage.py branch_backfill --tenant corona           # dry run, one tenant
    python manage.py branch_backfill --tenant corona --apply   # write
    python manage.py branch_backfill --apply -v 2              # write, one line per change

Dry-run by default; ``--apply`` writes. The plan is ``branch_audit``'s, from
:mod:`vs_finance.branch_derivation`: each row takes the first branch its sources
give, a one-branch tenant files what is left under its only branch, and at a
tenant with several branches what is left stays blank and is listed for an
administrator. The main branch is never assumed. A tenant that owns no branch is
a data error, because every tenant must own one: it is reported as one, nothing
of it is written, and the run goes on to the next set of books.

Writes go in batches of ``--batch-size``, one transaction each. A row that
already has a branch is never touched, including one that gained it between
planning and writing, so the command is safe to re-run and a second run finds
nothing to do. Every change is recorded in the central audit trail in the same
transaction as the write.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from vs_finance.branch_derivation import (
    DEFAULT_BATCH_SIZE,
    apply_plan,
    describe,
    entities,
    plan_entity,
)


class Command(BaseCommand):
    help = "Backfill the branch on transactions that name none (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Tenant slug (default: every tenant).")
        parser.add_argument("--entity", help="Ledger entity code (default: every entity).")
        parser.add_argument("--apply", action="store_true", help="Write the branches (default: dry run).")
        parser.add_argument(
            "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
            help=f"Rows written per transaction (default {DEFAULT_BATCH_SIZE}).",
        )
        parser.add_argument(
            "--flagged-limit", type=int, default=0,
            help="Flagged rows printed per model (default 0: all of them).",
        )

    def handle(self, *args, **opts):
        if opts["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1.")
        apply = opts["apply"]
        verbose = opts["verbosity"] >= 2
        planned = written = skipped = 0

        def log(target_plan, pk, label, branch_id, how, *, names):
            handle = f" {label}" if label else ""
            self.stdout.write(
                f"    set {target_plan.target.model_label} #{pk}{handle} -> {names[branch_id]} (from {how})"
            )

        for entity in entities(tenant_slug=opts.get("tenant"), entity_code=opts.get("entity")):
            plan = plan_entity(entity)
            for line in describe(plan, flagged_limit=opts["flagged_limit"]):
                self.stdout.write(line)
            planned += sum(len(p.assign) for p in plan.targets if p.target.has_branch_column)
            if apply:
                names = plan.ctx.branch_names
                on_change = (lambda *a: log(*a, names=names)) if verbose else None
                result = apply_plan(plan, batch_size=opts["batch_size"], on_change=on_change)
                written += sum(result.written.values())
                skipped += sum(result.skipped.values())
            self.stdout.write("")

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"Wrote a branch on {written} row(s); left {skipped} that gained one since planning."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Dry run: would write a branch on {planned} row(s). Pass --apply to write."
            ))
