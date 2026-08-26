"""The three ``platform.tasks.*`` keys must land, and land unevenly.

Whether the task monitor is safe depends entirely on these rows existing with
the right scope and going to the right roles. A seeder that silently skipped a
key would leave the endpoint refusing everyone; one that granted the two
CRITICAL keys to both platform roles would hand the raw tracebacks back to the
population this change took them away from.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.models import (
    Permission,
    PermissionScope,
    TenantRolePermission,
    TenantRoleTemplate,
)
from vs_tenants.models import Tenant

VIEW = "platform.tasks.view"
VIEW_ALL = "platform.tasks.view_all"
VIEW_SENSITIVE = "platform.tasks.view_sensitive"


def _call(command, **options):
    call_command(command, stdout=StringIO(), stderr=StringIO(), **options)


class SeedTaskPermissionsTests(TestCase):
    def setUp(self):
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        for key, name in (
            ("xvs_super_admin", "XVS Super Admin"),
            ("xvs_platform_admin", "XVS Platform Admin"),
        ):
            TenantRoleTemplate.objects.get_or_create(
                tenant=codex, key=key,
                defaults={"name": name, "is_system_role": True},
            )
        _call("seed_actions")
        _call("seed_platform_permissions")

    def _granted(self, role_key):
        return set(
            TenantRolePermission.objects.filter(
                role__key=role_key, granted=True,
                permission__key__startswith="platform.tasks.",
            ).values_list("permission__key", flat=True)
        )

    def test_all_three_keys_are_registered(self):
        keys = set(
            Permission.objects.filter(
                key__startswith="platform.tasks.",
            ).values_list("key", flat=True)
        )
        self.assertEqual(keys, {VIEW, VIEW_ALL, VIEW_SENSITIVE})

    def test_the_view_all_action_verb_exists(self):
        """Without it the seeder warns and skips the key rather than failing."""
        from vs_rbac.models import PermissionAction

        self.assertTrue(PermissionAction.objects.filter(name="view_all").exists())

    def test_every_key_is_platform_scoped(self):
        """A school role must not be offered these in its permission picker."""
        for key in (VIEW, VIEW_ALL, VIEW_SENSITIVE):
            self.assertEqual(
                Permission.objects.get(key=key).scope,
                PermissionScope.PLATFORM,
                msg=key,
            )

    def test_the_two_dangerous_keys_are_critical_and_restricted(self):
        for key in (VIEW_ALL, VIEW_SENSITIVE):
            perm = Permission.objects.get(key=key)
            self.assertEqual(perm.sensitivity_level, Permission.Sensitivity.CRITICAL, msg=key)
            self.assertTrue(perm.is_restricted, msg=key)

    def test_super_admin_holds_all_three(self):
        self.assertEqual(
            self._granted("xvs_super_admin"), {VIEW, VIEW_ALL, VIEW_SENSITIVE},
        )

    def test_platform_admin_holds_only_the_redacted_view(self):
        """The heart of the split: triage without the personal data.

        A platform admin can still answer "did Corona's import finish?". They
        cannot read the guardian's address out of the failure, and they cannot
        page through every customer at once.
        """
        self.assertEqual(self._granted("xvs_platform_admin"), {VIEW})

    def test_reseeding_changes_nothing(self):
        before = (self._granted("xvs_super_admin"), self._granted("xvs_platform_admin"))
        _call("seed_platform_permissions")
        after = (self._granted("xvs_super_admin"), self._granted("xvs_platform_admin"))
        self.assertEqual(before, after)
