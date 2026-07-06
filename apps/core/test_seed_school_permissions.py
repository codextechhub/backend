"""
Tests for the `seed_school_permissions` management command (WP-B1 / A.2).

Covers:
  (a) running the command creates the school + academics permission keys;
  (b) prebuilt roles carry the expected default counts;
  (c) a pre-existing SchoolRoleTemplate (created BEFORE the grants exist) gains
      the granted rows on re-run (backfill);
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
    SchoolRolePermission,
    SchoolRoleTemplate,
    SchoolUserRoleAssignment,
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
            38,
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

    def test_school_admin_gets_all_38(self):
        self.assertEqual(len(self._defaults("school_admin")), 38)

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
    """The critical new step — pre-existing school role templates get grants."""

    def setUp(self):
        _seed_actions_and_roles()
        self.school = School.objects.create(
            name="Backfill Academy", slug="backfill", status="ACTIVE"
        )
        # A SchoolRoleTemplate that was provisioned from school_admin BEFORE the
        # school permissions were ever seeded (the already-onboarded-school case).
        self.prebuilt = PrebuiltRoleTemplate.objects.get(key="school_admin")
        self.template = SchoolRoleTemplate.all_objects.create(
            school=self.school,
            name="School Admin",
            prebuilt_from=self.prebuilt,
            is_system_role=True,
        )
        # No permissions attached to it yet.
        self.assertEqual(
            SchoolRolePermission.objects.filter(role=self.template).count(), 0
        )

    def test_backfill_grants_rows_on_run(self):
        _run_school_seed()
        keys = set(
            SchoolRolePermission.objects
            .filter(role=self.template, granted=True)
            .values_list("permission_id", flat=True)
        )
        # school_admin defaults are all 38 keys.
        self.assertEqual(len(keys), 38)
        self.assertIn("school.students.view", keys)
        self.assertIn("academics.classes.assign", keys)

    def test_backfill_is_idempotent(self):
        _run_school_seed()
        first = SchoolRolePermission.objects.filter(role=self.template).count()
        _run_school_seed()
        second = SchoolRolePermission.objects.filter(role=self.template).count()
        self.assertEqual(first, second)

    def test_explicit_deny_not_overwritten(self):
        # Admin explicitly denied a permission that is a school_admin default.
        SchoolRolePermission.objects.create(
            role=self.template,
            permission_id="school.students.view",
            granted=False,
        )
        _run_school_seed()
        row = SchoolRolePermission.objects.get(
            role=self.template, permission_id="school.students.view"
        )
        self.assertFalse(row.granted, "Backfill must never flip an explicit deny.")
        # Other defaults still granted.
        self.assertTrue(
            SchoolRolePermission.objects.filter(
                role=self.template, permission_id="school.teachers.view", granted=True
            ).exists()
        )

    def test_teacher_template_backfilled_with_teacher_defaults_only(self):
        teacher_prebuilt = PrebuiltRoleTemplate.objects.get(key="teacher")
        teacher_tmpl = SchoolRoleTemplate.all_objects.create(
            school=self.school,
            name="Teacher",
            prebuilt_from=teacher_prebuilt,
            is_system_role=True,
        )
        _run_school_seed()
        keys = set(
            SchoolRolePermission.objects
            .filter(role=teacher_tmpl, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(len(keys), 7)
        self.assertIn("school.students.view", keys)
        self.assertNotIn("school.students.create", keys)


class SchoolAdminEffectivePermissionsTests(TestCase):
    """End-to-end: a user with an active school_admin assignment resolves grants."""

    def setUp(self):
        _seed_actions_and_roles()
        self.school = School.objects.create(
            name="Effective High", slug="effective", status="ACTIVE"
        )
        self.prebuilt = PrebuiltRoleTemplate.objects.get(key="school_admin")
        self.template = SchoolRoleTemplate.all_objects.create(
            school=self.school,
            name="School Admin",
            prebuilt_from=self.prebuilt,
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
            school=self.school,
        )
        SchoolUserRoleAssignment.all_objects.create(
            school=self.school,
            user=self.user,
            role=self.template,
            assignment_status="ACTIVE",
        )

    def test_effective_permissions_include_students_view(self):
        perms = get_effective_permissions(self.user, school=self.school)
        self.assertIn("school.students.view", perms)
        self.assertIn("academics.classes.assign", perms)

    def test_effective_permissions_respect_explicit_deny(self):
        SchoolRolePermission.objects.filter(
            role=self.template, permission_id="school.students.view"
        ).update(granted=False)
        # Clear any request-scoped cache on the user instance.
        if hasattr(self.user, "_rbac_effective_perms"):
            delattr(self.user, "_rbac_effective_perms")
        perms = get_effective_permissions(self.user, school=self.school)
        self.assertNotIn("school.students.view", perms)
