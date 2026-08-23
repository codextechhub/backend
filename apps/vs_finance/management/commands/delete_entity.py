"""Hard-delete ledger entities and all records that point to them.

This is an operator command for test/dev data cleanup.  It deliberately walks
reverse relationships before deleting each object so ``PROTECT`` relationships
inside the finance graph (for example child accounts and journal lines) do not
leave a partially deleted entity.

Usage::

    python manage.py delete_entity --entity LEKKI
    python manage.py delete_entity --entity LEKKI ABUJA
    python manage.py delete_entity --entity LEKKI --force

``--code`` is accepted as an alias for ``--entity``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


def _reverse_fk_relations(instance):
    """Yield every reverse FK/O2O relation, including hidden relationships."""

    for relation in instance._meta.get_fields(include_hidden=True):
        if not relation.auto_created or relation.concrete:
            continue
        if relation.one_to_many or relation.one_to_one:
            yield relation


def _delete_related_tree(instance, counts: dict[str, int], visited: set[tuple[str, object]]):
    """Delete ``instance`` after recursively deleting rows that reference it.

    Django's normal deletion collector stops as soon as it encounters ``PROTECT``.
    Entity-owned data uses ``PROTECT`` extensively, including between owned rows,
    so deleting only the models that point directly at ``LedgerEntity`` is not
    sufficient.  Walking all reverse FK/O2O edges gives us a dependency-safe
    leaf-to-root deletion order without following forward links to shared records
    such as the owning tenant, users, branches, or currencies.
    """

    identity = (instance._meta.label_lower, instance.pk)
    if identity in visited:
        return
    visited.add(identity)

    for relation in _reverse_fk_relations(instance):
        field_name = relation.field.name
        related = relation.related_model._base_manager.filter(**{field_name: instance})
        for child in list(related):
            _delete_related_tree(child, counts, visited)

    _, deleted_by_model = instance.delete()
    for label, count in deleted_by_model.items():
        counts[label] = counts.get(label, 0) + count


class Command(BaseCommand):
    help = "Hard-delete one or more ledger entities and all entity-owned data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--entity",
            "--code",
            dest="entity",
            nargs="+",
            required=True,
            metavar="CODE",
            help="One or more LedgerEntity codes to delete.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip confirmation prompt.",
        )

    def handle(self, *args, **options):
        from vs_finance.models import LedgerEntity

        codes = list(dict.fromkeys(code.strip().upper() for code in options["entity"]))
        entities = list(
            LedgerEntity.objects.select_related("tenant")
            .filter(code__in=codes)
            .order_by("code")
        )
        found_codes = {entity.code.upper() for entity in entities}
        not_found = [code for code in codes if code not in found_codes]

        if not entities:
            raise CommandError("None of the provided entity codes were found.")

        self.stdout.write(f"\n  {len(entities)} entity/entities to delete:\n")
        for entity in entities:
            self.stdout.write(
                f"    • {entity.code} · {entity.name}"
                f"  [{entity.kind} | tenant: {entity.tenant.slug}]\n"
            )

        if not options["force"]:
            confirm = input(
                "\n  Delete all of the above and ALL entity-owned data? "
                "Type YES to confirm: "
            )
            if confirm.strip() != "YES":
                self.stdout.write(self.style.WARNING("Aborted."))
                return

        total_counts: dict[str, int] = {}
        for entity in entities:
            code = entity.code
            with transaction.atomic():
                _delete_related_tree(entity, total_counts, set())
            self.stdout.write(f"  Deleted: {code}")

        self.stdout.write(self.style.SUCCESS(f"\nDeleted {len(entities)} entity/entities.\n"))
        for label, count in sorted(total_counts.items()):
            if count:
                self.stdout.write(f"    {label}: {count} deleted")

        if not_found:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {len(not_found)} entity code(s) not found (skipped):\n"
                    + "\n".join(f"    • {code}" for code in not_found)
                )
            )
        self.stdout.write("")
