"""Tests for the ``seed_school_permission_groups`` management command.

The command organises the school-facing permission keys into named bundles and
records, per bundle, whether it is school-wide or narrows to a branch.

What is worth testing here is not that rows appear. It is:

  (a) the table names only keys that actually exist - a bundle silently missing
      a member is the failure mode a hand-maintained table has;
  (b) every school/academics key is classified exactly once, while restricted
      keys stay out of the immediately attachable group rows;
  (c) the command grants nobody anything, which is the promise that makes it
      safe to run against a live school.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.seed_school_permission_groups import (
    BRANCH_SCOPABLE,
    BRANCH_SCOPABLE_KEYS,
    SCHOOL_PERMISSION_GROUPS,
    SCHOOL_WIDE,
    DELIBERATELY_UNGROUPED,
    SCHOOL_WIDE_KEYS,
)
from vs_rbac.evaluator import get_effective_permissions
from vs_rbac.models import (
    GroupPermission,
    Permission,
    PermissionGroup,
    PermissionScope,
    TenantRoleGroup,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)


def _call(command, **options):
    out = StringIO()
    call_command(command, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def _seed_every_module():
    """Register every key the group table names, the way production does."""
    _call("seed_actions")
    _call("seed_prebuilt_role_templates")
    _call("seed_school_permissions")
    _call("seed_onboarding_permissions")
    _call("seed_notification_permissions")
    _call("seed_ticket_permissions")


class SchoolPermissionGroupTableTests(TestCase):
    """The declared table, checked against itself and against the registry."""

    def test_every_key_named_in_the_table_is_a_real_permission(self):
        _seed_every_module()
        _call("seed_school_permission_groups")

        declared = {
            key for _n, _r, _d, keys in SCHOOL_PERMISSION_GROUPS for key in keys
        }
        registered = set(
            Permission.objects.filter(key__in=declared).values_list("key", flat=True)
        )
        self.assertEqual(
            declared - registered,
            set(),
            "The group table names permission keys that no seeder registers.",
        )

    def test_no_key_is_placed_in_two_groups(self):
        placed: list[str] = [
            key for _n, _r, _d, keys in SCHOOL_PERMISSION_GROUPS for key in keys
        ]
        self.assertEqual(
            len(placed),
            len(set(placed)),
            "A permission key appears in more than one school group.",
        )

    def test_reach_is_a_partition_of_the_catalogue(self):
        self.assertEqual(SCHOOL_WIDE_KEYS & BRANCH_SCOPABLE_KEYS, frozenset())
        self.assertTrue(SCHOOL_WIDE_KEYS)
        self.assertTrue(BRANCH_SCOPABLE_KEYS)
        for _name, reach, _description, _keys in SCHOOL_PERMISSION_GROUPS:
            self.assertIn(reach, (SCHOOL_WIDE, BRANCH_SCOPABLE))

    def test_every_school_and_academics_key_is_grouped(self):
        """The two modules the school catalogue owns are covered exhaustively.

        ``tickets``, ``communication`` and ``onboarding`` are engine modules
        with keys the platform also holds, so only the school-held subset of
        those is in the table. ``school`` and ``academics`` exist for schools
        and nobody else, so a key in either that no bundle claims is an
        omission, not a decision.
        """
        _seed_every_module()

        catalogued = SCHOOL_WIDE_KEYS | BRANCH_SCOPABLE_KEYS | DELIBERATELY_UNGROUPED
        registered = set(
            Permission.objects.filter(
                module_id__in=["school", "academics"],
            ).values_list("key", flat=True)
        )
        self.assertEqual(
            registered - catalogued,
            set(),
            "school/academics keys exist that no permission group places. Add "
            "them to a bundle, or to DELIBERATELY_UNGROUPED with the reason.",
        )

    def test_a_deliberate_exclusion_is_a_real_key_and_is_in_no_bundle(self):
        """The escape hatch must not become a way to hide a mistake.

        Without this, DELIBERATELY_UNGROUPED could be padded with typos or
        with keys that are in fact grouped, and the exhaustiveness test above
        would keep passing while meaning less each time.
        """
        _seed_every_module()
        registered = set(
            Permission.objects.filter(
                module_id__in=["school", "academics"],
            ).values_list("key", flat=True)
        )
        self.assertTrue(
            DELIBERATELY_UNGROUPED <= registered,
            "DELIBERATELY_UNGROUPED names a key the seeder does not register.",
        )
        self.assertFalse(
            DELIBERATELY_UNGROUPED & (SCHOOL_WIDE_KEYS | BRANCH_SCOPABLE_KEYS),
            "A key cannot be both deliberately ungrouped and in a bundle.",
        )

    def test_the_table_calls_a_branch_a_branch(self):
        """Group names and descriptions are read on screen by a school's admin.

        The site primitive is ``vs_tenants.Branch`` and the word is *branch*.
        A synonym drifted in far enough that this bundle was called "Campus
        Administration" for a while, which taught the customer the wrong word
        for the thing they were administering. Nothing else pins these
        strings, so this does.
        """
        names = {name for name, _reach, _description, _keys in SCHOOL_PERMISSION_GROUPS}
        self.assertIn("Branch Administration", names)

        for name, _reach, description, _keys in SCHOOL_PERMISSION_GROUPS:
            self.assertNotIn("campus", name.lower(), f"group name: {name!r}")
            self.assertNotIn(
                "campus", description.lower(), f"description of {name!r}",
            )


class SchoolPermissionGroupSeedTests(TestCase):
    def setUp(self):
        _seed_every_module()

    def test_creates_every_group_tenant_scoped_and_system_owned(self):
        _call("seed_school_permission_groups")

        for name, reach, _description, keys in SCHOOL_PERMISSION_GROUPS:
            group = PermissionGroup.objects.filter(name=name).first()
            self.assertIsNotNone(group, f"Group {name!r} was not created.")
            # An unclassified bundle is one TenantRoleGroup refuses to attach.
            self.assertEqual(group.scope, PermissionScope.TENANT, name)
            self.assertTrue(group.is_system, name)
            self.assertTrue(group.is_active, name)
            self.assertTrue(
                group.description.startswith(f"{reach}."),
                f"Group {name!r} does not declare its reach first: "
                f"{group.description!r}",
            )
            self.assertEqual(
                set(group.permissions.values_list("key", flat=True)),
                set(
                    Permission.objects.filter(
                        key__in=keys, is_restricted=False,
                    ).values_list("key", flat=True)
                ),
                f"Group {name!r} has the wrong membership.",
            )
            self.assertFalse(
                group.permissions.filter(is_restricted=True).exists(),
                f"Group {name!r} contains an approval bypass.",
            )

    def test_is_idempotent(self):
        _call("seed_school_permission_groups")
        groups_before = PermissionGroup.objects.count()
        links_before = GroupPermission.objects.count()

        _call("seed_school_permission_groups")

        self.assertEqual(PermissionGroup.objects.count(), groups_before)
        self.assertEqual(GroupPermission.objects.count(), links_before)

    def test_dry_run_writes_nothing(self):
        before = PermissionGroup.objects.count()
        _call("seed_school_permission_groups", dry_run=True)
        self.assertEqual(PermissionGroup.objects.count(), before)

    def test_grants_nobody_anything(self):
        """The promise that makes this safe to run against a live school."""
        role_perms_before = TenantRolePermission.objects.count()
        role_groups_before = TenantRoleGroup.objects.count()
        assignments_before = TenantUserRoleAssignment.objects.count()

        _call("seed_school_permission_groups")

        self.assertEqual(TenantRolePermission.objects.count(), role_perms_before)
        self.assertEqual(TenantRoleGroup.objects.count(), role_groups_before)
        self.assertEqual(
            TenantUserRoleAssignment.objects.count(), assignments_before,
        )

    def test_a_school_admins_effective_permissions_are_unchanged(self):
        from vs_rbac.tests.helpers import make_branch, make_school, make_staff_user
        from vs_rbac.services import provision_role_from_prebuilt

        school = make_school(slug="riverbank", name="Riverbank School")
        branch = make_branch(school, name="Main Branch")
        # An ordinary STAFF account. The whole point of the catalogue is that
        # the role carries the authority, and there is no admin persona left to
        # lean on even if the fixture wanted one.
        admin = make_staff_user(branch, email="head@riverbank.test")
        role = provision_role_from_prebuilt(
            tenant=school.tenant, prebuilt_key="school_admin",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=school.tenant, user=admin, role=role,
        )

        before = set(get_effective_permissions(admin, tenant=school.tenant))
        _call("seed_school_permission_groups")
        admin.refresh_from_db()
        after = set(get_effective_permissions(admin, tenant=school.tenant))

        self.assertEqual(before, after)
        self.assertTrue(before, "Fixture is inert; the assertion proves nothing.")

    def test_a_seeded_group_can_actually_be_attached_to_a_school_role(self):
        """The point of declaring the scope, exercised end to end."""
        from vs_rbac.tests.helpers import make_school

        _call("seed_school_permission_groups")

        school = make_school(slug="greenfield", name="Greenfield School")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="calendar-officer", name="Calendar Officer",
        )
        group = PermissionGroup.objects.get(name="Academic Calendar")

        link = TenantRoleGroup(role=role, group=group)
        link.full_clean()
        link.save()

        self.assertTrue(
            TenantRoleGroup.objects.filter(role=role, group=group).exists(),
        )

    def test_a_custom_group_of_the_same_name_is_left_alone(self):
        """An administrator's own bundle is never quietly widened."""
        custom = PermissionGroup.objects.create(
            name="Academic Calendar",
            description="Ours, not the catalogue's.",
            scope=PermissionScope.TENANT,
            is_system=False,
        )
        GroupPermission.objects.create(
            group=custom, permission_id="academics.calendar.view",
        )

        _call("seed_school_permission_groups")

        custom.refresh_from_db()
        self.assertFalse(custom.is_system)
        self.assertEqual(
            set(custom.permissions.values_list("key", flat=True)),
            {"academics.calendar.view"},
        )
