"""Count the transactions that name no branch, and say what a backfill could do about them.

    python manage.py branch_audit                          # every tenant
    python manage.py branch_audit --tenant corona          # one tenant, by slug
    python manage.py branch_audit --entity CORONA          # one set of books
    python manage.py branch_audit --flagged-limit 20       # cap the flagged rows per model

Reads only; it never writes. For each set of books it prints, per transaction
model, how many rows have no branch, how many a backfill would derive from facts
on the books, how many a one-branch tenant would file under its only branch, and
how many need an administrator, followed by those rows with the reason for each.
A tenant that owns no branch is reported as blocked, with its branch listed as a
prerequisite. The derivation rules are :mod:`vs_finance.branch_derivation`'s;
``branch_backfill`` applies the same plan.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from vs_finance.branch_derivation import describe, entities, plan_entity


class Command(BaseCommand):
    help = "Report transactions with no branch, and how many a backfill could derive (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Tenant slug (default: every tenant).")
        parser.add_argument("--entity", help="Ledger entity code (default: every entity).")
        parser.add_argument(
            "--flagged-limit", type=int, default=0,
            help="Flagged rows printed per model (default 0: all of them).",
        )

    def handle(self, *args, **opts):
        books = entities(tenant_slug=opts.get("tenant"), entity_code=opts.get("entity"))
        blocked, blank, flagged = [], 0, 0
        for entity in books:
            plan = plan_entity(entity)
            for line in describe(plan, flagged_limit=opts["flagged_limit"]):
                self.stdout.write(line)
            self.stdout.write("")
            written = [p for p in plan.targets if p.target.has_branch_column]
            blank += sum(p.blank for p in written)
            flagged += sum(len(p.flags) for p in written)
            if plan.owns_no_branch and any(p.blank for p in written):
                blocked.append(f"{entity.tenant.name} [{entity.tenant.slug}] books {entity.code}")
        self.stdout.write(
            f"{len(books)} set(s) of books; {blank} unbranched row(s); "
            f"{flagged} need an administrator."
        )
        if blocked:
            self.stdout.write(self.style.WARNING("Prerequisites (tenant owns no branch; create one first):"))
            for line in blocked:
                self.stdout.write(f"  {line}")
