"""Depth: how far into a module a tenant reaches, and what that shuts off.

The catalogue is two levels deep. A module is what a school is sold; a band is
a slice of that module cut at Core, Plus or Advanced. Nobody buys a band. A
band answers when the tenant's depth for its parent module reaches it, and the
whole point of storing it that way is that moving a feature between bands is a
single field edit that every school sees immediately.

These tests pin four things that would each be silently wrong in a way no
screen would show:

* a grant written before depth existed still reaches everything, because
  reading a missing depth as Core would have taken Advanced work away from
  every school on the platform on the day depth shipped;
* a band is refused for depth and refused for entitlement by two different
  routes, so a caller can tell a school that has not bought Finance from one
  that has bought it shallow;
* an uplift given as part of a deal expires on its own and drops the module
  back to the tier that was paid for, rather than taking the module away;
* the bulk evaluator, which is what the API and every screen actually read,
  agrees with the single-capability path on all of it.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from vs_rbac.models import (
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionModule,
    PermissionResource,
    RBACAuditLog,
    TenantRolePermission,
    TenantRoleTemplate,
)
from vs_rbac.plan_grants import UNSETTLED_SOURCE
from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user

from .models import (
    Capability,
    CapabilityDepthGrant,
    CapabilityEntitlement,
    CapabilityOverride,
)
from .services.capabilities import (
    bulk_effective_capabilities,
    clear_depth_grant,
    effective_capability,
    set_depth_grant,
    set_entitlement,
    set_override,
)
from .services.depth import UNLIMITED, depth_allows, resolved_depth


class DepthCatalogueShapeTests(TestCase):
    """The invariants that keep the catalogue two levels deep."""

    def setUp(self):
        self.finance = Capability.objects.create(key="d-finance", label="Finance")

    def test_a_module_carries_no_depth(self):
        self.finance.depth = Capability.Depth.PLUS
        with self.assertRaises(ValidationError):
            self.finance.save()

    def test_a_band_must_name_its_depth(self):
        band = Capability(key="d-finance-plus", label="Finance Plus", parent=self.finance)
        with self.assertRaises(ValidationError):
            band.save()

    def test_a_band_cannot_be_a_band_of_a_band(self):
        plus = Capability.objects.create(
            key="d-finance-plus", label="Finance Plus",
            parent=self.finance, depth=Capability.Depth.PLUS,
        )
        deeper = Capability(
            key="d-finance-deeper", label="Deeper",
            parent=plus, depth=Capability.Depth.ADVANCED,
        )
        with self.assertRaises(ValidationError):
            deeper.save()

    def test_a_module_with_bands_cannot_become_a_band(self):
        Capability.objects.create(
            key="d-finance-plus", label="Finance Plus",
            parent=self.finance, depth=Capability.Depth.PLUS,
        )
        other = Capability.objects.create(key="d-other", label="Other")
        self.finance.parent = other
        self.finance.depth = Capability.Depth.CORE
        with self.assertRaises(ValidationError):
            self.finance.save()

    def test_module_property_answers_for_both_shapes(self):
        plus = Capability.objects.create(
            key="d-finance-plus", label="Finance Plus",
            parent=self.finance, depth=Capability.Depth.PLUS,
        )
        self.assertEqual(self.finance.module, self.finance)
        self.assertEqual(plus.module, self.finance)

    def test_depth_values_are_ordered(self):
        # The ordering is the whole reason depth is an integer. Comparing
        # labels would put Advanced before Core.
        self.assertLess(Capability.Depth.CORE, Capability.Depth.PLUS)
        self.assertLess(Capability.Depth.PLUS, Capability.Depth.ADVANCED)


class _DepthFixture(TestCase):
    """A module with all three of its bands, and a school to sell it to."""

    def setUp(self):
        self.school = make_school(slug="depth-school")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.actor = make_vision_user(email="depth-actor@example.com")

        self.finance = Capability.objects.create(
            key="dp-finance", label="Finance", requires_entitlement=True,
        )
        self.core = Capability.objects.create(
            key="dp-finance-core", label="Finance Core",
            parent=self.finance, depth=Capability.Depth.CORE,
        )
        self.plus = Capability.objects.create(
            key="dp-finance-plus", label="Finance Plus",
            parent=self.finance, depth=Capability.Depth.PLUS,
        )
        self.advanced = Capability.objects.create(
            key="dp-finance-advanced", label="Finance Advanced",
            parent=self.finance, depth=Capability.Depth.ADVANCED,
        )

    def grant(self, depth):
        return set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.actor, depth=depth,
        )

    def bands_on(self):
        """The band keys that answer for this tenant, read the way the API does."""
        states = {
            row["key"]: row["enabled"]
            for row in bulk_effective_capabilities(tenant=self.tenant)
        }
        return {key for key, enabled in states.items() if enabled and key.startswith("dp-")}


class DepthResolutionTests(_DepthFixture):
    """Which bands answer, for a tenant on each tier."""

    def test_an_unentitled_module_closes_every_band(self):
        self.assertFalse(effective_capability(self.core, tenant=self.tenant))
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))
        self.assertEqual(self.bands_on(), set())

    def test_core_reaches_core_only(self):
        self.grant(Capability.Depth.CORE)
        self.assertTrue(effective_capability(self.core, tenant=self.tenant))
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))
        self.assertFalse(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(self.bands_on(), {"dp-finance", "dp-finance-core"})

    def test_plus_reaches_core_and_plus(self):
        self.grant(Capability.Depth.PLUS)
        self.assertTrue(effective_capability(self.core, tenant=self.tenant))
        self.assertTrue(effective_capability(self.plus, tenant=self.tenant))
        self.assertFalse(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(
            self.bands_on(), {"dp-finance", "dp-finance-core", "dp-finance-plus"},
        )

    def test_advanced_reaches_everything(self):
        self.grant(Capability.Depth.ADVANCED)
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(
            self.bands_on(),
            {"dp-finance", "dp-finance-core", "dp-finance-plus", "dp-finance-advanced"},
        )

    def test_a_grant_with_no_depth_reaches_everything(self):
        # Every entitlement written before depth existed has depth NULL. If
        # NULL read as Core, those schools would lose payroll on deploy day
        # without a single row changing.
        self.grant(None)
        self.assertIs(resolved_depth(self.finance, self.tenant), UNLIMITED)
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(
            self.bands_on(),
            {"dp-finance", "dp-finance-core", "dp-finance-plus", "dp-finance-advanced"},
        )

    def test_setting_state_alone_leaves_depth_where_it_was(self):
        # Most callers of set_entitlement have no opinion about depth. One of
        # them changing the dates must not quietly widen the tier.
        self.grant(Capability.Depth.CORE)
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.MANUAL, actor=self.actor,
            ends_at=timezone.now() + timedelta(days=365),
        )
        self.assertEqual(resolved_depth(self.finance, self.tenant), Capability.Depth.CORE)
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))

    def test_resolved_depth_answers_for_a_band_by_its_module(self):
        self.grant(Capability.Depth.PLUS)
        self.assertEqual(
            resolved_depth(self.advanced, self.tenant), Capability.Depth.PLUS,
        )

    def test_depth_allows_treats_no_limit_as_deeper_than_anything(self):
        self.assertTrue(depth_allows(Capability.Depth.ADVANCED, UNLIMITED))
        self.assertTrue(depth_allows(UNLIMITED, Capability.Depth.CORE))
        self.assertFalse(depth_allows(Capability.Depth.ADVANCED, Capability.Depth.PLUS))


class DepthGrantTests(_DepthFixture):
    """The deal: deeper reach for a while, that ends by itself."""

    def test_an_uplift_opens_a_band_the_tier_does_not(self):
        self.grant(Capability.Depth.CORE)
        set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
            ends_at=timezone.now() + timedelta(days=365),
            reason="Signed on the promise of payroll.",
        )
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(
            self.bands_on(),
            {"dp-finance", "dp-finance-core", "dp-finance-plus", "dp-finance-advanced"},
        )

    def test_an_expired_uplift_drops_back_to_the_tier_not_to_nothing(self):
        # The reason the uplift is its own row. Expiring it on the
        # entitlement would end the grant and take Finance away entirely,
        # rather than returning the school to the Core it pays for.
        self.grant(Capability.Depth.CORE)
        grant = set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
        )
        CapabilityDepthGrant.all_objects.filter(pk=grant.pk).update(
            ends_at=timezone.now() - timedelta(days=1),
        )
        self.assertTrue(effective_capability(self.finance, tenant=self.tenant))
        self.assertTrue(effective_capability(self.core, tenant=self.tenant))
        self.assertFalse(effective_capability(self.advanced, tenant=self.tenant))
        self.assertEqual(self.bands_on(), {"dp-finance", "dp-finance-core"})

    def test_an_uplift_scheduled_for_later_is_inert(self):
        self.grant(Capability.Depth.CORE)
        set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
            starts_at=timezone.now() + timedelta(days=7),
        )
        self.assertFalse(effective_capability(self.advanced, tenant=self.tenant))

    def test_an_uplift_never_demotes(self):
        # A school upgraded to Advanced keeps it even though a stale Plus
        # uplift from an old deal is still on the row.
        self.grant(Capability.Depth.ADVANCED)
        set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.PLUS, actor=self.actor,
        )
        self.assertEqual(
            resolved_depth(self.finance, self.tenant), Capability.Depth.ADVANCED,
        )
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))

    def test_an_uplift_is_recorded_against_the_module_when_given_a_band(self):
        self.grant(Capability.Depth.CORE)
        row = set_depth_grant(
            capability=self.advanced, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
        )
        self.assertEqual(row.capability, self.finance)

    def test_clearing_an_uplift_returns_the_module_to_its_tier(self):
        self.grant(Capability.Depth.CORE)
        set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
        )
        self.assertTrue(clear_depth_grant(
            capability=self.finance, tenant=self.tenant, actor=self.actor,
        ))
        self.assertFalse(effective_capability(self.advanced, tenant=self.tenant))
        self.assertFalse(clear_depth_grant(
            capability=self.finance, tenant=self.tenant, actor=self.actor,
        ))

    def test_an_uplift_does_not_reach_another_school(self):
        other = make_school(slug="depth-other-school")
        self.grant(Capability.Depth.CORE)
        set_entitlement(
            capability=self.finance, tenant=other.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.actor, depth=Capability.Depth.CORE,
        )
        set_depth_grant(
            capability=self.finance, tenant=self.tenant,
            depth=Capability.Depth.ADVANCED, actor=self.actor,
        )
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))
        self.assertFalse(effective_capability(self.advanced, tenant=other.tenant))


class DepthAgainstOtherGatesTests(_DepthFixture):
    """Depth sits alongside the gates that were already there."""

    def test_an_override_still_switches_a_reachable_band_off(self):
        self.grant(Capability.Depth.ADVANCED)
        set_override(
            capability=self.plus, state=CapabilityOverride.State.DISABLED,
            actor=self.actor, tenant=self.tenant,
        )
        self.assertTrue(effective_capability(self.core, tenant=self.tenant))
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))

    def test_a_branch_override_narrows_one_site_only(self):
        self.grant(Capability.Depth.ADVANCED)
        set_override(
            capability=self.plus, state=CapabilityOverride.State.DISABLED,
            actor=self.actor, tenant=self.tenant, branch=self.branch,
        )
        self.assertTrue(effective_capability(self.plus, tenant=self.tenant))
        self.assertFalse(
            effective_capability(self.plus, tenant=self.tenant, branch=self.branch)
        )

    def test_an_expired_entitlement_closes_the_module_and_every_band(self):
        self.grant(Capability.Depth.ADVANCED)
        CapabilityEntitlement.all_objects.filter(
            capability=self.finance, tenant=self.tenant,
        ).update(ends_at=timezone.now() - timedelta(days=1))
        self.assertFalse(effective_capability(self.finance, tenant=self.tenant))
        self.assertFalse(effective_capability(self.core, tenant=self.tenant))
        self.assertEqual(self.bands_on(), set())

    def test_an_archived_band_stops_answering_without_touching_its_siblings(self):
        self.grant(Capability.Depth.ADVANCED)
        Capability.objects.filter(pk=self.plus.pk).update(is_active=False)
        self.plus.refresh_from_db()
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))
        self.assertTrue(effective_capability(self.advanced, tenant=self.tenant))

    def test_moving_a_band_shallower_reaches_every_school_at_once(self):
        # The promise the whole design is for: rebanding is one field, and
        # nothing per tenant has to be rewritten for a school to see it.
        self.grant(Capability.Depth.CORE)
        self.assertFalse(effective_capability(self.plus, tenant=self.tenant))
        self.plus.depth = Capability.Depth.CORE
        self.plus.save()
        self.assertTrue(effective_capability(self.plus, tenant=self.tenant))
        self.assertEqual(
            CapabilityEntitlement.all_objects.filter(tenant=self.tenant).count(), 1,
        )


class AnEntitlementWriteReportsWhatItLeftStandingTests(TestCase):
    """A write that shallows a module names the role it could not settle.

    Taking a key back re-saves the role, and the save checks that role's
    permission dependencies. A key the tenant keeps may require one being taken
    away: the bursar keeps a reporting key that requires the payroll key the
    new depth no longer reaches, and the check refuses the save. The role is
    left exactly as it was, the write completes because a commercial decision
    is never blocked by one role's internal wiring, and the report travels in
    ``roles_needing_attention`` beside the row that was written.

    The same event is described on two screens, and it has to read the same way
    on both. An operator shallowing a module from the configuration screen and
    an operator moving a school down a tier from the plan screen are told the
    same thing, under the same key and in the same sentence, rather than one of
    them being told nothing and left to find the role in the audit trail.
    """

    def setUp(self):
        self.client = APIClient()
        self.operator = make_vision_user(
            email="entitlement-report@example.com", super_admin=True,
        )
        self.school = make_school(slug="entitlement-report-school")
        make_branch(self.school)
        self.tenant = self.school.tenant

        self.finance = Capability.objects.create(
            key="rep-finance", label="Finance", requires_entitlement=True,
        )
        self.core = Capability.objects.create(
            key="rep-finance-core", label="Finance Core", parent=self.finance,
            depth=Capability.Depth.CORE, requires_entitlement=False,
        )
        self.advanced = Capability.objects.create(
            key="rep-finance-advanced", label="Finance Advanced",
            parent=self.finance, depth=Capability.Depth.ADVANCED,
            requires_entitlement=False,
        )
        # Bought as part of a package, reaching every band. A settlement asks
        # whether the tenant was given a plan at all before it takes anything
        # back, so a grant written any other way would settle nothing and the
        # tests below would pass against a service doing nothing.
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.operator, depth=Capability.Depth.ADVANCED,
        )

        # A plan grants every module it sells, so one module's grant going away
        # leaves the others standing. A school holding no package grant at all
        # is unprovisioned rather than unentitled, the gate refuses it nothing,
        # and the settlement rightly takes nothing from it: a fixture with one
        # module would pass the tests below for that reason instead of the one
        # they are about.
        self.students = Capability.objects.create(
            key="rep-students", label="Students", requires_entitlement=True,
        )
        set_entitlement(
            capability=self.students, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.operator, depth=Capability.Depth.ADVANCED,
        )

        self.kept = self._permission_on(self.core, "cfgfin.ledger.view")
        self.revoked = self._permission_on(self.advanced, "cfgfin.payroll.pay")
        PermissionDependency.objects.create(
            permission=self.kept, depends_on=self.revoked,
        )
        self.bursar = self._role_with("bursar", "Bursar", self.kept, self.revoked)

    def _permission_on(self, capability, key):
        """A permission governed by one of this fixture's own bands.

        The capabilities here are invented, so nothing in the real catalogue
        points at them: a permission looked up rather than built would answer to
        no band, and the settlement would leave it alone for the wrong reason.
        """
        module, _ = PermissionModule.objects.get_or_create(
            name="cfgfin", defaults={"is_active": True},
        )
        resource, _ = PermissionResource.objects.get_or_create(
            module=module, name=key.split(".")[1], defaults={"is_active": True},
        )
        action, _ = PermissionAction.objects.get_or_create(
            name=key.rsplit(".", 1)[1], defaults={"is_active": True},
        )
        return Permission.objects.create(
            key=key, module=module, resource=resource, action=action,
            capability=capability, description=key, is_active=True,
            sensitivity_level="NORMAL", scope="TENANT",
        )

    def _role_with(self, key, name, *permissions):
        role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key=key, name=name, status="ACTIVE",
        )
        for permission in permissions:
            TenantRolePermission.objects.create(
                role=role, permission=permission, granted=True,
            )
        return role

    def _granted(self, role):
        return set(
            TenantRolePermission.objects.filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )

    def _sign_in(self):
        """Authenticate the way the console does.

        The scope a configuration write lands on comes from the tenant asserted
        on the token. A test that only forced authentication would write the
        platform's own row instead of this school's, and settle nothing.

        Sign-in is throttled per address, and the throttle counts in a cache
        that outlives a test. Clearing it keeps a test failing because of what
        it asserts rather than because of how many tests signed in before it.
        """
        cache.clear()
        response = self.client.post(
            "/v1/user/auth/login/",
            {
                "email": self.operator.email,
                "password": "testpass123",
                "tenant": self.operator.tenant.slug,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['data']['access']}"
        )

    def _post_entitlement(self, *, depth=None, reason="Re-writing the Finance grant."):
        """Write the grant through the endpoint, shallowing it when asked to.

        The request body carries state, source and dates and no depth, so the
        one field a shallowing write turns on is attached to the call the view
        makes. Everything either side of it is the real path: the real service
        writes the row, settles the tenant's role grants and records the role it
        could not save, and the view collects that report and renders it.
        """
        self._sign_in()
        url = f"/v1/config/entitlements/?tenant={self.school.slug}"
        body = {
            "capability": self.finance.key,
            "state": CapabilityEntitlement.State.GRANTED,
            "source": CapabilityEntitlement.Source.PACKAGE,
            "reason": reason,
        }
        if depth is None:
            return self.client.post(url, body, format="json")

        def shallower(**kwargs):
            return set_entitlement(**{**kwargs, "depth": depth})

        with patch("vs_config.views.set_entitlement", side_effect=shallower):
            return self.client.post(url, body, format="json")

    def test_the_endpoint_names_the_role_it_could_not_settle(self):
        response = self._post_entitlement(depth=Capability.Depth.CORE)

        self.assertEqual(response.status_code, 201, response.data)
        reported = response.data["data"]["roles_needing_attention"]
        self.assertEqual([entry["role_name"] for entry in reported], ["Bursar"])
        self.assertEqual(reported[0]["role_key"], "bursar")
        self.assertEqual(reported[0]["permission_keys"], [self.revoked.key])
        self.assertIn(self.revoked.key, reported[0]["detail"])
        self.assertIn(
            "Bursar", response.data["message"],
            "an operator reading only the message was told the grant saved and "
            "nothing more",
        )

    def test_the_write_still_answers_with_the_row_it_wrote(self):
        response = self._post_entitlement(depth=Capability.Depth.CORE)

        self.assertEqual(response.data["data"]["capability_key"], self.finance.key)
        self.assertEqual(response.data["data"]["tenant"], self.school.tenant_id)
        row = CapabilityEntitlement.all_objects.get(
            capability=self.finance, tenant=self.tenant,
        )
        self.assertEqual(row.depth, Capability.Depth.CORE)

    def test_the_audit_trail_still_names_the_role_the_endpoint_reported(self):
        self._post_entitlement(depth=Capability.Depth.CORE)

        entries = [
            entry
            for entry in RBACAuditLog.objects.filter(
                entity_type="TenantRoleTemplate", entity_id=str(self.bursar.pk),
            )
            if (entry.metadata or {}).get("source") == UNSETTLED_SOURCE
        ]
        self.assertEqual(len(entries), 1, "the role vanished from the trail")
        self.assertEqual(entries[0].entity_label, "Bursar")
        self.assertEqual(entries[0].status, "FAILED")
        self.assertEqual(entries[0].metadata["permission_keys"], [self.revoked.key])

    def test_every_other_role_in_the_tenant_is_still_settled(self):
        registrar = self._role_with("registrar", "Registrar", self.revoked)

        self._post_entitlement(depth=Capability.Depth.CORE)

        self.assertNotIn(
            self.revoked.key, self._granted(registrar),
            "one role that could not be saved stopped every other role in the "
            "tenant being settled",
        )

    def test_the_role_it_could_not_settle_is_left_exactly_as_it_was(self):
        version = self.bursar.version

        self._post_entitlement(depth=Capability.Depth.CORE)

        self.bursar.refresh_from_db()
        self.assertEqual(
            self._granted(self.bursar), {self.kept.key, self.revoked.key},
            "the role was half written instead of being left alone",
        )
        self.assertEqual(self.bursar.version, version)

    def test_a_write_that_narrows_nothing_reports_an_empty_list(self):
        response = self._post_entitlement()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["roles_needing_attention"], [])
        self.assertNotIn("attention", response.data["message"])
        self.assertEqual(
            self._granted(self.bursar), {self.kept.key, self.revoked.key},
            "a write that took nothing away rewrote a role anyway",
        )

    def test_a_narrowing_write_fills_the_list_its_caller_handed_it(self):
        # The collector belongs to the service rather than to the endpoint, so
        # every caller answering to a person can carry the same report.
        unsettled = []

        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.operator, depth=Capability.Depth.CORE,
            reason="The school moved down a tier.",
            unsettled_roles=unsettled,
        )

        self.assertEqual([entry["role_key"] for entry in unsettled], ["bursar"])
        self.assertEqual(unsettled[0]["permission_keys"], [self.revoked.key])

    def _platform_grant(self, depth):
        """The house's own grant for the same module.

        A tenant's own row wins over this one while it exists, which is what
        makes deleting that row a narrowing write rather than a tidy-up.
        """
        return set_entitlement(
            capability=self.finance, tenant=None,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PLATFORM,
            actor=self.operator, depth=depth,
        )

    def _reset_entitlement(self, reason="Returning Finance to the platform grant."):
        self._sign_in()
        return self.client.delete(
            f"/v1/config/entitlements/{self.finance.key}/?tenant={self.school.slug}",
            {"reason": reason},
            format="json",
        )

    def test_dropping_a_tenant_grant_to_a_shallower_one_takes_the_keys_too(self):
        self._platform_grant(Capability.Depth.CORE)
        registrar = self._role_with("registrar", "Registrar", self.revoked)

        response = self._reset_entitlement()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["cleared"])
        self.assertNotIn(
            self.revoked.key, self._granted(registrar),
            "the school returned to the depth the house sells with an Advanced "
            "key still granted, refused at the door and invisible in the picker",
        )

    def test_the_reset_names_the_role_it_could_not_settle(self):
        self._platform_grant(Capability.Depth.CORE)

        response = self._reset_entitlement()

        reported = response.data["data"]["roles_needing_attention"]
        self.assertEqual([entry["role_name"] for entry in reported], ["Bursar"])
        self.assertEqual(reported[0]["permission_keys"], [self.revoked.key])
        self.assertIn("Bursar", response.data["message"])
        self.assertEqual(
            self._granted(self.bursar), {self.kept.key, self.revoked.key},
            "the role was half written instead of being left alone",
        )

    def test_clearing_the_last_layer_closes_the_module_and_keeps_every_key(self):
        # A module closed rather than shallowed is a state meant to be
        # reversed, and the gate refuses every key behind it while it lasts.
        # Emptying the roles would cost the school a structure that paying the
        # invoice cannot give back. No platform grant stands behind this one,
        # so the module closes rather than returning to a shallower depth.
        registrar = self._role_with("registrar", "Registrar", self.revoked)

        response = self._reset_entitlement()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["effective"])
        self.assertIn(
            self.revoked.key, self._granted(registrar),
            "a module closed rather than shallowed emptied a role anyway",
        )
        self.assertEqual(response.data["data"]["roles_needing_attention"], [])

    def test_a_reset_that_narrows_nothing_reports_an_empty_list(self):
        # The house sells the module as deep as this school's own row did, so
        # the school reaches exactly what it reached before.
        self._platform_grant(Capability.Depth.ADVANCED)

        response = self._reset_entitlement()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["roles_needing_attention"], [])
        self.assertNotIn("attention", response.data["message"])
        self.assertEqual(
            self._granted(self.bursar), {self.kept.key, self.revoked.key},
        )

    def test_a_narrowing_write_with_no_collector_still_settles_the_tenant(self):
        # A caller with nobody reading its response still takes the grants
        # back, and still leaves the refused role whole.
        registrar = self._role_with("registrar", "Registrar", self.revoked)

        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=self.operator, depth=Capability.Depth.CORE,
            reason="The school moved down a tier.",
        )

        self.assertNotIn(self.revoked.key, self._granted(registrar))
        self.assertEqual(
            self._granted(self.bursar), {self.kept.key, self.revoked.key},
        )
