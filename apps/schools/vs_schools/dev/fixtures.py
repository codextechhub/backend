"""Building blocks for the development school fixtures.

Dev-only, and deliberately not under ``services/``: nothing in the product
imports this. It exists so ``seed_test_school`` (one school) and
``seed_onboarding_scenarios`` (a cast of them, each parked in a different state)
build the same shape of school instead of two shapes that drift.

Why a school is not one row: it needs a tenant, at least one branch, the
prebuilt role templates with their permissions copied in, a person holding each
role, a set of books and a provisioned control room. Miss any one and the school
looks fine in the admin, then answers 403 or an empty checklist to the first
screen that asks it a real question.

Never import this from application code, and never run it against production -
it writes known passwords.
"""
from __future__ import annotations

import zlib

from dataclasses import dataclass, field

DEFAULT_PASSWORD = "School@2025"

#: (main-branch street, annex street) pairs, so two branches at one school never
#: read as the same place. Real Lagos roads: a seeded address that looks made up
#: undermines the screen it is there to demonstrate.
_STREETS = [
    ("12 Awolowo Road, Ikoyi", "5 Admiralty Way, Lekki"),
    ("7 Bode Thomas Street, Surulere", "21 Opebi Road, Ikeja"),
    ("3 Ozumba Mbadiwe Avenue, Victoria Island", "18 Allen Avenue, Ikeja"),
    ("45 Isaac John Street, GRA Ikeja", "9 Ligali Ayorinde Street, Victoria Island"),
    ("2 Herbert Macaulay Way, Yaba", "31 Adeniran Ogunsanya Street, Surulere"),
    ("64 Marina Street, Lagos Island", "8 Kofo Abayomi Street, Victoria Island"),
    ("15 Oba Akran Avenue, Ikeja", "27 Ikorodu Road, Palmgrove"),
    ("39 Adeola Odeku Street, Victoria Island", "6 Ogunlana Drive, Surulere"),
    ("22 Awolowo Way, Ikeja", "11 Sanusi Fafunwa Street, Victoria Island"),
    ("50 Ahmadu Bello Way, Victoria Island", "4 Montgomery Road, Yaba"),
]


@dataclass
class BuiltSchool:
    """What a caller needs back to drive the school into a state."""

    school: object
    tenant: object
    admin: object
    branch_admin: object
    main_branch: object
    created: bool = False
    notes: list[str] = field(default_factory=list)


