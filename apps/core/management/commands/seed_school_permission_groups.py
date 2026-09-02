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

**It never puts a restricted key in a group.** Restricted authority must travel
through a reviewed role change, while attaching a group takes effect
immediately. Re-running removes legacy restricted memberships so a bundle can
never become an approval bypass.

Reach: school-wide versus branch-scopable
-----------------------------------------

Each group declares a ``reach``, and its description says it out loud in the
first two words, because this is the distinction the ``SCHOOL_ADMIN`` and
``BRANCH_ADMIN`` user types used to stand in for. Those personas are gone, and
nothing else in the schema records the distinction.

``SCHOOL_WIDE``
    The key is only meaningful for the whole school. There is one role
    catalogue, one set of school settings, one go-live request and one branch
    roster per school, so pinning such a grant to a branch either means nothing
    or means something dangerous. Corona Secondary School has three branches; a
    "Bursar at Ikeja" who could edit *the school's* fee structure would be
    editing Lekki's and Yaba's fees too, and the branch on their grant would
    quietly not be doing the job the person granting it assumed it was doing.

``BRANCH_SCOPABLE``
    The key is meaningful for one branch. Student records, classes, the term
    calendar and the branch dashboard all narrow correctly through
    ``vs_rbac.scoping.visible_branch_ids``, so a grant pinned to Ikeja shows
    Ikeja's students and no others. Such a key is *also* legitimately held
    whole-tenant, by somebody who works across every branch - branch-scopable
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
        "A school goes live once, so none of this narrows to a branch.",
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
            "school.profile.view",
            "school.profile.update",
            "school.settings.view",
            "school.settings.manage",
        ),
    ),
    (
        "Branch Administration",
        SCHOOL_WIDE,
        "See and administer the branches this school already has. Opening a new "
        "branch, or changing one's details, is CodeX's to do - ask the team.",
        (
            "school.branches.view",
            "school.branches.manage",
            # ``create`` and ``update`` are deliberately absent - see
            # DELIBERATELY_UNGROUPED below, which is where that decision is
            # recorded in a form the exhaustiveness test can read.
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
            "school.roles.approve",
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
        "Read the school's fee structures, concessions and payment plans. A fee "
        "structure prices a term for the whole school, so this does not narrow "
        "to a branch.",
        (
            # The real gate. ``finance.feestructure.view`` is what
            # ``vs_finance.views_ar`` actually checks (views_ar.py:1103); the
            # two ``school.fees.*`` keys below are enforced by NOTHING on the
            # server. This group used to contain only those two, so granting it
            # gave a bursar the appearance of fee access and none of the
            # substance: every finance endpoint still refused her, because she
            # held no ``finance.*`` key at all.
            "finance.feestructure.view",
            "finance.concession.view",
            "finance.paymentplan.view",
            "finance.paymentplan.create",
            # Kept deliberately. school-fe maps permission codes 100601 and
            # 100608 to these two keys (src/permissions/index.ts), so dropping
            # them here would hide the frontend's fee navigation from the very
            # people this fix is meant to enable. They are inert on the server
            # and should be retired once the frontend reads the finance keys.
            "school.fees.view",
            "school.fees.manage",
        ),
    ),
    (
        "Fee Collections",
        BRANCH_SCOPABLE,
        "Read the bills raised, the money received against them, and what is "
        "still owed. A bursar granted this at one branch sees that branch's "
        "families and no others.",
        (
            "finance.invoice.view",
            "finance.payment.view",
            "finance.customer.view",
            "finance.creditnote.view",
            "finance.refund.view",
            "finance.writeoff.view",
            "finance.dunning.view",
        ),
    ),
    (
        "Finance Reports",
        BRANCH_SCOPABLE,
        "Read the finance reports. Narrows to a branch for somebody who runs "
        "one, and is held whole-school by the person who closes the books.",
        (
            "finance.report.view",
        ),
    ),
    (
        "Banking",
        SCHOOL_WIDE,
        "Read the school's bank accounts and their balances. The accounts "
        "belong to the school rather than to any one branch.",
        (
            "finance.bankaccount.view",
        ),
    ),
    (
        "Expenses and Petty Cash",
        BRANCH_SCOPABLE,
        "Raise and read expense claims, and read the petty cash float and its "
        "vouchers. Each branch runs its own float.",
        (
            "finance.expenseclaim.view",
            "finance.expenseclaim.create",
            "finance.pettycash.view",
            "finance.pettycashvoucher.view",
        ),
    ),
    (
        "Fixed Assets",
        BRANCH_SCOPABLE,
        "Read the school's capitalised assets: the bus, the generator, the "
        "buildings. An asset sits at a branch, so this narrows.",
        (
            "finance.fixedasset.view",
        ),
    ),
    (
        "Budgets and Cost Centres",
        SCHOOL_WIDE,
        "Read the budget and maintain the cost centres it is spent against. "
        "One budget covers the school.",
        (
            "finance.budget.view",
            "finance.costcenter.view",
            "finance.costcenter.create",
        ),
    ),
    (
        "The Ledger",
        SCHOOL_WIDE,
        "Read-only access to the books themselves: the chart of accounts, the "
        "journals, the direct entries and the accounting periods. Posting to "
        "any of them is restricted and travels through a role change.",
        (
            "finance.entity.view",
            "finance.account.view",
            "finance.journal.view",
            "finance.directentry.view",
            "finance.period.view",
            "finance.settings.view",
        ),
    ),
    (
        "Tax",
        SCHOOL_WIDE,
        "Read the school's tax position and maintain its tax codes. Tax is "
        "filed for the school, not per branch.",
        (
            "finance.tax.view",
            "finance.taxcode.view",
            "finance.taxcode.create",
        ),
    ),
    (
        "Supplier Bills and Payments",
        BRANCH_SCOPABLE,
        "Read what the school has been billed by its suppliers and what has "
        "been paid against those bills. A bill is raised at a branch or "
        "school-wide, so this narrows.",
        (
            # The money end of procurement only. Requisitions, approvals, RFQs
            # and goods receipts are deliberately absent: a vice principal
            # ordering textbooks is doing procurement, not finance, and must
            # not need a finance bundle to do it.
            #
            # Every verb here is ``view`` or ``attach``. Creating a supplier
            # payment is CRITICAL and posting a bill is CRITICAL, so neither is
            # groupable - see the payroll note below for why that matters.
            "procurement.vendor_invoice.view",
            "procurement.vendor_invoice.attach",
            "procurement.vendor_payment.view",
            "procurement.vendor_payment.attach",
            "procurement.vendor.view",
            "procurement.report.view",
        ),
    ),
    (
        "Money In",
        SCHOOL_WIDE,
        "Read the money arriving from parents: gateway checkouts, what settled "
        "against them, and the school's dedicated funding accounts. Reading "
        "only - starting a checkout or opening an account is restricted.",
        (
            # Collections are the school's own cash-in, and a bursar cannot do
            # the job without them. They were reachable on the Collections
            # screen and grantable nowhere, so the screen answered 403 to
            # everybody: the payments module is separate from finance, and no
            # amount of finance grants carries a payments key with it.
            #
            # School-wide rather than branch-scopable: a payment arrives
            # against an invoice, and the invoice carries the branch. The
            # gateway record has no branch of its own to narrow by.
            #
            # Every verb here is ``view``. Creating a collection, opening or
            # managing a virtual account, and the two ``view_sensitive`` keys
            # that expose the payer's own account details are all restricted,
            # so none of them is groupable - which is the intended shape.
            "payments.collection.view",
            "payments.virtual_account.view",
            "payments.report.view",
        ),
    ),
    # There is deliberately NO "Payroll" group, and there cannot be one.
    #
    # Every payroll key is SENSITIVE or CRITICAL - ``payrollrun`` view and
    # create are SENSITIVE, post and pay are CRITICAL, and all four ``salary``
    # verbs are SENSITIVE (vs_finance seed_finance_permissions.py:88-95). This
    # command refuses restricted keys, so a payroll group would seed empty and
    # read as an access bug rather than as the rule it is.
    #
    # The rule is the right one. Attaching a group takes effect immediately,
    # while a role change is reviewed, and what a payroll key exposes is every
    # teacher's pay. Mrs Adeyemi being handed "Payroll" from a dropdown at
    # 4pm on a Friday is exactly the grant that should require somebody to
    # stop and think. Payroll travels through a named role, or not at all.
    #
    # The same is true of the paying half of the group above: reading supplier
    # bills is a bundle, paying suppliers is a role.

    (
        "Academic Sessions",
        SCHOOL_WIDE,
        "Open, edit and close academic sessions and terms. A session normally "
        "covers the whole school, and can be narrowed to named branches when "
        "one of them runs its own year.",
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
        "Read the overview. Held for one branch it shows that branch.",
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
        "Student Bulk Data",
        # Deliberately its own bundle rather than two more keys on Student
        # Records. Loading a roll from a spreadsheet and taking one out of the
        # building are the two operations in this module that act on every
        # child at once, and a school that wants a registrar to enrol students
        # one at a time does not thereby want them able to replace the whole
        # roll or export it. SCHOOL_WIDE for the same reason: neither act is
        # meaningfully narrowed to a branch.
        SCHOOL_WIDE,
        "Load students in bulk from a spreadsheet, and export the roll.",
        (
            "school.students.import",
            "school.students.export",
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
        "Academic Structure",
        BRANCH_SCOPABLE,
        "Build the departments, programmes and levels a school teaches. Held "
        "for one branch it shows that branch's, plus everything the school "
        "shares.",
        (
            "academics.structure.view",
            "academics.structure.create",
            "academics.structure.update",
            "academics.structure.manage",
        ),
    ),
    (
        "Subjects",
        BRANCH_SCOPABLE,
        "Add subjects and record the levels they are taught at. A subject can "
        "belong to the whole school or to one branch.",
        (
            "academics.subject.view",
            "academics.subject.create",
            "academics.subject.update",
            "academics.subject.manage",
        ),
    ),
    (
        "Academic Calendar",
        BRANCH_SCOPABLE,
        "Add and edit calendar entries. A branch keeps its own dates inside the "
        "school's session.",
        (
            "academics.calendar.view",
            "academics.calendar.create",
            "academics.calendar.update",
            "academics.calendar.manage",
        ),
    ),
    (
        "Timetables & Rooms",
        BRANCH_SCOPABLE,
        "Build the bell schedule, class timetables and exam schedules, and keep "
        "the school's rooms. A branch keeps its own periods and rooms.",
        (
            "academics.timetable.view",
            "academics.timetable.create",
            "academics.timetable.update",
            "academics.timetable.manage",
            "academics.timetable.publish",
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


#: Every key the school catalogue calls school-wide, flattened. This is the
#: split that survived the removal of the ``SCHOOL_ADMIN`` persona and
#: ``BRANCH_ADMIN``: it says which keys are meaningless when pinned to one
#: branch, which is the only thing those two personas ever really recorded. It
#: is exported rather than left implicit in the table.
#: Keys that exist in the registry and belong in no bundle, on purpose.
#:
#: The exhaustiveness test treats an ungrouped key as an omission, which is
#: right: a key nobody can find is a key nobody can grant. But a deliberate
#: exclusion is a different thing from a forgotten one, and it has to be
#: written somewhere the test can read, or the test fails forever and is
#: eventually deleted rather than answered.
#:
#: ``school.branches.create`` and ``.update`` are here because every branch
#: write view demands ``platform.branches.*``, which no school role holds. A
#: school administrator posting to the branch endpoint is refused outright, so
#: offering the keys in a bundle would promise something the API declines. The
#: import engine was briefly a way around that - see vs_import_data/datasets.py.
DELIBERATELY_UNGROUPED: frozenset[str] = frozenset({
    "school.branches.create",
    "school.branches.update",
})


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
        "which of them are school-wide and which narrow to a branch. Grants "
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

            removed, _ = GroupPermission.objects.filter(
                group=group, permission__is_restricted=True,
            ).delete()
            if removed:
                self.stdout.write(self.style.WARNING(
                    f"  !  removed {removed} restricted membership(s) from {name!r}."
                ))

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
                perm = Permission.objects.filter(key=key, is_active=True).first()
                if perm is None:
                    # The key's own seeder has not run, or has been changed
                    # without this table being updated. Named rather than
                    # skipped in silence: a bundle that quietly loses a member
                    # is worse than one that fails to build.
                    missing_keys.append(key)
                    continue
                if perm.is_restricted:
                    continue
                _, link_created = GroupPermission.objects.get_or_create(
                    group=group,
                    permission=perm,
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
