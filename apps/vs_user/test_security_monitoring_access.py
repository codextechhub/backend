"""Who may open the live sessions and login attempts lists.

Both lists read with ``platform.security.view``. The key is seeded by
``seed_platform_permissions`` and granted to both platform roles, so a
platform administrator opens both screens without being the super
administrator, and a platform staff member holding no role is refused.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.models import Permission, PermissionScope, TenantRoleTemplate
from vs_rbac.tests.helpers import codex_tenant, make_platform_assignment, make_vision_user

SECURITY_VIEW = "platform.security.view"
LISTS = ("/v1/user/sessions/", "/v1/user/auth-attempts/")


class SecurityMonitoringAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for command in ("seed_actions", "seed_platform_permissions"):
            call_command(command, stdout=StringIO(), stderr=StringIO(), verbosity=0)
        cls.admin = make_vision_user(email="platform.admin@codex.test")
        make_platform_assignment(
            cls.admin,
            TenantRoleTemplate.objects.get(tenant=codex_tenant(), key="xvs_platform_admin"),
        )
        cls.roleless = make_vision_user(email="roleless@codex.test")

    def test_the_key_is_a_seeded_platform_permission(self):
        perm = Permission.objects.get(key=SECURITY_VIEW)
        self.assertEqual(perm.scope, PermissionScope.PLATFORM)
        self.assertTrue(perm.is_active)

    def test_both_platform_roles_hold_the_key(self):
        for role_key in ("xvs_super_admin", "xvs_platform_admin"):
            role = TenantRoleTemplate.objects.get(tenant=codex_tenant(), key=role_key)
            with self.subTest(role=role_key):
                self.assertTrue(role.role_permissions.filter(
                    permission__key=SECURITY_VIEW, granted=True,
                ).exists())

    def test_a_platform_admin_opens_both_lists(self):
        client = TenantAPIClient(self.admin)
        for url in LISTS:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_platform_staff_without_the_key_are_refused(self):
        client = TenantAPIClient(self.roleless)
        for url in LISTS:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)