def build_school(
    *,
    slug: str,
    name: str,
    password: str = DEFAULT_PASSWORD,
    live: bool = False,
    extra_branch: bool = True,
    admin_name: tuple[str, str] = ("Adaeze", "Okonkwo"),
    branch_admin_name: tuple[str, str] = ("Chukwuemeka", "Eze"),
    with_books: bool = True,
    with_onboarding: bool = True,
    log=None,
) -> BuiltSchool:
    """Create or top up one fully provisioned school. Idempotent throughout.

    ``with_onboarding=False`` is not an oversight when a caller passes it: it is
    how the "never provisioned" scenario is built, and that state has to be
    reachable because the control room must tell it apart from "you have not
    started yet".
    """
    from django.contrib.auth import get_user_model
    from vs_rbac.models import TenantUserRoleAssignment
    from vs_rbac.services import provision_role_from_prebuilt
    from vs_tenants.models import Branch
    from vs_user.email_normalization import normalize_email

    from schools.vs_onboarding.services.provisioning import provision_onboarding

    from ..models import (
        Currency,
        OwnershipType,
        School,
        SchoolBranding,
        SchoolStatus,
        TermStructure,
    )
    from ..services.books import provision_books_for_school

    User = get_user_model()
    say = log or (lambda *_: None)
    notes: list[str] = []

    school = School.objects.filter(slug=slug).first()
    created = school is None
    if created:
        school = School(
            name=name,
            slug=slug,
            # Left at their defaults rather than blanked: ownership type, term
            # structure and currency are what the profile step asks the school
            # to confirm, and a school seeded with nothing set cannot reach the
            # states that need the step closed.
            ownership_type=OwnershipType.PRIVATE,
            term_structure=TermStructure.THREE_TERMS,
            currency=Currency.NGN,
            address=f"1 {name} Road, Lagos",
            status=SchoolStatus.ACTIVE if live else SchoolStatus.PENDING,
        )
        school.save()
        say(f"  + school {slug} ({school.code})")
    else:
        say(f"    school {slug} exists ({school.code})")

    tenant = school.tenant
    SchoolBranding.objects.get_or_create(school=school)

    # Two different streets per school, picked from the slug so a given school
    # always seeds the same pair. A branch with no address renders as "No
    # address on file" on the branches screen, which is honest but makes every
    # seeded school look half-built when the point of the seed is to show the
    # screen working.
    # crc32 rather than a character sum: summing characters collides constantly
    # on slugs of similar length, and two seeded schools sharing a street makes
    # the whole fixture look copy-pasted.
    street = _STREETS[zlib.crc32(slug.encode()) % len(_STREETS)]
    # No trailing state: ``state`` is its own column and the branches screen
    # joins address, state and country. Including it here rendered
    # "GRA Ikeja, Lagos, Lagos, Nigeria".
    main_address = street[0]
    annex_address = street[1]

    # Renamed BEFORE the get_or_create below, not instead of it. That call keys
    # on the name, so simply changing the string would leave every school seeded
    # earlier with its old "Main Campus" row and create a second "Main Branch"
    # beside it. They are branches, and the seed should say so.
    Branch.all_objects.filter(
        tenant=tenant, name=f"{name} Main Campus",
    ).update(name=f"{name} Main Branch")

    main, _ = Branch.all_objects.get_or_create(
        tenant=tenant, name=f"{name} Main Branch",
        defaults=dict(
            is_main=True, status="ACTIVE", country="Nigeria", state="Lagos",
            _type="Secondary", email=f"main@{slug}.example.com",
            address=main_address,
        ),
    )
    if extra_branch:
        Branch.all_objects.get_or_create(
            tenant=tenant, name=f"{name} Annex",
            defaults=dict(
                is_main=False, status="ACTIVE", country="Nigeria", state="Lagos",
                _type="Primary", email=f"annex@{slug}.example.com",
                address=annex_address,
            ),
        )

    # Top up a branch seeded before addresses were added here. ``get_or_create``
    # only applies its defaults on creation, so without this every school seeded
    # earlier keeps an empty address for good. Only a BLANK one is filled, so a
    # branch somebody has since given a real address is left alone.
    Branch.all_objects.filter(
        tenant=tenant, is_main=True, address="",
    ).update(address=main_address)
    Branch.all_objects.filter(
        tenant=tenant, is_main=False, address="",
    ).update(address=annex_address)

    # Roles copied from the prebuilt templates, never hand-assembled: a role
    # built here would carry whatever this file happened to list and would drift
    # from what a real school gets the first time a key is added.
    roles = {}
    for key in ("school_admin", "branch_admin"):
        role = provision_role_from_prebuilt(tenant=tenant, prebuilt_key=key)
        if role is None:
            raise RuntimeError(
                f"Prebuilt role '{key}' not found. Run seed_all_permissions first."
            )
        roles[key] = role
        if role.role_permissions.filter(granted=True).count() == 0:
            notes.append(f"role '{key}' carries no permissions - run seed_all_permissions")

    def make_user(local_part, first, last, branch, role_key):
        """One account, its password reset, and its role assignment.

        Scoped to this tenant: one address can be an account at several schools,
        so an unscoped lookup would find - and quietly re-point - somebody else's.
        """
        email = normalize_email(f"{local_part}@{slug}.example.com")
        user = User.objects.filter(email=email, tenant=tenant).first()
        if user is None:
            user = User.objects.create_user(
                email=email, password=password, first_name=first, last_name=last,
                status="ACTIVE", tenant=tenant, branch=branch,
            )
        # Re-set every run, so a forgotten password is one command away.
        user.set_password(password)
        user.is_active = True
        user.status = "ACTIVE"
        user.branch = branch
        user.save()

        assignment, _ = TenantUserRoleAssignment.objects.get_or_create(
            tenant=tenant, user=user, role=roles[role_key], branch=branch,
            defaults=dict(
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ),
        )
        assignment.assignment_status = (
            TenantUserRoleAssignment.AssignmentStatus.ACTIVE
        )
        assignment.save()
        return user

    # branch=None is a real posting meaning "the whole school", and it is what
    # the onboarding first-administrator check looks for: a person pinned to one
    # site is not the school's administrator.
    admin = make_user("admin", *admin_name, None, "school_admin")
    branch_admin = make_user("branch.admin", *branch_admin_name, main, "branch_admin")

    if with_books:
        # Before the control room: "confirm your set of books" is a required
        # step, and a school seeded without books starts blocked on a step it
        # has no way to clear.
        provision_books_for_school(school)

    if with_onboarding:
        provision_onboarding(tenant, actor=admin)

    return BuiltSchool(
        school=school, tenant=tenant, admin=admin, branch_admin=branch_admin,
        main_branch=main, created=created, notes=notes,
    )
