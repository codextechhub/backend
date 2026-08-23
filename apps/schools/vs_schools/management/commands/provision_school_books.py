"""Give a set of books to every school that does not have one.

School creation now provisions books itself, but two kinds of school still need
this command: those created before that existed, and those whose provisioning
failed (it is deliberately best effort, so a school is created with no books
rather than not created at all).

There is deliberately **no** endpoint for this. Provisioning a set of books is
gated on ``finance.entity.create``, which a School Admin does not hold and should
not: an entity becomes the tenant of its own documents and numbering, and a
school that could mint one at will could mint a second and make the primary-books
lookup ambiguous. This is an operator command.

Usage::

    python manage.py provision_school_books --dry-run
    python manage.py provision_school_books
    python manage.py provision_school_books --school corona-secondary

Idempotent: a school that already keeps a primary set of books is skipped and
its books are never touched.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Provision a set of books for any school that has none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--school",
            dest="school_slugs",
            action="append",
            default=[],
            help="Limit to one school slug. Repeatable.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be provisioned without writing anything.",
        )

    def handle(self, *args, **options):
        from vs_finance.provisioning import primary_entity_for

        from schools.vs_schools.models import School
        from schools.vs_schools.services.books import (
            derive_entity_code,
            provision_books_for_school,
        )

        dry_run = options["dry_run"]
        slugs = options["school_slugs"]

        schools = School.objects.select_related("tenant").order_by("slug")
        if slugs:
            schools = schools.filter(slug__in=slugs)
            found = set(schools.values_list("slug", flat=True))
            for missing in sorted(set(slugs) - found):
                self.stderr.write(self.style.WARNING(f"No school with slug '{missing}'."))

        skipped = provisioned = failed = 0
        for school in schools:
            existing = primary_entity_for(school.tenant)
            if existing is not None:
                skipped += 1
                self.stdout.write(f"  = {school.slug}: already keeps books ({existing.code}).")
                continue

            if dry_run:
                provisioned += 1
                self.stdout.write(
                    f"  + {school.slug}: would provision books as "
                    f"{derive_entity_code(school)} in {school.currency or 'NGN'}."
                )
                continue

            entity = provision_books_for_school(school)
            if entity is None:
                failed += 1
                self.stderr.write(self.style.ERROR(
                    f"  ! {school.slug}: provisioning failed; see the log for the cause."
                ))
            else:
                provisioned += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  + {school.slug}: books provisioned as {entity.code}."
                ))

        verb = "would provision" if dry_run else "provisioned"
        summary = f"{verb} {provisioned}, skipped {skipped}, failed {failed}."
        style = self.style.WARNING if failed else self.style.SUCCESS
        self.stdout.write(style(summary))
