"""
Tests for the `seed_school_permissions` management command (WP-B1 / A.2).

Covers:
  (a) running the command creates the school + academics permission keys;
  (b) prebuilt roles carry the expected default counts;
  (c) pre-existing tenant role templates (natively provisioned by prebuilt key)
      gain the granted rows on re-run (backfill);
  (d) an explicit deny is not overwritten by the backfill;
  (e) get_effective_permissions for a user with an active school_admin
      assignment returns `school.students.view`.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.evaluator import get_effective_permissions
from vs_rbac.models import (
    Permission,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_schools.models import School
from vs_user.models import User


def _seed_actions_and_roles():
    """Prerequisite seeds the school-permission command relies on."""
    call_command("seed_actions", stdout=StringIO(), stderr=StringIO())
    call_command("seed_prebuilt_role_templates", stdout=StringIO(), stderr=StringIO())


def _run_school_seed(**kwargs):
    out = StringIO()
    call_command("seed_school_permissions", stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


class SeedSchoolPermissionsKeyTests(TestCase):
    def setUp(self):
        _seed_actions_and_roles()

    def test_creates_school_and_academics_keys(self):
        _run_school_seed()
        # A representative spread across both modules and several verbs.
        for key in (
            "school.dashboard.view",
            "school.students.view",
            "school.students.view_sensitive",
            "school.administrators.suspend",
            "school.administrators.reactivate",
            "school.roles.assign",
            "school.roles.create",
            "school.roles.update",
            "school.roles.delete",
            "academics.session.view",
            "academics.calendar.manage",
            "academics.classes.assign",
        ):
            self.assertTrue(
                Permission.objects.filter(key=key).exists(),
                f"Expected permission key {key} to be created.",
            )

    def test_total_key_count(self):
        _run_school_seed()
        self.assertEqual(
            Permission.objects.filter(module_id__in=["school", "academics"]).count(),
            41,
        )

    def test_sensitivity_levels_applied(self):
        _run_school_seed()
        self.assertEqual(
            Permission.objects.get(key="school.students.view_sensitive").sensitivity_level,
            "SENSITIVE",
        )
        self.assertEqual(
            Permission.objects.get(key="school.dashboard.view").sensitivity_level,
            "NORMAL",
        )

    def test_idempotent(self):
        _run_school_seed()
        before = Permission.objects.filter(module_id__in=["school", "academics"]).count()
        out = _run_school_seed()
        after = Permission.objects.filter(module_id__in=["school", "academics"]).count()
        self.assertEqual(before, after)
        self.assertIn("0 new permission(s) created", out)


class SeedSchoolPrebuiltDefaultsTests(TestCase):
    def setUp(self):
        _seed_actions_and_roles()
        _run_school_seed()

    def _defaults(self, role_key):
        role = PrebuiltRoleTemplate.objects.get(key=role_key)
        return set(
            PrebuiltRolePermission.objects
            .filter(prebuilt_role=role)
            .values_list("permission_id", flat=True)
        )

    def test_school_admin_gets_all_41(self):
        self.assertEqual(len(self._defaults("school_admin")), 41)

    def test_branch_admin_default_count(self):
        self.assertEqual(len(self._defaults("branch_admin")), 22)

    def test_teacher_default_count(self):
        keys = self._defaults("teacher")
        self.assertEqual(len(keys), 7)
        self.assertIn("school.dashboard.view", keys)
        self.assertIn("school.students.view", keys)
        self.assertIn("academics.classes.update", keys)
        # Teacher must NOT get create/manage verbs.
        self.assertNotIn("school.students.create", keys)
        self.assertNotIn("school.branches.view", keys)


class SeedSchoolBackfillTests(TestCase):
    """The critical step — pre-existing tenant role templates get grants.

    Roles are found by the backfill through their native prebuilt key
    (key=<prebuilt.key> or key=<prebuilt.key>-<branch pk>).
    """

    def setUp(self):
        _seed_actions_and_roles()
        self.school = School.objects.create(
            name="Backfill Academy", slug="backfill", status="ACTIVE"
        )
        self.prebuilt = PrebuiltRoleTemplate.objects.get(key="school_admin")
        # Native lineage: a system role provisioned straight into the tenant
        # tables with the prebuilt key.
        self.role = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="school_admin",
            name="School Admin",
            is_system_role=True,
        )
        # No permissions attached to it yet.
        self.assertEqual(
            TenantRolePermission.objects.filter(role=self.role).count(), 0
        )

    def test_backfill_grants_rows_on_run(self):
        _run_school_seed()
        keys = set(
            TenantRolePermission.objects
            .filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        # school_admin defaults are all 41 keys.
        self.assertEqual(len(keys), 41)
        self.assertIn("school.students.view", keys)
        self.assertIn("school.roles.create", keys)
        self.assertIn("academics.classes.assign", keys)

    def test_backfill_is_idempotent(self):
        _run_school_seed()
        first = TenantRolePermission.objects.filter(role=self.role).count()
        _run_school_seed()
        second = TenantRolePermission.objects.filter(role=self.role).count()
        self.assertEqual(first, second)

    def test_explicit_deny_not_overwritten(self):
        # Admin explicitly denied a permission that is a school_admin default.
        _run_school_seed()  # first run registers the Permission rows
        TenantRolePermission.objects.filter(
            role=self.role, permission_id="school.students.view"
        ).update(granted=False)
        _run_school_seed()
        row = TenantRolePermission.objects.get(
            role=self.role, permission_id="school.students.view"
        )
        self.assertFalse(row.granted, "Backfill must never flip an explicit deny.")
        # Other defaults still granted.
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=self.role, permission_id="school.teachers.view", granted=True
            ).exists()
        )

    def test_native_teacher_role_backfilled_with_teacher_defaults_only(self):
        # Native lineage: provisioned straight into the tenant tables with the
        # prebuilt key (no legacy row at all).
        teacher_role = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="teacher",
            name="Teacher",
            is_system_role=True,
        )
        _run_school_seed()
        keys = set(
            TenantRolePermission.objects
            .filter(role=teacher_role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(len(keys), 7)
        self.assertIn("school.students.view", keys)
        self.assertNotIn("school.students.create", keys)

    def test_non_system_role_with_prebuilt_like_key_not_backfilled(self):
        # A custom (non-system) role must not silently inherit prebuilt
        # defaults just because its key resembles a prebuilt key.
        custom = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="teacher-lead",
            name="Lead Teacher (custom)",
            is_system_role=False,
        )
        _run_school_seed()
        self.assertEqual(
            TenantRolePermission.objects.filter(role=custom).count(), 0
        )


class SchoolAdminEffectivePermissionsTests(TestCase):
    """End-to-end: a user with an active school_admin assignment resolves grants."""

    def setUp(self):
        _seed_actions_and_roles()
        self.school = School.objects.create(
            name="Effective High", slug="effective", status="ACTIVE"
        )
        self.role = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="school_admin",
            name="School Admin",
            is_system_role=True,
        )
        _run_school_seed()

        self.user = User.objects.create_user(
            email="head@effective.test",
            password="Str0ng!pass123",
            user_type="SCHOOL_ADMIN",
            status="ACTIVE",
            first_name="Head",
            last_name="Teacher",
            tenant=self.school.tenant,
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.school.tenant,
            user=self.user,
            role=self.role,
            assignment_status="ACTIVE",
        )

    def test_effective_permissions_include_students_view(self):
        perms = get_effective_permissions(self.user, tenant=self.school.tenant)
        self.assertIn("school.students.view", perms)
        self.assertIn("academics.classes.assign", perms)
        self.assertIn("school.roles.update", perms)

    def test_effective_permissions_respect_explicit_deny(self):
        TenantRolePermission.objects.filter(
            role=self.role, permission_id="school.students.view"
        ).update(granted=False)
        # Clear any request-scoped cache on the user instance.
        if hasattr(self.user, "_rbac_effective_perms"):
            delattr(self.user, "_rbac_effective_perms")
        perms = get_effective_permissions(self.user, tenant=self.school.tenant)
        self.assertNotIn("school.students.view", perms)
