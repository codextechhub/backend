"""The search bar's endpoint, its matcher and its registry.

The fixture is two tenants and the platform. Lagoon has two branches and was
never given a plan, so every module is on; Harbour has one branch and a plan
that includes Students and not Finance, so the capability filter is live. The
platform operator is the only caller who can hold ``config.value.view``, which
the generic configuration settings need.

People are found by the modules that own them, and are tested beside those
modules (``schools/vs_students/tests/test_search_providers.py`` and
``schools/vs_staff/tests/test_search_provider.py``).
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from vs_config import search_places as config_places
from vs_config.models import Capability, CapabilityEntitlement, ConfigurationDefinition
from vs_rbac.capability_map import capability_for
from vs_rbac.models import Permission, PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    make_vision_user,
)
from vs_tenants.models import Tenant

from . import registry
from .matching import rank_places
from .registry import (
    ACTION,
    SETTING,
    DuplicateCondition,
    DuplicatePlaceKey,
    Place,
    PlaceRegistry,
    UnknownCondition,
)
from .views import GROUP_ORDER

URL = "/v1/search/"
DUE_DAYS_KEY = "finance.settings.documents.default_invoice_due_days"


def _place(key, label, *, description="", keywords=()):
    return Place(kind=ACTION, key=key, label=label, screen=key,
                 description=description, keywords=keywords)


class MatcherTests(SimpleTestCase):
    """Matching and ranking, with no database."""

    def test_every_query_word_must_start_a_word_of_the_place(self):
        place = _place("a", "Days until an invoice is due")
        self.assertEqual(rank_places([place], "INV due"), [place])
        self.assertEqual(rank_places([place], "invoice xyz"), [])
        self.assertEqual(rank_places([place], "voice"), [])

    def test_a_label_match_ranks_above_a_keyword_above_a_description(self):
        by_description = _place("c", "Alpha", description="Banking defaults")
        by_keyword = _place("b", "Beta", keywords=("banking",))
        by_label = _place("a", "Zulu banking")
        self.assertEqual(
            rank_places([by_description, by_keyword, by_label], "bank"),
            [by_label, by_keyword, by_description],
        )

    def test_equal_matches_read_alphabetically_by_label(self):
        runs = _place("x", "Payroll runs")
        settings = _place("y", "Payroll settings")
        self.assertEqual(rank_places([settings, runs], "payroll"), [runs, settings])

    def test_the_invoice_due_setting_answers_to_due_date(self):
        from vs_finance.search_places import places as finance_places

        hit = rank_places(list(finance_places()), "due date")[0]
        self.assertEqual(
            (hit.key, hit.kind, hit.screen, hit.anchor),
            (DUE_DAYS_KEY, SETTING, "finance.settings.documents", "default_invoice_due_days"),
        )


class RegistryTests(SimpleTestCase):
    def test_a_key_registered_twice_raises(self):
        registry = PlaceRegistry()
        registry.register(_place("finance.invoices", "Customer invoices"))
        with self.assertRaises(DuplicatePlaceKey):
            registry.register(_place("finance.invoices", "Invoices again"))

    def test_a_contributed_key_that_collides_raises(self):
        registry = PlaceRegistry()
        registry.register(_place("finance.invoices", "Customer invoices"))
        registry.register_contributor(lambda: [_place("finance.invoices", "Again")])
        with self.assertRaises(DuplicatePlaceKey):
            registry.places()

    def test_a_condition_registered_twice_under_one_name_raises(self):
        registry = PlaceRegistry()
        registry.register_condition("several_branches", lambda **_: True)
        with self.assertRaises(DuplicateCondition):
            registry.register_condition("several_branches", lambda **_: False)

    def test_an_unregistered_condition_raises_when_asked(self):
        with self.assertRaises(UnknownCondition):
            PlaceRegistry().condition("nobody_registered_this")


class SearchEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for key in ("finance", "students", "procurement"):
            Capability.objects.create(key=key, label=key.title())
        perms = [
            make_permission(key, scope=PermissionScope.TENANT)
            for key in ("finance.settings.view", "finance.invoice.view")
        ]

        cls.lagoon = Tenant.objects.create(name="Lagoon", slug="lagoon")
        make_branch(cls.lagoon, name="Lekki", is_main=True)
        make_branch(cls.lagoon, name="Ikeja", is_main=False)
        bursar_role = make_role(cls.lagoon, name="Bursar", key="bursar")
        for perm in perms:
            make_role_permission(bursar_role, perm)
        cls.bursar = make_school_admin(None, email="bursar@lagoon.test", tenant=cls.lagoon)
        make_assignment(cls.lagoon, cls.bursar, bursar_role, branch=None)
        # Holds nothing at all.
        cls.clerk = make_school_admin(None, email="clerk@lagoon.test", tenant=cls.lagoon)

        cls.harbour = Tenant.objects.create(name="Harbour", slug="harbour")
        make_branch(cls.harbour, name="Main", is_main=True)
        CapabilityEntitlement.objects.create(
            tenant=cls.harbour, capability=Capability.objects.get(key="students"),
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE, depth=Capability.Depth.CORE,
        )
        harbour_role = make_role(cls.harbour, name="Bursar", key="bursar")
        for perm in perms:
            make_role_permission(harbour_role, perm)
        cls.harbour_bursar = make_school_admin(
            None, email="bursar@harbour.test", tenant=cls.harbour,
        )
        make_assignment(cls.harbour, cls.harbour_bursar, harbour_role, branch=None)

        cls.operator = make_vision_user(email="operator@codex.test")
        operator_role = make_platform_role(name="Configuration reader")
        make_platform_role_permission(
            operator_role,
            make_permission("config.value.view", scope=PermissionScope.PLATFORM),
        )
        make_platform_assignment(cls.operator, operator_role)
        ConfigurationDefinition.objects.create(
            key="students.probe_tenant", label="Probe tenant setting",
            description="A tenant may set this.", value_type="BOOLEAN",
            default_value=False, allowed_scopes=["platform", "school"],
        )
        ConfigurationDefinition.objects.create(
            key="security.probe_platform", label="Probe platform setting",
            description="Only the platform may set this.", value_type="BOOLEAN",
            default_value=False, allowed_scopes=["platform"],
        )

    def setUp(self):
        config_places.clear_cache()

    def search(self, user, q):
        response = TenantAPIClient(user).get(URL, {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    @staticmethod
    def keys(hits):
        return [hit["key"] for hit in hits]

    # -- security -------------------------------------------------------

    def test_an_anonymous_caller_is_refused(self):
        response = APIClient().get(URL, {"q": "invoice", "tenant": self.lagoon.slug})
        self.assertEqual(response.status_code, 401)

    def test_finance_places_need_the_finance_read_key(self):
        clerk = self.search(self.clerk, "invoice")
        found = self.keys(clerk["actions"]) + self.keys(clerk["settings"])
        self.assertFalse([k for k in found if k.startswith("finance.")], found)

        bursar = self.search(self.bursar, "invoice")
        self.assertIn("finance.invoices", self.keys(bursar["actions"]))
        self.assertIn(DUE_DAYS_KEY, self.keys(bursar["settings"]))

    def test_a_tenant_without_finance_sees_no_finance_places(self):
        data = self.search(self.harbour_bursar, "invoice")
        found = self.keys(data["actions"]) + self.keys(data["settings"])
        self.assertFalse([k for k in found if k.startswith("finance.")], found)

    def test_a_hit_never_names_its_permission_or_capability(self):
        data = self.search(self.bursar, "due date")
        self.assertTrue(data["actions"] or data["settings"])
        for hit in data["actions"] + data["settings"]:
            self.assertFalse({"permission", "capability"} & set(hit), hit)

    # -- what is found --------------------------------------------------

    def test_due_date_finds_the_invoice_due_setting(self):
        hit = self.search(self.bursar, "due date")["settings"][0]
        self.assertEqual(hit, {
            "key": DUE_DAYS_KEY,
            "label": "Days until an invoice is due",
            "description": hit["description"],
            "group": "Document settings",
            "screen": "finance.settings.documents",
            "anchor": "default_invoice_due_days",
        })
        self.assertTrue(hit["description"])

    def test_every_definition_is_a_platform_setting_and_no_tenant_member_sees_one(self):
        keys = self.keys(self.search(self.operator, "probe")["settings"])
        self.assertIn("config.settings.students.probe_tenant", keys)
        self.assertIn("config.settings.security.probe_platform", keys)
        self.assertEqual(self.search(self.bursar, "probe")["settings"], [])

    def test_groups_are_people_actions_settings_guides(self):
        data = self.search(self.bursar, "invoice")
        self.assertEqual(data["order"], ["people", "actions", "settings", "guides"])
        self.assertEqual(list(GROUP_ORDER), data["order"])
        self.assertEqual(data["guides"], [])

    def test_a_one_character_query_answers_every_group_empty(self):
        self.assertEqual(self.search(self.bursar, "a"), {
            "order": ["people", "actions", "settings", "guides"],
            "people": [], "actions": [], "settings": [], "guides": [],
        })


class PlaceCatalogueTests(TestCase):
    """Every registered place names keys and capabilities the seeds really produce.

    A permission key with a typo hides its screen from everybody, silently, and
    a missing capability offers a screen to a school whose plan leaves the
    module out. Both are checked against the catalogue the seeds build, with
    every configuration definition present so the contributed settings are
    checked too.
    """

    @classmethod
    def setUpTestData(cls):
        for command in ("seed_all_permissions", "seed_config_catalogue"):
            call_command(command, stdout=StringIO(), stderr=StringIO(), verbosity=0)
        config_places.clear_cache()
        cls.places = registry.places()
        cls.permissions = dict(Permission.objects.values_list("key", "scope"))

    def test_the_seeds_and_the_contributors_ran(self):
        keys = {place.key for place in self.places}
        self.assertIn("school.students.view", self.permissions)
        self.assertIn("attendance.settings.edit_window_days", keys)
        self.assertIn("config.settings.platform.entitlements.enforce", keys)

    def test_every_permission_key_is_a_seeded_permission(self):
        missing = sorted(
            f"{place.key}: {key}"
            for place in self.places for key in place.permission_keys
            if key not in self.permissions
        )
        self.assertEqual(missing, [])

    def test_every_condition_is_a_registered_condition(self):
        known = registry.condition_names()
        unknown = sorted(
            f"{place.key}: {place.condition}"
            for place in self.places
            if place.condition is not None and place.condition not in known
        )
        self.assertEqual(unknown, [])

    def test_every_capability_is_a_seeded_capability(self):
        known = set(Capability.objects.filter(is_active=True).values_list("key", flat=True))
        unknown = sorted(
            f"{place.key}: {place.capability}"
            for place in self.places
            if place.capability is not None and place.capability not in known
        )
        self.assertEqual(unknown, [])

    def test_a_place_naming_no_capability_opens_to_no_gated_tenant_key(self):
        """A school key in a module its plan may leave out needs that module's capability."""
        ungated = []
        for place in self.places:
            if place.capability is not None:
                continue
            for key in place.permission_keys:
                if self.permissions.get(key) != PermissionScope.TENANT:
                    continue
                module, resource, _action = key.split(".", 2)
                if capability_for(module, resource) is not None:
                    ungated.append(f"{place.key}: {key}")
        self.assertEqual(sorted(ungated), [])


