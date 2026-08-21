"""Create (or top up) one school to develop and test against.

Why this exists rather than a shell snippet: a school is not one row. A usable
one needs a tenant, a main branch, the two prebuilt role templates with their
permissions copied in, a person holding each of those roles, a set of books and
a provisioned onboarding control room. Miss any of them and the school looks
fine in the admin and then answers 403 or an empty checklist to the first
screen that asks it a real question - which is exactly what happened to the
school this command was written after.

Idempotent. Everything is get_or_create or an explicit top-up, so re-running it
repairs a half-built school instead of failing on the half that exists.

    python manage.py seed_test_school
    python manage.py seed_test_school --slug bright-star --name "Bright Star Academy"
    python manage.py seed_test_school --live        # skip onboarding, go straight to active

Run the permission seeders first, or the roles will be created empty::

    python manage.py seed_all_permissions

Never run against production: it writes known passwords.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_SLUG = "bright-star"
DEFAULT_NAME = "Bright Star Academy"
DEFAULT_PASSWORD = "School@2025"


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
                "PENDING, which is the state the onboarding control room is "
                "for and the one worth testing."
            ),
        )

    def handle(self, *args, **options):
        slug = str(options["slug"]).strip().lower()
        name = str(options["name"]).strip()
        password = options["password"]
        live = options["live"]

        from django.contrib.auth import get_user_model
        from vs_rbac.models import TenantUserRoleAssignment
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_tenants.models import Branch
        from vs_user.email_normalization import normalize_email

        from schools.vs_onboarding.services.provisioning import provision_onboarding
        from ...models import (
            Currency,
            OwnershipType,
            School,
            SchoolBranding,
            SchoolStatus,
            TermStructure,
        )
        from ...services.books import provision_books_for_school

        User = get_user_model()

        with transaction.atomic():
            # ── The school. Its tenant is created by School.save(). ──────────
            school = School.objects.filter(slug=slug).first()
            if school is None:
                school = School(
                    name=name,
                    slug=slug,
                    # Left deliberately at their defaults rather than filled in:
                    # ownership type, term structure and currency are what the
                    # onboarding profile step asks the school to confirm, and a
                    # school seeded with every box already ticked cannot be used
                    # to test the step that ticks them.
                    ownership_type=OwnershipType.PRIVATE,
                    term_structure=TermStructure.THREE_TERMS,
                    currency=Currency.NGN,
                    address=f"1 {name} Road, Lagos",
                    status=SchoolStatus.ACTIVE if live else SchoolStatus.PENDING,
                )
                school.save()
                self.stdout.write(self.style.SUCCESS(f"  + school {slug} ({school.code})"))
            else:
                self.stdout.write(f"    school {slug} exists ({school.code})")

            tenant = school.tenant
            SchoolBranding.objects.get_or_create(school=school)

            # ── Branches. A school always has a main one; the second exists so
            #    a branch admin has somewhere to be pinned that is not "the
            #    whole school". ────────────────────────────────────────────────
            main, _ = Branch.all_objects.get_or_create(
                tenant=tenant, name=f"{name} Main Campus",
                defaults=dict(
                    is_main=True, status="ACTIVE", country="Nigeria",
                    state="Lagos", _type="Secondary",
                    email=f"main@{slug}.example.com",
                ),
            )
            second, _ = Branch.all_objects.get_or_create(
                tenant=tenant, name=f"{name} Annex",
                defaults=dict(
                    is_main=False, status="ACTIVE", country="Nigeria",
                    state="Lagos", _type="Primary",
                    email=f"annex@{slug}.example.com",
                ),
            )
            self.stdout.write(f"    branches: {main.name}, {second.name}")

            # ── Roles, copied from the prebuilt templates with their default
            #    permissions. Not hand-built: a role assembled here would carry
            #    whatever this file happened to list, and would drift from what
            #    a real school gets the first time a key is added. ─────────────
            roles = {}
            for key in ("school_admin", "branch_admin"):
                role = provision_role_from_prebuilt(
                    tenant=tenant, prebuilt_key=key,
                )
                if role is None:
                    raise CommandError(
                        f"Prebuilt role '{key}' not found. Run "
                        f"`python manage.py seed_all_permissions` first."
                    )
                roles[key] = role
                count = role.role_permissions.filter(granted=True).count()
                self.stdout.write(f"    role {key}: {count} permission(s)")
                if count == 0:
                    self.stdout.write(self.style.WARNING(
                        f"  !  '{key}' carries no permissions. Run "
                        f"seed_all_permissions, then re-run this command."
                    ))

            # ── The two people. ──────────────────────────────────────────────
            def make_user(local_part, first, last, branch, role_key):
                """One account, its password reset, and its role assignment.

                Scoped to this tenant on purpose: one address can be an account
                at several schools, so an unscoped lookup would find - and
                quietly re-point - somebody else's user.
                """
                email = normalize_email(f"{local_part}@{slug}.example.com")
                user = User.objects.filter(email=email, tenant=tenant).first()
                if user is None:
                    user = User.objects.create_user(
                        email=email, password=password,
                        first_name=first, last_name=last,
                        status="ACTIVE", tenant=tenant, branch=branch,
                    )
                    made = True
                else:
                    made = False
                # Re-set every run so a forgotten password is one command away.
                user.set_password(password)
                user.is_active = True
                user.status = "ACTIVE"
                user.branch = branch
                user.save()

                assignment, _ = TenantUserRoleAssignment.objects.get_or_create(
                    tenant=tenant, user=user, role=roles[role_key], branch=branch,
                    defaults=dict(
                        assignment_status=(
                            TenantUserRoleAssignment.AssignmentStatus.ACTIVE
                        ),
                    ),
                )
                assignment.assignment_status = (
                    TenantUserRoleAssignment.AssignmentStatus.ACTIVE
                )
                assignment.save()
                self.stdout.write(
                    f"    {'+' if made else ' '} {role_key}: {email}"
                )
                return user

            # branch=None is a real posting meaning "the whole school", and it
            # is what the onboarding first-administrator check looks for: a
            # person pinned to one site is not the school's administrator.
            admin = make_user("admin", "Adaeze", "Okonkwo", None, "school_admin")
            make_user("branch.admin", "Chukwuemeka", "Eze", main, "branch_admin")

            # ── Books, then the control room. Books first: "confirm your set of
            #    books" is a required onboarding step and a school seeded
            #    without them starts blocked on a step it cannot clear. ────────
            entity = provision_books_for_school(school)
            self.stdout.write(f"    books: {entity}")

            progress = provision_onboarding(tenant, actor=admin)
            self.stdout.write(f"    onboarding: {progress.readiness_state}")

        self.stdout.write(self.style.SUCCESS(
            f"\n  {name} is ready.\n"
            f"    address        {slug}.localhost:5199 (or ?tenant={slug})\n"
            f"    school admin   admin@{slug}.example.com / {password}\n"
            f"    branch admin   branch.admin@{slug}.example.com / {password}\n"
            f"    state          {'live' if live else 'pending - onboarding is open'}\n"
        ))
