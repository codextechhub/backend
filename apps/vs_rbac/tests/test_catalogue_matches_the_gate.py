"""The role builder must not offer what the plan gate will refuse.

An administrator composing a role sees a list of permissions with an
``available`` flag; the drawer dims and disables the ones that are false. The
flag was computed from a module-level map that asked "does this school have
Finance?", and every school has Finance. The gate asks a different question -
which band of Finance - so 148 keys were offered to a Basic school and refused
the moment anybody used one.

That gap is not cosmetic. Bright Star's administrator ticks "Generate fees" for
her bursar and saves without complaint; the bursar clicks it and is told the
school is on Core. The role builder said yes and the product said no, and
nothing on either screen explains which was wrong.

So the two now read through one function. The invariant these tests pin is the
one that matters and is easy to lose again: **anything the catalogue marks
available, the gate must allow.** The reverse is deliberately not asserted -
the catalogue may hide more than the gate refuses, because a permission can be
withheld for reasons that have nothing to do with a plan.
"""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from schools.vs_schools.models import School
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from vs_user.tokens import CodeXRefreshToken

from ..models import Permission, PermissionScope
from ..plan_gate import plan_refusal
from .helpers import (
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    make_vision_user,
)


class CatalogueMatchesTheGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for command in (
            "seed_actions",
            "seed_config_catalogue",
            "seed_package",
            "seed_school_permissions",
            "seed_finance_permissions",
            "seed_procurement_permissions",
            "seed_payments_permissions",
            "seed_permission_bands",
        ):
            call_command(command, verbosity=0)
        cls.operator = make_vision_user(
            email="catalogue@codexng.test", super_admin=True,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.operator)
        set_value(
            definition=ConfigurationDefinition.objects.get(
                key="platform.entitlements.enforce"
            ),
            value=True, actor=self.operator, tenant=None, branch=None,
            reason="The catalogue must agree with the gate that is running.",
        )

    def _school(self, slug, plan_code):
        response = self.client.post(
            reverse("school-create"),
            {
                "name": "Bright Star Academy", "slug": slug,
                "package_setup_data": {
                    "package_plan": plan_code,
                    "subscription_expires_at": (
                        date.today() + timedelta(days=365)
                    ).isoformat(),
                },
                "branches": [{
                    "name": "Main", "state": "Lagos",
                    "is_main": True,
                    "primary_admin_data": {
                        "full_name": "Bright Star Head", "email": f"head@{slug}.test",
                    },
                }],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return School.objects.get(slug=slug)

    def _reader(self, school):
        """The school's own administrator, holding only the key this screen needs.

        The catalogue is a tenant-facing screen: a platform operator cannot
        assert somebody else's tenant on it, and asking it as CodeX would prove
        nothing about what a school is shown.
        """
        role = make_role(school, name="Role Reader", key="catalogue_reader")
        make_role_permission(
            role, Permission.objects.get(key="school.roles.view"),
        )
        admin = make_school_admin(
            None,
            email=f"reader@{school.slug}.test",
            tenant=school.tenant,
        )
        make_assignment(school, admin, role, branch=None)
        client = APIClient()
        token = str(CodeXRefreshToken.for_user(admin).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _catalogue(self, school):
        slug = school.tenant.slug
        response = self._reader(school).get(
            reverse(
                "rbac-tenant-permission-catalogue",
                kwargs={"tenant_slug": slug},
            ),
            {"tenant": slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        entries = {}
        for module in response.data["data"]:
            for row in module["permissions"]:
                entries[row["key"]] = row
        return entries

    def _offered_but_refused(self, school):
        entries = self._catalogue(school)
        refused = {
            permission.key
            for permission in Permission.objects.filter(
                is_active=True, scope=PermissionScope.TENANT,
            )
            if plan_refusal([permission.key], school.tenant)
        }
        return sorted(
            key for key in refused
            if entries.get(key, {}).get("available")
        )

    def test_a_basic_school_is_offered_nothing_its_plan_refuses(self):
        school = self._school("catalogue-basic", "basic")
        offered = self._offered_but_refused(school)
        self.assertEqual(
            offered, [],
            f"{len(offered)} keys are offered to a Basic school and refused "
            f"the moment anybody uses one, for example {offered[:3]}",
        )

    def test_a_standard_school_is_offered_nothing_its_plan_refuses(self):
        school = self._school("catalogue-standard", "standard")
        self.assertEqual(self._offered_but_refused(school), [])

    def test_a_premium_school_is_offered_nothing_its_plan_refuses(self):
        school = self._school("catalogue-premium", "premium")
        self.assertEqual(self._offered_but_refused(school), [])

    def test_a_deeper_plan_is_offered_strictly_more(self):
        basic = self._catalogue(self._school("catalogue-cmp-basic", "basic"))
        premium = self._catalogue(self._school("catalogue-cmp-premium", "premium"))
        basic_keys = {k for k, row in basic.items() if row["available"]}
        premium_keys = {k for k, row in premium.items() if row["available"]}
        self.assertTrue(
            basic_keys < premium_keys,
            "Premium should offer everything Basic does and more",
        )

    def test_core_permissions_stay_offered_to_the_shallowest_plan(self):
        # The failure that would matter more than the one being fixed: a
        # catalogue that hides what a school has paid for.
        entries = self._catalogue(self._school("catalogue-core", "basic"))
        for key in (
            "finance.invoice.create",
            "finance.payment.create",
            "school.students.create",
            "school.branches.view",
        ):
            with self.subTest(key=key):
                self.assertTrue(
                    entries[key]["available"], f"{key} is Core and was hidden",
                )

    def test_an_unavailable_entry_says_which_wall_it_is(self):
        """The depth travels as data, and never as prose.

        ``band`` and ``depth_label`` are what the drawer groups and sorts by,
        and the console reads to decide what to sell. The sentence beside the
        dimmed box says none of it: a school administrator composing a role is
        not shopping, and Core, Plus and Advanced are our words, not theirs.
        """
        entries = self._catalogue(self._school("catalogue-why", "basic"))
        row = entries["finance.feestructure.generate"]
        self.assertFalse(row["available"])
        self.assertEqual(row["band"], "finance_plus")
        self.assertEqual(row["depth_label"], "Plus")
        self.assertIn("Finance", row["unavailable_reason"])
        self.assertNotIn("Plus", row["unavailable_reason"])

    def test_an_available_entry_carries_no_reason(self):
        entries = self._catalogue(self._school("catalogue-noreason", "basic"))
        row = entries["finance.invoice.create"]
        self.assertTrue(row["available"])
        self.assertIsNone(row["unavailable_reason"])

    def test_a_core_key_still_names_its_band(self):
        # The drawer can group by depth whether or not a thing is reachable.
        entries = self._catalogue(self._school("catalogue-band", "basic"))
        self.assertEqual(entries["finance.invoice.create"]["band"], "finance_core")
        self.assertEqual(entries["finance.invoice.create"]["depth_label"], "Core")

    def test_a_key_that_belongs_to_no_plan_is_core_for_everybody(self):
        entries = self._catalogue(self._school("catalogue-free", "basic"))
        row = entries["school.branches.view"]
        self.assertTrue(row["available"])
        self.assertIsNone(row["band"])
        self.assertIsNone(row["depth_label"])
