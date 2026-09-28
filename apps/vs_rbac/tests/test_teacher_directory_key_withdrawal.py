"""``0030_a_teacher_reads_colleagues_by_relationship``: whose grant it takes, and whose it leaves.

Bright Star has the Teacher role a school is provisioned with, a per-branch
copy of it, a Teacher role an administrator explicitly denied the key, a
custom "Lead Teacher" role built on the key, and its School Admin. Green Field
is a second school with its own Teacher role, so the withdrawal is shown
reaching every school rather than the first one found.

The migration's two steps are plain functions over the models, so they are run
here against the live registry: the tables they touch have not changed shape
since.
"""
from importlib import import_module

from django.apps import apps
from django.test import TestCase

from vs_rbac.models import (
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
)

KEY = "school.teachers.view"
migration = import_module("vs_rbac.migrations.0030_a_teacher_reads_colleagues_by_relationship")


class TeacherDirectoryKeyWithdrawalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.key = make_permission(KEY)
        cls.prebuilt, _ = PrebuiltRoleTemplate.objects.get_or_create(
            key="teacher", defaults={"name": "Teacher", "scope": "branch"},
        )
        PrebuiltRolePermission.objects.get_or_create(
            prebuilt_role=cls.prebuilt, permission=cls.key,
        )

        bright = make_school(slug="bright-star", name="Bright Star")
        annex = make_branch(bright, name="Annex Branch", is_main=False)
        green = make_school(slug="green-field", name="Green Field")

        def role(school, key, *, system=True, granted=True):
            row = make_role(school, name=key.replace("-", " ").title(), key=key,
                            is_system_role=system)
            make_role_permission(row, cls.key, granted=granted)
            return row

        cls.teacher = role(bright, "teacher")
        cls.annex_teacher = role(bright, f"teacher-{annex.pk}")
        cls.lead = role(bright, "teacher-lead", system=False)
        cls.admin = role(bright, "school_admin")
        cls.green_teacher = role(green, "teacher")
        cls.denied = role(make_school(slug="denied", name="Denied"), "teacher", granted=False)

    def _held(self, role):
        return TenantRolePermission.objects.filter(
            role=role, permission_id=KEY, granted=True,
        ).exists()

    def test_every_schools_teacher_role_loses_the_key(self):
        migration.withdraw(apps, None)
        for role in (self.teacher, self.annex_teacher, self.green_teacher):
            with self.subTest(role=role.key):
                self.assertFalse(self._held(role))
        self.assertFalse(
            PrebuiltRolePermission.objects.filter(
                prebuilt_role=self.prebuilt, permission_id=KEY,
            ).exists(),
            "a school created from now on would copy the key back",
        )

    def test_custom_roles_other_roles_and_denies_are_left_alone(self):
        migration.withdraw(apps, None)
        self.assertTrue(self._held(self.lead))
        self.assertTrue(self._held(self.admin))
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=self.denied, permission_id=KEY, granted=False,
            ).exists(),
        )

    def test_the_reverse_grants_it_again(self):
        migration.withdraw(apps, None)
        migration.regrant(apps, None)
        for role in (self.teacher, self.annex_teacher, self.green_teacher):
            with self.subTest(role=role.key):
                self.assertTrue(self._held(role))
        self.assertFalse(self._held(self.denied), "a deny stays a deny")
        self.assertTrue(
            PrebuiltRolePermission.objects.filter(
                prebuilt_role=self.prebuilt, permission_id=KEY,
            ).exists(),
        )
