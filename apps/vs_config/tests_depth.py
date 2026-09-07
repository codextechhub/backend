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

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

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
