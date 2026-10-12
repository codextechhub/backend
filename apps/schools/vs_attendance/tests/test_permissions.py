"""The attendance permission keys, who holds them by default, and how they reach schools.

``mark`` and ``mark_any`` are two keys because RBAC has no "only my own class"
scope: a teacher takes the class they lead, a registrar takes any. A school
opened before the keys existed gains them on the next seed, once, and a key a
school took off a role stays off.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.capability_map import capability_for
from vs_rbac.models import (
    Permission,
    PermissionAction,
    PrebuiltRolePermission,
    TenantRolePermission,
)
from vs_rbac.services import provision_role_from_prebuilt
from vs_rbac.tests.helpers import make_branch, make_school

KEYS = {
    "attendance.register.view": "NORMAL",
    "attendance.register.mark": "NORMAL",
    "attendance.register.mark_any": "NORMAL",
    "attendance.register.remind": "NORMAL",
    "attendance.correction.create": "NORMAL",
    "attendance.correction.approve": "SENSITIVE",
    "attendance.staff.view": "NORMAL",
    "attendance.staff.mark": "NORMAL",
    "attendance.report.view": "NORMAL",
    "attendance.report.export": "SENSITIVE",
    "attendance.settings.update": "SENSITIVE",
}
TEACHER = {
    "attendance.register.view", "attendance.register.mark", "attendance.correction.create",
}
BRANCH_ADMIN = set(KEYS) - {"attendance.register.mark", "attendance.settings.update"}
SCHOOL_ADMIN = set(KEYS) - {"attendance.register.mark"}


def seed():
    call_command("seed_school_permissions", stdout=StringIO(), stderr=StringIO())


class AttendanceKeyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", stdout=StringIO(), stderr=StringIO())
        call_command("seed_prebuilt_role_templates", stdout=StringIO(), stderr=StringIO())
        seed()

    def defaults(self, role_key):
        return set(
            PrebuiltRolePermission.objects.filter(
                prebuilt_role__key=role_key, permission__module_id="attendance",
            ).values_list("permission_id", flat=True)
        )

    def test_exactly_these_eleven_keys_are_registered_at_tenant_scope(self):
        rows = Permission.objects.filter(module_id="attendance")
        self.assertEqual({row.key: row.sensitivity_level for row in rows}, KEYS)
        self.assertEqual({row.scope for row in rows}, {"TENANT"})

    def test_the_two_verbs_the_keys_needed_exist(self):
        for name in ("mark", "mark_any", "remind", "view", "create", "approve", "export", "update"):
            self.assertTrue(PermissionAction.objects.filter(name=name).exists(), name)

    def test_a_teacher_takes_their_own_class_and_an_administrator_takes_any(self):
        self.assertEqual(self.defaults("teacher"), TEACHER)
        self.assertEqual(self.defaults("branch_admin"), BRANCH_ADMIN)
        self.assertEqual(self.defaults("school_admin"), SCHOOL_ADMIN)

    def test_only_the_school_admin_changes_the_settings_and_only_administrators_decide(self):
        self.assertIn("attendance.settings.update", self.defaults("school_admin"))
        self.assertNotIn("attendance.settings.update", self.defaults("branch_admin"))
        self.assertNotIn("attendance.correction.approve", self.defaults("teacher"))
        self.assertNotIn("attendance.register.mark_any", self.defaults("teacher"))

    def test_the_sensitive_keys_are_restricted(self):
        for key, level in KEYS.items():
            self.assertEqual(Permission.objects.get(key=key).is_restricted, level != "NORMAL", key)

    def test_the_module_answers_to_the_attendance_capability(self):
        for resource in ("register", "correction", "staff", "report", "settings"):
            self.assertEqual(capability_for("attendance", resource), "attendance")

    def test_a_second_run_writes_nothing(self):
        before = Permission.objects.filter(module_id="attendance").count()
        seed()
        self.assertEqual(Permission.objects.filter(module_id="attendance").count(), before)


class AttendanceKeysReachExistingSchoolsTests(TestCase):
    """Brightfield and Sunrise were provisioned before attendance existed."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", stdout=StringIO(), stderr=StringIO())
        call_command("seed_prebuilt_role_templates", stdout=StringIO(), stderr=StringIO())
        seed()
        bright = make_school(slug="grow-bright", name="Bright Star").tenant
        lekki = make_branch(bright, name="Lekki", is_main=True)
        sunrise = make_school(slug="grow-sunrise", name="Sunrise").tenant
        main = make_branch(sunrise, name="Main", is_main=True)

        def provision(tenant, key, branch=None):
            return provision_role_from_prebuilt(tenant=tenant, branch=branch, prebuilt_key=key)

        cls.admin = provision(bright, "school_admin")
        cls.branch_admin = provision(bright, "branch_admin", lekki)
        cls.teacher = provision(bright, "teacher", lekki)
        cls.sunrise_teacher = provision(sunrise, "teacher", main)

        # Rewind to a database that predates the keys.
        TenantRolePermission.objects.filter(permission__module_id="attendance").delete()
        PrebuiltRolePermission.objects.filter(permission__module_id="attendance").delete()

    def held(self, role):
        return set(
            TenantRolePermission.objects.filter(
                role=role, granted=True, permission__module_id="attendance",
            ).values_list("permission_id", flat=True)
        )

    def test_every_copy_gains_exactly_its_roles_defaults(self):
        seed()
        self.assertEqual(self.held(self.admin), SCHOOL_ADMIN)
        self.assertEqual(self.held(self.branch_admin), BRANCH_ADMIN)
        self.assertEqual(self.held(self.teacher), TEACHER)
        self.assertEqual(self.held(self.sunrise_teacher), TEACHER)

    def test_a_key_a_school_took_off_stays_off(self):
        seed()
        TenantRolePermission.objects.filter(
            role=self.sunrise_teacher, permission_id="attendance.correction.create",
        ).delete()
        seed()
        self.assertEqual(
            self.held(self.sunrise_teacher), TEACHER - {"attendance.correction.create"},
        )
        self.assertEqual(self.held(self.teacher), TEACHER)
