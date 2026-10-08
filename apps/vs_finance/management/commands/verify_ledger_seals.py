"""Check every set of books against the figures sealed when its periods closed.

For each ledger entity (or those named with ``--entity``), recomputes every
current seal (:func:`vs_finance.seals.verify_entity`) and prints one line per
difference. Exits with an error when anything differs, so it can run on a
schedule as a health check; ``--open-incident`` also files one VIGIL incident
per affected set of books, which stays open until an operator resolves it.

Usage::

    python manage.py verify_ledger_seals
    python manage.py verify_ledger_seals --entity BRIGHTSTAR
    python manage.py verify_ledger_seals --open-incident

Read-only on the ledger: it never repairs a seal or a balance.
"""
from __future__ import annotations

from vs_finance.wording import agrees, counted

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify every closed period's recorded figures against the ledger."

    def add_arguments(self, parser):
        parser.add_argument("--entity", nargs="+", metavar="CODE",
                            help="Only these ledger entity codes.")
        parser.add_argument("--open-incident", action="store_true",
                            help="File a health incident for each set of books that differs.")

    def handle(self, *args, **options):
        from vs_finance.models import LedgerEntity
        from vs_finance.seals import describe, verify_entity

        entities = LedgerEntity.objects.select_related("tenant").order_by("code")
        if options.get("entity"):
            entities = entities.filter(code__in=[c.strip().upper() for c in options["entity"]])

        failed = []
        for entity in entities:
            result = verify_entity(entity)
            if result.ok:
                self.stdout.write(
                    f"  {entity.code}: "
                    f"{counted(len(result.checks), 'closed period or year', 'closed periods and years')} "
                    f"{agrees(len(result.checks), 'matches', 'match')}."
                )
                continue
            lines = [describe(check) for check in result.mismatches]
            if result.chain_breaks:
                lines.append(
                    f"{counted(len(result.chain_breaks), 'record of closed figures', 'records of closed figures')} "
                    f"{agrees(len(result.chain_breaks), 'does', 'do')} not follow the record before "
                    f"{agrees(len(result.chain_breaks), 'it', 'them')} "
                    f"(ids {', '.join(map(str, result.chain_breaks))})."
                )
            failed.append(entity.code)
            self.stdout.write(self.style.ERROR(f"  {entity.code}:"))
            for line in lines:
                self.stdout.write(f"    {line}")
            if options.get("open_incident"):
                self._open_incident(entity, lines)

        if failed:
            raise CommandError(
                f"Closed figures differ from the ledger for {counted(len(failed), 'set')} of books: "
                f"{', '.join(failed)}."
            )
        self.stdout.write(self.style.SUCCESS("Every closed figure matches the ledger."))

    @staticmethod
    def _open_incident(entity, lines):
        from vs_health.faults import report_configuration_fault
        from vs_health.models import Severity

        report_configuration_fault(
            fault_key=f"finance.sealed-figures.{entity.code}",
            title=f"Closed figures changed: {entity.code}",
            summary=" ".join(lines)[:2000],
            severity=Severity.SEV2, affected_tenant_count=1,
            who="Closed figures verification",
        )
