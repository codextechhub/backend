"""Organise the school-facing permission keys into named, reusable bundles.

The keys themselves are registered by other seeders - ``seed_school_permissions``
(``school``/``academics``), ``seed_onboarding_permissions`` (``onboarding``),
``seed_notification_permissions`` (``communication``) and
``seed_ticket_permissions`` (``tickets``). Every one of those seeders knows its
own module and nothing else, so until now there was no place that said what the
school catalogue as a whole looks like. Reading the registry gave a flat list of
just under sixty dotted keys in five modules and no statement of which of them
belong together.

This command is that statement. It creates one ``PermissionGroup`` per coherent
job a school actually hands somebody - the calendar, student records, the role
catalogue, onboarding - and puts the keys for that job inside it.

Two things it deliberately does NOT do
--------------------------------------

**It grants nothing.** A ``PermissionGroup`` only confers anything once a
``TenantRoleGroup`` row attaches it to a role, and this command writes no such
row. Every school's effective permissions are byte-identical before and after a
run. The groups are a catalogue an administrator can pick from, not a change to
anybody's access.

**It never removes a key from a group.** Membership is topped up, never pruned,
for the same reason ``seed_school_permissions`` phase 3 never flips an explicit
deny: an administrator who has curated a bundle must not have that undone by a
re-run.

Reach: school-wide versus branch-scopable
-----------------------------------------

Each group declares a ``reach``, and its description says it out loud in the
first two words, because this is the distinction the ``SCHOOL_ADMIN`` and
``BRANCH_ADMIN`` user types have been standing in for and nothing else in the
schema records.

``SCHOOL_WIDE``
    The key is only meaningful for the whole school. There is one role
    catalogue, one set of school settings, one go-live request and one campus
    roster per school, so pinning such a grant to a branch either means nothing
    or means something dangerous. Corona Secondary School has three campuses; a
    "Bursar at Ikeja" who could edit *the school's* fee structure would be
    editing Lekki's and Yaba's fees too, and the branch on their grant would
    quietly not be doing the job the person granting it assumed it was doing.

``BRANCH_SCOPABLE``
    The key is meaningful for one campus. Student records, classes, the term
    calendar and the campus dashboard all narrow correctly through
    ``vs_rbac.scoping.visible_branch_ids``, so a grant pinned to Ikeja shows
    Ikeja's students and no others. Such a key is *also* legitimately held
    whole-tenant, by somebody who works across every campus - branch-scopable
    says the narrowing is available and means what it says, not that it is
    compulsory.

The reach lives in the description because there is nowhere better yet.
``Permission`` has ``scope`` (who may hold a key at all: PLATFORM or TENANT) and
``sensitivity_level`` (how dangerous it is), and neither answers this question.
``PrebuiltRoleTemplate.scope`` carries institution/branch/class/portal but that
is a property of a *role*, it is read by no code, and it cannot say anything
about an individual key. A declared column on ``Permission`` is the durable
answer and is a migration, so it is deliberately not attempted here;
:data:`SCHOOL_WIDE_KEYS` and :data:`BRANCH_SCOPABLE_KEYS` below are the single
source of truth for that follow-on work in the meantime.

Run order::

    python manage.py seed_all_permissions     # this command runs last

Safe to re-run - everything uses get_or_create. Supports ``--dry-run``.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


SCHOOL_WIDE = "School-wide"
BRANCH_SCOPABLE = "Branch-scopable"

# (group name, reach, description sentence, [permission keys])
#
# The name is what an administrator picks from a list, so it names the job and
# not the module: "Academic Calendar", not "academics.calendar".
SCHOOL_PERMISSION_GROUPS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # ── School-wide ───────────────────────────────────────────────────────────
    (
        "School Onboarding",
        SCHOOL_WIDE,
        "Run the onboarding control room, work the checklist and ask to go live. "
        "A school goes live once, so none of this narrows to a campus.",
        (
            "onboarding.progress.view",
            "onboarding.task.update",
            "onboarding.go_live.submit",
            "onboarding.go_live.view",
        ),
    ),
    (
        "School Profile and Settings",
        SCHOOL_WIDE,
        "Read and change the school's own profile and configuration.",
        (
            "school.settings.view",
            "school.settings.manage",
        ),
    ),
    (
        "Campus Administration",
        SCHOOL_WIDE,
        "Open, edit, close and list the school's campuses. Creating a campus is "
        "an act of the school, not of any one campus.",
        (
            "school.branches.view",
            "school.branches.create",
            "school.branches.update",
            "school.branches.manage",
        ),
    ),
    (
        "School Administrator Accounts",
        SCHOOL_WIDE,
        "Create, edit, suspend and reactivate the accounts that administer the "
        "school.",
        (
            "school.administrators.view",
            "school.administrators.create",
            "school.administrators.update",
            "school.administrators.suspend",
            "school.administrators.reactivate",
        ),
    ),
    (
        "School Roles and Permissions",
        SCHOOL_WIDE,
        "Build the school's role catalogue, assign roles and grant per-person "
        "exceptions. There is one catalogue per school.",
        (
            "school.roles.view",
            "school.roles.create",
            "school.roles.update",
            "school.roles.delete",
            "school.roles.assign",
            "school.user_overrides.view",
            "school.user_overrides.manage",
        ),
    ),
    (
        "School Impersonation",
        SCHOOL_WIDE,
        "Proxy another user inside this school and read the proxy trail. The "
        "most powerful bundle a school can hold.",
        (
            "school.impersonation.start",
            "school.impersonation.end",
            "school.impersonation.view",
        ),
    ),
    (
        "Fee Configuration",
        SCHOOL_WIDE,
        "Read and set the school's fee structure.",
        (
            "school.fees.view",
            "school.fees.manage",
        ),
    ),
    (
        "Academic Sessions",
        SCHOOL_WIDE,
        "Open, edit and close academic sessions and terms. A session is the "
        "school's, and every campus sits inside the same one.",
        (
            "academics.session.view",
            "academics.session.create",
            "academics.session.update",
            "academics.session.manage",
        ),
    ),
    (
        "Communication Settings",
        SCHOOL_WIDE,
        "Configure who receives which notifications, and read the delivery "
        "history.",
        (
            "communication.communication_permissions.enforce",
            "communication.message_activity.audit",
        ),
    ),

    # ── Branch-scopable ───────────────────────────────────────────────────────
    (
        "School Dashboard",
        BRANCH_SCOPABLE,
        "Read the overview. Held for one campus it shows that campus.",
        (
            "school.dashboard.view",
        ),
    ),
    (
        "Student Records",
        BRANCH_SCOPABLE,
        "Enrol, edit and manage students, including their restricted fields.",
        (
            "school.students.view",
            "school.students.create",
            "school.students.update",
            "school.students.manage",
            "school.students.view_sensitive",
        ),
    ),
    (
        "Teaching Staff Records",
        BRANCH_SCOPABLE,
        "Add, edit and manage teaching staff.",
        (
            "school.teachers.view",
            "school.teachers.create",
            "school.teachers.update",
            "school.teachers.manage",
        ),
    ),
    (
        "Class Management",
        BRANCH_SCOPABLE,
        "Create classes and put students and teachers in them.",
        (
            "academics.classes.view",
            "academics.classes.create",
            "academics.classes.update",
            "academics.classes.manage",
            "academics.classes.assign",
        ),
    ),
    (
        "Academic Calendar",
        BRANCH_SCOPABLE,
        "Add and edit calendar entries. A campus keeps its own dates inside the "
        "school's session.",
        (
            "academics.calendar.view",
            "academics.calendar.create",
            "academics.calendar.update",
            "academics.calendar.manage",
        ),
    ),
    (
        "Support Tickets",
        BRANCH_SCOPABLE,
        "Read, answer and manage support tickets raised from the school.",
        (
            "tickets.ticket.view",
            "tickets.ticket.update",
            "tickets.ticket.manage",
            "tickets.comment.post",
            "tickets.attachment.create",
            "tickets.report.view",
        ),
    ),
]


#: Every key the school catalogue calls school-wide, flattened. The follow-on
#: work that removes ``User.UserType.SCHOOL_ADMIN`` and ``BRANCH_ADMIN`` needs
#: exactly this split, so it is exported rather than left implicit in the table.
SCHOOL_WIDE_KEYS: frozenset[str] = frozenset(
    key
    for _name, reach, _description, keys in SCHOOL_PERMISSION_GROUPS
    if reach == SCHOOL_WIDE
    for key in keys
)

#: Every key the school catalogue calls branch-scopable, flattened.
BRANCH_SCOPABLE_KEYS: frozenset[str] = frozenset(
    key
    for _name, reach, _description, keys in SCHOOL_PERMISSION_GROUPS
    if reach == BRANCH_SCOPABLE
    for key in keys
)


class Command(BaseCommand):
    help = (
        "Group the school-facing permission keys into named bundles and record "
        "which of them are school-wide and which narrow to a campus. Grants "
        "nothing (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            try:
                with transaction.atomic():
                    self._run(dry_run=True)
                    raise _DryRunRollback()
            except _DryRunRollback:
                self.stdout.write(self.style.WARNING(
                    "\n  [dry-run] All changes rolled back. Nothing was written.\n"
                ))
        else:
            with transaction.atomic():
                self._run(dry_run=False)

    def _run(self, dry_run: bool):
        from vs_rbac.models import (
            GroupPermission,
            Permission,
            PermissionGroup,
            PermissionScope,
        )

        prefix = "  [dry-run]" if dry_run else " "

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Grouping the school permission catalogue...\n"
        ))

        created_groups = 0
        linked_keys = 0
        missing_keys: list[str] = []

        for name, reach, description, keys in SCHOOL_PERMISSION_GROUPS:
            # The reach leads the description so it survives into every screen
            # that renders a group, and into the API, without a schema change.
            full_description = f"{reach}. {description}"

            group, created = PermissionGroup.objects.get_or_create(
                name=name,
                defaults={
                    "description": full_description,
                    # Not left blank. An unclassified group is refused by
                    # TenantRoleGroup for every tenant that is not the platform,
                    # so a bundle seeded without this is one nobody can use.
                    # Every key below is TENANT-scoped by construction.
                    "scope": PermissionScope.TENANT,
                    "is_system": True,
                    "is_active": True,
                },
            )
            if created:
                created_groups += 1
                self.stdout.write(f"{prefix} + group: {name} [{reach}]")
            else:
                self.stdout.write(f"    {name} (exists)")

            if not group.is_system:
                # Somebody built a bundle of their own under this name before
                # the catalogue claimed it. Topping it up would silently widen
                # a grant they curated, so it is left exactly as it is and
                # named instead.
                self.stdout.write(self.style.WARNING(
                    f"  !  {name!r} already exists as a custom group; its "
                    "membership was left untouched."
                ))
                continue

            attached = 0
            for key in keys:
                if not Permission.objects.filter(key=key, is_active=True).exists():
                    # The key's own seeder has not run, or has been changed
                    # without this table being updated. Named rather than
                    # skipped in silence: a bundle that quietly loses a member
                    # is worse than one that fails to build.
                    missing_keys.append(key)
                    continue
                _, link_created = GroupPermission.objects.get_or_create(
                    group=group,
                    permission_id=key,
                )
                if link_created:
                    attached += 1
            linked_keys += attached
            if attached:
                self.stdout.write(f"{prefix}   + linked {attached} key(s)")

        if missing_keys:
            self.stdout.write(self.style.WARNING(
                f"\n  !  {len(missing_keys)} key(s) named here are not in the "
                f"registry and were skipped: {', '.join(sorted(set(missing_keys)))}\n"
                "     Run seed_all_permissions so every module registers first."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_groups} new group(s) of "
            f"{len(SCHOOL_PERMISSION_GROUPS)}, {linked_keys} new membership(s). "
            f"{len(SCHOOL_WIDE_KEYS)} school-wide and "
            f"{len(BRANCH_SCOPABLE_KEYS)} branch-scopable key(s) catalogued. "
            "No role was granted anything.\n"
        ))


class _DryRunRollback(Exception):
    """Internal sentinel to roll back the transaction in --dry-run mode."""