class ConditionalPlaceTests(TestCase):
    """Screens that exist only sometimes, in more than one shape of tenant.

    Lagoon has two branches and has never chosen a custody mode, so its online
    payments are held and paid out. Creek has one branch and is paid directly
    into its bank. Each has a bursar holding the same keys across the whole
    tenant, and Lagoon also has one pinned to Lekki.
    """

    KEYS = ("finance.interbranch.view", "finance.payment.view", "finance.report.view",
            "finance.invoice.view", "payments.payout.view")

    @classmethod
    def setUpTestData(cls):
        from vs_payments.constants import CustodyMode
        from vs_payments.models import PaymentCustodySettings

        for key in ("finance", "procurement"):
            Capability.objects.create(key=key, label=key.title())
        perms = [make_permission(key, scope=PermissionScope.TENANT) for key in cls.KEYS]

        cls.lagoon = Tenant.objects.create(name="Lagoon", slug="lagoon")
        cls.lekki = make_branch(cls.lagoon, name="Lekki", is_main=True)
        make_branch(cls.lagoon, name="Ikeja", is_main=False)
        cls.creek = Tenant.objects.create(name="Creek", slug="creek")
        make_branch(cls.creek, name="Main", is_main=True)
        PaymentCustodySettings.objects.create(tenant=cls.creek, mode=CustodyMode.DIRECT)

        cls.bursars = {}
        for tenant in (cls.lagoon, cls.creek):
            role = make_role(tenant, name="Bursar", key="bursar")
            for perm in perms:
                make_role_permission(role, perm)
            user = make_school_admin(None, email=f"bursar@{tenant.slug}.test", tenant=tenant)
            make_assignment(tenant, user, role, branch=None)
            cls.bursars[tenant.slug] = user
            if tenant is cls.lagoon:
                cls.lekki_bursar = make_school_admin(
                    None, email="lekki@lagoon.test", tenant=tenant,
                )
                make_assignment(tenant, cls.lekki_bursar, role, branch=cls.lekki)
        cls.clerk = make_school_admin(None, email="clerk@lagoon.test", tenant=cls.lagoon)
        cls.operator = make_vision_user(email="operator@codex.test")

    def actions(self, user, q):
        response = TenantAPIClient(user).get(URL, {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return [hit["key"] for hit in response.data["data"]["actions"]]

    def test_inter_branch_screens_exist_only_at_a_tenant_with_several_branches(self):
        inter_branch = {"finance.inter_branch_transfers", "finance.inter_branch_balances",
                        "finance.recharges", "finance.shared_cost_rules",
                        "finance.held_receipts"}
        found = set()
        for q in ("inter branch", "recharge", "held receipts", "shared cost"):
            found |= set(self.actions(self.bursars["lagoon"], q))
            self.assertFalse(inter_branch & set(self.actions(self.bursars["creek"], q)), q)
        self.assertEqual(inter_branch & found, inter_branch)

    def test_payouts_exist_only_while_payments_are_held(self):
        held = set(self.actions(self.bursars["lagoon"], "payout"))
        self.assertTrue({"payments.payouts", "payments.payout_batches"} <= held, held)
        direct = set(self.actions(self.bursars["creek"], "payout"))
        self.assertFalse({"payments.payouts", "payments.payout_batches"} & direct, direct)

    def test_the_statutory_pack_is_offered_only_to_a_whole_tenant_reader(self):
        self.assertIn("finance.statutory_pack", self.actions(self.bursars["lagoon"], "statutory"))
        self.assertNotIn("finance.statutory_pack", self.actions(self.lekki_bursar, "statutory"))

    def test_the_finance_overview_needs_any_finance_key(self):
        self.assertIn("finance.dashboard", self.actions(self.bursars["lagoon"], "finance overview"))
        self.assertNotIn("finance.dashboard", self.actions(self.clerk, "finance overview"))

    def test_keyless_platform_screens_are_offered_to_platform_staff_only(self):
        self.assertIn("user.lockouts", self.actions(self.operator, "lockouts"))
        self.assertIn("todo.board", self.actions(self.operator, "to-do"))
        self.assertNotIn("user.lockouts", self.actions(self.bursars["lagoon"], "lockouts"))
        self.assertNotIn("todo.board", self.actions(self.bursars["lagoon"], "to-do"))

    def test_a_condition_is_asked_only_when_a_matched_place_needs_it_and_once(self):
        from vs_rbac import scoping

        with mock.patch.object(
            scoping, "only_branch_id_or_several", wraps=scoping.only_branch_id_or_several,
        ) as asked:
            self.actions(self.bursars["lagoon"], "invoice")
            self.assertEqual(asked.call_count, 0)
            keys = self.actions(self.bursars["lagoon"], "inter branch")
            self.assertGreater(len([k for k in keys if "inter_branch" in k]), 1)
            self.assertEqual(asked.call_count, 1)

    def test_an_unconditional_search_costs_no_more_queries_than_before(self):
        """Matched places with no condition add no query for the conditions."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = TenantAPIClient(self.bursars["lagoon"])
        client.get(URL, {"q": "invoice"})
        with CaptureQueriesContext(connection) as plain:
            client.get(URL, {"q": "invoice"})
        with CaptureQueriesContext(connection) as conditional:
            client.get(URL, {"q": "inter branch"})
        self.assertLessEqual(len(conditional) - len(plain), 2)
