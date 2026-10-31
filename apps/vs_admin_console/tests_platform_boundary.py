"""Console surfaces that show every school are CX staff's alone, by two checks.

Health, the admin dashboard and the finance inventory serve rows about every
tenant on the platform. Their keys are platform-scoped, so a school role cannot
carry them (``vs_rbac.tests.test_platform_scope_escalation``). That makes the
key's label the whole boundary, and a label is one seeder edit away from
``TENANT``. So each surface also asks ``IsPlatformActor``, and these tests
mislabel the key on purpose, hand it to a school admin and knock.

The last test pins the list of ``platform.*`` keys a school may hold, because
moving a key onto it opens every route keyed by that key alone to schools.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from core.management.commands.seed_platform_permissions import TENANT_HOLDABLE_KEYS
from vs_rbac.evaluator import has_permission
from vs_rbac.models import Permission, PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_user.tokens import CodeXRefreshToken

# (url, key) for each surface whose rows describe every school.
SURFACES = (
    ("/v1/health/overview/", "platform.health.view"),
    ("/v1/health/incidents/", "platform.health.view"),
    ("/v1/admin/dashboard/", "platform.dashboard.view"),
    ("/v1/admin/finance/entities/", "platform.schools.view"),
)


def _grant(user, key):
    role = make_role(user.tenant, name=f"boundary-{user.pk}-{key}")
    make_role_permission(role, make_permission(key))
    make_assignment(user.tenant, user, role)


def _get(user, url):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client.get(f"{url}?tenant={user.tenant.slug}")


class PlatformSurfaceBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="boundary-school", name="Boundary School")
        cls.branch = make_branch(cls.school)

    def test_cx_staff_with_the_key_get_in(self):
        for index, (url, key) in enumerate(SURFACES):
            with self.subTest(url=url):
                cx = make_vision_user(email=f"boundary-cx-{index}@codex.test")
                _grant(cx, key)
                self.assertEqual(_get(cx, url).status_code, 200)

    def test_a_school_admin_holding_a_mislabelled_key_is_still_refused(self):
        for index, (url, key) in enumerate(SURFACES):
            with self.subTest(url=url):
                make_permission(key)
                Permission.objects.filter(key=key).update(scope=PermissionScope.TENANT)
                admin = make_school_admin(self.branch, email=f"boundary-admin-{index}@test.com")
                _grant(admin, key)
                # The key check alone would let them in: they hold it.
                self.assertTrue(has_permission(admin, key))

                self.assertEqual(_get(admin, url).status_code, 403)


class TenantHoldablePlatformKeysTests(TestCase):
    def test_the_platform_keys_a_school_may_hold_are_exactly_these(self):
        """Adding a key here lets schools reach every route keyed by it alone.

        Before you add one, find those routes and check each fences its rows to
        the caller's tenant, or composes ``IsPlatformActor``.
        """
        self.assertEqual(
            TENANT_HOLDABLE_KEYS,
            {
                "platform.team.view",
                "platform.team.create",
                "platform.team.update",
                "platform.team.delete",
                "platform.team.suspend",
                "platform.team.reactivate",
                "platform.audit.view",
                "platform.audit.export",
            },
        )
