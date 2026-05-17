"""
backfill_school_admin_roles
===========================

One-shot repair command for school admins that were created before the
prebuilt "school_admin" role existed.

Background
----------
School onboarding calls ``provision_role_from_prebuilt(prebuilt_key="school_admin", ...)``
to fetch / create the per-school SchoolRoleTemplate before assigning it to
the primary admin user. Until the seed was fixed, no PrebuiltRoleTemplate
with that key existed, so the lookup returned ``None``, the role assignment
step was silently skipped, and the user landed in the system with:

    user.user_type = 'SCHOOL_ADMIN'
    user.role      = ''                          # blank
    SchoolUserRoleAssignment.objects.filter(user=user).count() == 0

This command walks every such user and:

  1. Calls provision_role_from_prebuilt(prebuilt_key="school_admin") for the
     user's school — get-or-creates the SchoolRoleTemplate and copies the
     permissions from the prebuilt template.
  2. Creates an active SchoolUserRoleAssignment.
  3. Stamps user.role with the role's name so the UI label matches the
     assignment.

Prerequisites
-------------
Run ``python manage.py seed_prebuilt_role_templates`` first so the
``school_admin`` PrebuiltRoleTemplate exists; otherwise this command will
abort with a clear message.

Usage
-----
    python manage.py backfill_school_admin_roles --dry-run
    python manage.py backfill_school_admin_roles
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_user.models import User
from vs_rbac.models import (
    PrebuiltRoleTemplate,
    SchoolUserRoleAssignment,
)
from vs_rbac.services import provision_role_from_prebuilt


PREBUILT_KEY = "school_admin"


class Command(BaseCommand):
    help = (
        "Backfill SchoolUserRoleAssignment for SCHOOL_ADMIN users created "
        "before the school_admin PrebuiltRoleTemplate existed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List affected users without writing any changes.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]

        if not PrebuiltRoleTemplate.objects.filter(key=PREBUILT_KEY, is_active=True).exists():
            self.stdout.write(self.style.ERROR(
                f"PrebuiltRoleTemplate '{PREBUILT_KEY}' is missing or inactive. "
                f"Run `python manage.py seed_prebuilt_role_templates` first."
            ))
            return

        # Find SCHOOL_ADMIN users with a school but no active role assignment
        # in that school. The "no assignment in their OWN school" condition
        # can't be expressed cleanly via a single ORM .exclude() clause
        # (it would compare against any school, not the user's), so filter
        # in Python after fetching the candidate set.
        candidates = [
            u for u in User.objects.filter(
                user_type=User.UserType.SCHOOL_ADMIN,
                school__isnull=False,
            ).select_related("school")
            if not SchoolUserRoleAssignment.objects.filter(
                user=u,
                school=u.school,
                assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).exists()
        ]

        total = len(candidates)
        self.stdout.write(f"Found {total} school admin(s) without an active role assignment.")

        if dry_run:
            for u in candidates:
                self.stdout.write(f"  [dry-run] {u.email} (school={u.school_id}) — would assign school_admin")
            self.stdout.write(self.style.WARNING("Dry-run complete. No changes written."))
            return

        backfilled = 0
        skipped = 0

        for u in candidates:
            try:
                with transaction.atomic():
                    role = provision_role_from_prebuilt(
                        school=u.school,
                        branch=None,
                        prebuilt_key=PREBUILT_KEY,
                        created_by=None,
                    )
                    if role is None:
                        self.stdout.write(self.style.ERROR(
                            f"  ✗ {u.email}: provision_role_from_prebuilt returned None"
                        ))
                        skipped += 1
                        continue

                    SchoolUserRoleAssignment.objects.create(
                        user=u,
                        role=role,
                        school=u.school,
                        assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
                        assigned_by=None,
                        reason_note="Backfill: school_admin role created retroactively.",
                    )

                    if not (u.role or "").strip():
                        u.role = role.name
                        u.save(update_fields=["role"])

                    backfilled += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ {u.email}: assigned {role.name} (school={u.school_id})"
                    ))
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                self.stdout.write(self.style.ERROR(f"  ✗ {u.email}: {exc}"))

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Backfilled={backfilled}, skipped={skipped}, total candidates={total}."
        ))
