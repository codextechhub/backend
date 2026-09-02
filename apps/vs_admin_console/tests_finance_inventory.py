"""The finance inventory is platform-only, and lists absence as well as presence.

The security case leads, because this endpoint is a roll-call of every tenant on
the platform. A school reaching it would learn which other schools exist and how
far each one is set up, which is a disclosure bug rather than a cosmetic one.

The second group pins the thing the endpoint exists for: a school with no books
gets a row saying so. That absent row is the answer to "were they provisioned at
all", which is indistinguishable from "their books are broken" from the school's
own side.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from vs_finance.models import LedgerEntity
from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_user.tokens import CodeXRefreshToken

URL = "/v1/admin/finance/entities/"
PERM_VIEW = "platform.schools.view"


def grant(user, *keys):
    """Give *user* an active role on their own tenant carrying *keys*."""
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"fin-inv-test-{user.pk}",
        defaults={"name": f"Finance Inventory Test Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


class FinanceInventoryTestBase(TestCase):
    def setUp(self):
        self.school = make_school(slug="inv-school", name="Inventory School")
        self.branch = make_branch(self.school)
        self.bookless = make_school(slug="inv-bookless", name="Bookless School")
        self.entity = LedgerEntity.objects.create(
            name="Inventory School Books", code="INVSCHOOL", tenant=self.school.tenant,
        )
        self.cx = make_vision_user(email="inv-cx@codex.test")

    def client_for(self, user):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def fetch(self, user=None):
        user = user or self.cx
        return self.client_for(user).get(f"{URL}?tenant={user.tenant.slug}")


class FinanceInventoryPermissionTests(FinanceInventoryTestBase):
    """Only the platform sees the roll-call."""

    def test_anonymous_is_rejected(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))

    def test_a_platform_user_without_the_key_is_denied(self):
        self.assertEqual(self.fetch().status_code, 403)

    def test_a_platform_user_with_the_key_is_allowed(self):
        grant(self.cx, PERM_VIEW)
        self.assertEqual(self.fetch().status_code, 200, self.fetch().content[:200])

    def test_a_school_cannot_even_be_granted_the_key(self):
        """The disclosure case, closed at the model rather than at the view.

        ``platform.schools.view`` is ``PermissionScope.PLATFORM``, so a school
        tenant cannot manufacture the authority for itself - which matters,
        because a school admin holding the role-create key writes its own rows.
        """
        admin = make_school_admin(self.branch, email="inv-school-admin@test.com")
        with self.assertRaises(ValidationError):
            grant(admin, PERM_VIEW)

    def test_a_school_admin_cannot_read_the_inventory(self):
        admin = make_school_admin(self.branch, email="inv-school-read@test.com")
        self.assertEqual(
            self.fetch(user=admin).status_code, 403,
            "a school-tenant user read the platform-wide finance inventory",
        )


class FinanceInventoryContentTests(FinanceInventoryTestBase):
    def setUp(self):
        super().setUp()
        grant(self.cx, PERM_VIEW)

    def rows(self):
        response = self.fetch()
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()["data"]

    def test_it_reports_the_books_a_tenant_actually_has(self):
        row = next(r for r in self.rows() if r["tenant"]["slug"] == self.school.slug)
        self.assertTrue(row["has_books"])
        self.assertEqual([e["code"] for e in row["entities"]], [self.entity.code])
        self.assertEqual(row["entities"][0]["base_currency"], "NGN")

    def test_a_tenant_with_no_books_is_listed_rather_than_omitted(self):
        """The most useful row on the screen is the empty one."""
        row = next(r for r in self.rows() if r["tenant"]["slug"] == self.bookless.slug)
        self.assertFalse(row["has_books"])
        self.assertEqual(row["entities"], [])

    def test_every_tenant_appears_exactly_once(self):
        from vs_tenants.models import Tenant

        slugs = [r["tenant"]["slug"] for r in self.rows()]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(
            sorted(slugs), sorted(Tenant.objects.values_list("slug", flat=True)),
        )

    def test_it_carries_no_money(self):
        """An inventory answers 'do they have books', never 'what is in them'.

        Reading a school's figures stays on the proxying route, where it is
        attributable to somebody entitled to them. If a balance ever appears
        here, that control has been bypassed platform-wide in one commit.
        """
        forbidden = {"balance", "total", "amount", "revenue", "outstanding"}
        for row in self.rows():
            for book in row["entities"]:
                self.assertFalse(
                    forbidden & {k.lower() for k in book},
                    f"the inventory leaked a figure: {sorted(book)}",
                )
