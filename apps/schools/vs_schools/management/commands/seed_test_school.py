"""Create (or top up) one school to develop and test against.

Thin wrapper over ``schools.vs_schools.dev.fixtures.build_school``, which is
shared with ``seed_onboarding_scenarios`` so the two build the same shape of
school. See that module for why a school is not one row.

Idempotent: re-running repairs a half-built school and resets both passwords.

    python manage.py seed_test_school
    python manage.py seed_test_school --slug bright-star --name "Bright Star Academy"
    python manage.py seed_test_school --live        # active, onboarding closed

Run the permission seeders first or the roles are created empty::

    python manage.py seed_all_permissions

Never run against production: it writes known passwords.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ...dev.fixtures import DEFAULT_PASSWORD, build_school

DEFAULT_SLUG = "bright-star"
DEFAULT_NAME = "Bright Star Academy"


class Command(BaseCommand):
    help = (
        "Create a fully provisioned test school with a school admin and a "
        "branch admin (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--slug", default=DEFAULT_SLUG)
        parser.add_argument("--name", default=DEFAULT_NAME)
        parser.add_argument("--password", default=DEFAULT_PASSWORD)
        parser.add_argument(
            "--live",
            action="store_true",
            help=(
                "Create the school already active. Without it the school is "
                "PENDING, which is the state the control room is for."
            ),
        )

    def handle(self, *args, **options):
        slug = str(options["slug"]).strip().lower()
        name = str(options["name"]).strip()
        password = options["password"]
        live = options["live"]

        try:
            with transaction.atomic():
                built = build_school(
                    slug=slug, name=name, password=password, live=live,
                    log=self.stdout.write,
                )
        except RuntimeError as error:
            raise CommandError(str(error)) from error

        for note in built.notes:
            self.stdout.write(self.style.WARNING(f"  !  {note}"))

        self.stdout.write(self.style.SUCCESS(
            f"\n  {name} is ready.\n"
            f"    address        {slug}.localhost:5199 (or ?tenant={slug})\n"
            f"    school admin   admin@{slug}.example.com / {password}\n"
            f"    branch admin   branch.admin@{slug}.example.com / {password}\n"
            f"    state          {'live' if live else 'pending - onboarding is open'}\n"
        ))
