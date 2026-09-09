"""The plan a school pays for, and the grants it produces.

Onboarding's package step is the only place in the school app that grants
capabilities, and what it grants changed shape twice.

It first wrote ``CapabilityEntitlement`` rows itself, with a ``school=`` field
the model does not have and a ``school:<pk>`` scope key nothing else looks for.
Both halves were broken, so the write raised and the read blew up school
detail. The write moved to ``vs_config.services.capabilities.set_entitlement``,
which owns the ``tenant:<pk>`` scope key and the audit trail, and the tests
below still pin that: a grant written by school onboarding has to be the same
row vs_config's own evaluation reads back. Asserting only "a row exists" would
have passed against the old scope key too.

It then stopped picking modules. A wizard list decided what a school got, which
made the plan and the product two unrelated facts about the same school: a
school on the cheapest tier could be ticked into everything, and a school never
saw a module nobody thought to sell it. Now every school is granted every
module and the plan decides how deep, so what these tests assert is a depth,
not a set.

Two of them cover defects rather than design. A grant used to be written with
no end date while the subscription expiry sat unread on the row above, so a
school that stopped paying kept the product. And a plan used to cap how many
branches a school could open, which is a ceiling on a dial that is now priced
rather than fenced.

Creating a school with ``package_setup_data`` is the main creation path, so it
has to be exercised here rather than assumed.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from vs_config.models import (
    Capability,
    CapabilityDependency,
    CapabilityEntitlement,
)
from vs_config.services.capabilities import (
    bulk_effective_capabilities,
    effective_capability,
)
from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_tenants.models import Branch, BranchStatus

from .models import (
    CapabilityDepth,
    PackagePlan,
    PackagePlanModuleDepth,
    School,
    SchoolPackageSetup,
    SchoolStatus,
)


class _PackageFixture(TestCase):
    """Three modules, their bands, and an operator who can create schools."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="package-entitlements@example.com", super_admin=True
        )

        cls.basic = PackagePlan.objects.create(
            name="Entitlement Test Basic", code="entitlement-basic",
            default_depth=CapabilityDepth.CORE,
        )
        cls.premium = PackagePlan.objects.create(
            name="Entitlement Test Premium", code="entitlement-premium",
            default_depth=CapabilityDepth.ADVANCED,
        )

        # procurement requires finance. The dependency is still worth having
        # under a plan that grants both, because an operator can deny finance
        # for one school and procurement has to follow it off.
        cls.finance = Capability.objects.create(
            key="ent-finance", label="Finance", kind=Capability.Kind.MODULE,
        )
        cls.procurement = Capability.objects.create(
            key="ent-procurement", label="Procurement", kind=Capability.Kind.MODULE,
        )
        cls.students = Capability.objects.create(
            key="ent-students", label="Students", kind=Capability.Kind.MODULE,
        )
        CapabilityDependency.objects.create(
            capability=cls.procurement, requires=cls.finance,
        )
        cls.finance_core = Capability.objects.create(
            key="ent-finance-core", label="Finance Core", parent=cls.finance,
            depth=CapabilityDepth.CORE, requires_entitlement=False,
        )
        cls.finance_plus = Capability.objects.create(
            key="ent-finance-plus", label="Finance Plus", parent=cls.finance,
            depth=CapabilityDepth.PLUS, requires_entitlement=False,
        )
        cls.finance_advanced = Capability.objects.create(
            key="ent-finance-advanced", label="Finance Advanced", parent=cls.finance,
            depth=CapabilityDepth.ADVANCED, requires_entitlement=False,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _branch(self, name, slug, index=0):
        return {
            "name": f"{name} Branch {index}" if index else f"{name} Main Branch",
            "state": "Lagos",
            "is_main": index == 0,
            "primary_admin_data": {
                "full_name": f"{name} Head {index}",
                "email": f"head{index}@{slug}.test",
            },
        }

    def _payload(self, name, slug, *, plan=None, modules=None, branches=1, expires=None):
        package = {"package_plan": (plan or self.basic).code}
        if modules is not None:
            package["enabled_modules"] = modules
        if expires is not None:
            package["subscription_expires_at"] = expires.isoformat()
        return {
            "name": name,
            "slug": slug,
            "package_setup_data": package,
            "branches": [self._branch(name, slug, i) for i in range(branches)],
        }

    def _create(self, *args, **kwargs):
        response = self._client().post(
            reverse("school-create"), self._payload(*args, **kwargs), format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def _rows(self, school):
        return CapabilityEntitlement.all_objects.filter(tenant=school.tenant)


class PlanGrantsEveryModuleTests(_PackageFixture):
    """Every school gets every module. The plan decides how far in."""

    def test_a_plan_grants_every_sellable_module(self):
        self._create("Grant School", "ent-grant")
        school = School.objects.get(slug="ent-grant")
        self.assertEqual(
            {row.capability.key for row in self._rows(school)},
            {"ent-finance", "ent-procurement", "ent-students"},
        )

    def test_a_module_the_wizard_did_not_tick_is_granted_anyway(self):
        # The point of the change. A school that never asked for procurement
        # can still raise a requisition in week one and find out it wants it.
        self._create("Untick School", "ent-untick", modules=["ent-students"])
        school = School.objects.get(slug="ent-untick")
        self.assertIn(
            "ent-procurement", {row.capability.key for row in self._rows(school)},
        )

    def test_an_empty_module_list_no_longer_grants_nothing(self):
        # A frontend that has not caught up keeps working rather than
        # silently creating a school with no product at all.
        self._create("Empty School", "ent-empty", modules=[])
        school = School.objects.get(slug="ent-empty")
        self.assertEqual(self._rows(school).count(), 3)

    def test_bands_are_not_granted_separately(self):
        # Granting a band would be a second answer to a question the module's
        # depth already settles, and the two would drift.
        self._create("Band School", "ent-band")
        school = School.objects.get(slug="ent-band")
        self.assertFalse(
            self._rows(school).filter(capability__parent__isnull=False).exists()
        )

    def test_the_grant_uses_the_tenant_scope_key_vs_config_looks_for(self):
        """The assertion the old ``school:<pk>`` key would have failed.

        A row with the wrong scope key still exists and still has the right
        capability, so counting rows proves nothing. This is the check that
        the grant is visible to the module that owns entitlements.
        """
        self._create("Scope School", "ent-scope")
        school = School.objects.get(slug="ent-scope")
        self.assertEqual(
            set(self._rows(school).values_list("scope_key", flat=True)),
            {f"tenant:{school.tenant_id}"},
        )

    def test_the_grant_is_marked_as_coming_from_the_package(self):
        self._create("Source School", "ent-source")
        school = School.objects.get(slug="ent-source")
        for row in self._rows(school):
            self.assertEqual(row.state, CapabilityEntitlement.State.GRANTED)
            self.assertEqual(row.source, CapabilityEntitlement.Source.PACKAGE)

    def test_the_package_setup_row_is_still_created(self):
        self._create("Setup School", "ent-setup")
        school = School.objects.get(slug="ent-setup")
        setup = SchoolPackageSetup.objects.get(school=school)
        self.assertEqual(setup.package_plan, self.basic)


class PlanDepthTests(_PackageFixture):
    """Which bands a school reaches, and how a plan bends for one module."""

    def test_a_shallow_plan_reaches_core_only(self):
        self._create("Core School", "ent-core")
        school = School.objects.get(slug="ent-core")
        tenant = school.tenant
        self.assertTrue(effective_capability(self.finance_core, tenant=tenant))
        self.assertFalse(effective_capability(self.finance_plus, tenant=tenant))
        self.assertFalse(effective_capability(self.finance_advanced, tenant=tenant))

    def test_the_deepest_plan_reaches_everything(self):
        self._create("Deep School", "ent-deep", plan=self.premium)
        tenant = School.objects.get(slug="ent-deep").tenant
        self.assertTrue(effective_capability(self.finance_advanced, tenant=tenant))

    def test_a_plan_exception_overrides_its_own_default(self):
        # Standard everywhere, Advanced Finance. The shape a deal takes when
        # it is the plan that differs rather than the school.
        PackagePlanModuleDepth.objects.create(
            plan=self.basic, capability_key="ent-finance",
            depth=CapabilityDepth.ADVANCED,
        )
        self._create("Exception School", "ent-exception")
        tenant = School.objects.get(slug="ent-exception").tenant
        self.assertTrue(effective_capability(self.finance_advanced, tenant=tenant))

    def test_an_exception_does_not_leak_into_other_modules(self):
        PackagePlanModuleDepth.objects.create(
            plan=self.basic, capability_key="ent-finance",
            depth=CapabilityDepth.ADVANCED,
        )
        self._create("Narrow School", "ent-narrow")
        school = School.objects.get(slug="ent-narrow")
        depths = {
            row.capability.key: row.depth for row in self._rows(school)
        }
        self.assertEqual(depths["ent-finance"], CapabilityDepth.ADVANCED)
        self.assertEqual(depths["ent-students"], CapabilityDepth.CORE)

    def test_a_branch_scoped_check_inherits_the_tenant_depth(self):
        self._create("Branch Depth School", "ent-branch-depth", branches=2)
        school = School.objects.get(slug="ent-branch-depth")
        branch = Branch.all_objects.filter(tenant=school.tenant).first()
        self.assertTrue(
            effective_capability(self.finance_core, tenant=school.tenant, branch=branch)
        )
        self.assertFalse(
            effective_capability(self.finance_plus, tenant=school.tenant, branch=branch)
        )


class MovingDownATierTakesTheGrantsWithItTests(_PackageFixture):
    """What a school may do and what its roles say it may do stay one fact.

    Entitlements decide what the product offers; role grants are what the school
    handed its own people, and a tier change does not rewrite those on its own.
    Left alone, the bursar keeps every Advanced key after the school drops to
    Core: refused at the door, invisible in the picker, and back in force the
    moment the school moves up again for an unrelated reason.
    """

    def _permission_on(self, capability, key):
        """A permission governed by one of the fixture's own bands.

        The fixture invents its capabilities, so nothing in the real catalogue
        points at them. Looking for an existing permission found none and the
        tests skipped, which reads as passing and proves nothing.
        """
        from vs_rbac.models import (
            Permission, PermissionAction, PermissionModule, PermissionResource,
        )

        module, _ = PermissionModule.objects.get_or_create(
            name="entfin", defaults={"is_active": True},
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

    def _bursar_with(self, school, *keys):
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate,
        )

        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="bursar", name="Bursar", status="ACTIVE",
        )
        for key in keys:
            TenantRolePermission.objects.create(
                role=role, permission=Permission.objects.get(key=key), granted=True,
            )
        return role

    def _granted(self, role):
        from vs_rbac.models import TenantRolePermission

        return set(
            TenantRolePermission.objects.filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )

    def test_a_downgrade_revokes_what_the_new_tier_cannot_reach(self):
        from vs_rbac.models import Permission
        from schools.vs_schools.services.packages import change_plan

        self._create("Dropping School", "ent-drop", plan=self.premium)
        school = School.objects.get(slug="ent-drop")

        core_key = self._permission_on(self.finance_core, "entfin.ledger.view")
        deep_key = self._permission_on(self.finance_advanced, "entfin.payroll.pay")
        role = self._bursar_with(school, core_key.key, deep_key.key)

        change_plan(school=school, plan=self.basic, actor=self.vision_user)

        held = self._granted(role)
        self.assertIn(core_key.key, held, "core survived the drop")
        self.assertNotIn(deep_key.key, held, "the Advanced key outlived the tier")

    def test_it_leaves_alone_what_the_new_tier_still_reaches(self):
        from vs_rbac.models import Permission
        from schools.vs_schools.services.packages import change_plan

        self._create("Staying School", "ent-stay", plan=self.premium)
        school = School.objects.get(slug="ent-stay")

        core_key = self._permission_on(self.finance_core, "entfin.ledger.view")
        role = self._bursar_with(school, core_key.key)

        change_plan(school=school, plan=self.basic, actor=self.vision_user)

        self.assertIn(core_key.key, self._granted(role))

    def test_the_revocation_is_recorded_against_the_plan_change(self):
        """A permission that vanished with no record of why is the question
        this system exists to answer."""
        from vs_rbac.models import Permission, RBACAuditLog
        from schools.vs_schools.services.packages import change_plan

        self._create("Audited School", "ent-audited", plan=self.premium)
        school = School.objects.get(slug="ent-audited")
        deep_key = self._permission_on(self.finance_advanced, "entfin.payroll.pay")
        role = self._bursar_with(school, deep_key.key)

        change_plan(school=school, plan=self.basic, actor=self.vision_user)

        entry = RBACAuditLog.objects.filter(
            entity_type="TenantRoleTemplate", entity_id=str(role.pk),
        ).latest("created_at")
        self.assertEqual(entry.metadata["source"], "plan_downgrade")


class SubscriptionExpiryTests(_PackageFixture):
    """The expiry that was recorded and never enforced."""

    def test_the_grant_carries_the_subscription_expiry(self):
        expires = date.today() + timedelta(days=90)
        self._create("Expiry School", "ent-expiry", expires=expires)
        school = School.objects.get(slug="ent-expiry")
        for row in self._rows(school):
            self.assertIsNotNone(
                row.ends_at, "a grant with no end date is a product given away",
            )
            self.assertEqual(row.ends_at.date(), expires + timedelta(days=1))

    def test_the_school_keeps_the_product_on_its_last_paid_day(self):
        # ``ends_at`` is exclusive and the expiry is a date, so a school paid
        # up to the 31st must still work on the 31st.
        expires = date.today()
        self._create("Last Day School", "ent-last-day", expires=expires)
        tenant = School.objects.get(slug="ent-last-day").tenant
        self.assertTrue(effective_capability(self.finance, tenant=tenant))
        self.assertTrue(effective_capability(self.finance_core, tenant=tenant))

    def test_an_expired_subscription_closes_the_modules_and_their_bands(self):
        self._create("Lapsed School", "ent-lapsed")
        school = School.objects.get(slug="ent-lapsed")
        self._rows(school).update(ends_at=timezone.now() - timedelta(days=1))
        self.assertFalse(effective_capability(self.finance, tenant=school.tenant))
        self.assertFalse(effective_capability(self.finance_core, tenant=school.tenant))
        states = {
            row["key"]: row["enabled"]
            for row in bulk_effective_capabilities(tenant=school.tenant)
        }
        self.assertFalse(states["ent-finance"])
        self.assertFalse(states["ent-finance-core"])


class NoSizeCeilingTests(_PackageFixture):
    """A plan prices size; it does not fence it.

    A ceiling on branches used to be enforced on both creation paths. It is
    gone on purpose: refusing a proprietor the fourth site she planned for
    months, on the cheapest tier, made the product the reason a school could
    not open. Size is charged for instead, and the only wall left is a depth
    wall the school can see and ask about.
    """

    def test_the_cheapest_plan_may_be_onboarded_with_several_branches(self):
        self._create("Wide School", "ent-wide", branches=4)
        school = School.objects.get(slug="ent-wide")
        self.assertEqual(Branch.all_objects.filter(tenant=school.tenant).count(), 4)

    def test_a_school_may_open_another_branch_after_creation(self):
        self._create("Growing School", "ent-growing")
        school = School.objects.get(slug="ent-growing")
        # The standalone endpoint serves live schools only. A school arrives
        # PENDING from the wizard, which is a different gate from the ceiling
        # this test is about.
        school.status = SchoolStatus.ACTIVE
        school.save(update_fields=["status"])
        response = self._client().post(
            reverse("branch-create", kwargs={"slug": school.slug}),
            {
                "name": "Ikeja Branch",
                "state": "Lagos",
                "is_main": False,
                "primary_admin_data": {
                    "full_name": "Ikeja Head",
                    "email": "ikeja-head@ent-growing.test",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Branch.all_objects.filter(
                tenant=school.tenant, status__in=[BranchStatus.ACTIVE, BranchStatus.PENDING],
            ).count(),
            2,
        )

    def test_a_plan_carries_no_capacity_columns_at_all(self):
        field_names = {f.name for f in PackagePlan._meta.get_fields()}
        self.assertFalse(
            field_names & {"max_students", "max_teachers", "max_admins", "max_branch"},
            "a ceiling nobody enforces is a sales promise the product breaks quietly",
        )


class PackageIsolationTests(_PackageFixture):
    """One school's package never becomes another's."""

    def test_creating_a_second_school_does_not_disturb_the_first(self):
        self._create("First School", "ent-first")
        self._create("Second School", "ent-second", plan=self.premium)
        first = School.objects.get(slug="ent-first")
        second = School.objects.get(slug="ent-second")
        self.assertEqual(
            {row.depth for row in self._rows(first)}, {CapabilityDepth.CORE},
        )
        self.assertEqual(
            {row.depth for row in self._rows(second)}, {CapabilityDepth.ADVANCED},
        )

    def test_a_deep_school_does_not_open_a_shallow_school_s_bands(self):
        self._create("Shallow School", "ent-shallow")
        self._create("Deep Neighbour", "ent-neighbour", plan=self.premium)
        shallow = School.objects.get(slug="ent-shallow").tenant
        deep = School.objects.get(slug="ent-neighbour").tenant
        self.assertTrue(effective_capability(self.finance_advanced, tenant=deep))
        self.assertFalse(effective_capability(self.finance_advanced, tenant=shallow))

    def test_school_detail_returns_the_granted_modules(self):
        self._create("Detail School", "ent-detail")
        school = School.objects.get(slug="ent-detail")
        response = self._client().get(
            reverse("school-detail", kwargs={"slug": school.slug})
        )
        self.assertEqual(response.status_code, 200, response.data)
        setup = response.data["data"]["package_setup"]
        self.assertEqual(
            {row["key"] for row in setup["enabled_modules"]},
            {"ent-finance", "ent-procurement", "ent-students"},
        )

    def test_one_schools_detail_never_shows_another_schools_modules(self):
        self._create("Own School", "ent-own")
        other = make_school(slug="ent-outsider")
        make_branch(other)
        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-own"})
        )
        keys = {
            row["key"]
            for row in response.data["data"]["package_setup"]["enabled_modules"]
        }
        self.assertEqual(keys, {"ent-finance", "ent-procurement", "ent-students"})
        self.assertFalse(
            CapabilityEntitlement.all_objects.filter(tenant=other.tenant).exists()
        )

    def test_school_detail_without_a_package_still_works(self):
        school = make_school(slug="ent-packageless")
        make_branch(school)
        response = self._client().get(
            reverse("school-detail", kwargs={"slug": school.slug})
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"].get("package_setup"))


class PackageAuditTests(_PackageFixture):
    """Every grant is written through the service that audits it."""

    def test_a_grant_is_audited_by_the_service(self):
        from vs_config.models import ConfigurationAuditEvent

        self._create("Audit School", "ent-audit")
        school = School.objects.get(slug="ent-audit")
        events = ConfigurationAuditEvent.objects.filter(
            tenant=school.tenant, action="config.entitlement.updated",
        )
        self.assertEqual(events.count(), 3)

    def test_the_actor_is_recorded_on_the_entitlement(self):
        self._create("Actor School", "ent-actor")
        school = School.objects.get(slug="ent-actor")
        for row in self._rows(school):
            self.assertEqual(row.updated_by, self.vision_user)


class ApplyPlansCommandTests(_PackageFixture):
    """Bringing schools created before the plan decided anything onto it.

    The interesting case is a school whose grants predate depth. Those rows
    carry no depth, which means the school reaches every band - correct, and
    also the reason nothing can be sold to it. Applying its plan is what makes
    the plan and the product one fact.
    """

    def _apply(self, **options):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("apply_plans", stdout=out, **options)
        return out.getvalue()

    def test_a_school_with_depthless_grants_is_moved_onto_its_plan(self):
        self._create("Legacy School", "ent-legacy")
        school = School.objects.get(slug="ent-legacy")
        self._rows(school).update(depth=None)
        self.assertTrue(effective_capability(self.finance_advanced, tenant=school.tenant))

        self._apply()
        self.assertEqual(
            {row.depth for row in self._rows(school)}, {CapabilityDepth.CORE},
        )
        self.assertFalse(effective_capability(self.finance_advanced, tenant=school.tenant))

    def test_a_missing_module_is_granted(self):
        self._create("Partial School", "ent-partial")
        school = School.objects.get(slug="ent-partial")
        self._rows(school).filter(capability=self.procurement).delete()
        self.assertEqual(self._rows(school).count(), 2)

        self._apply()
        self.assertEqual(self._rows(school).count(), 3)

    def test_a_dry_run_writes_nothing(self):
        self._create("Dry School", "ent-dry")
        school = School.objects.get(slug="ent-dry")
        self._rows(school).update(depth=None)
        output = self._apply(dry_run=True)
        self.assertIn("DRY RUN", output)
        self.assertEqual({row.depth for row in self._rows(school)}, {None})

    def test_one_school_can_be_applied_alone(self):
        self._create("Target School", "ent-target")
        self._create("Bystander School", "ent-bystander")
        target = School.objects.get(slug="ent-target")
        bystander = School.objects.get(slug="ent-bystander")
        self._rows(target).update(depth=None)
        self._rows(bystander).update(depth=None)

        self._apply(slug="ent-target")
        self.assertEqual({row.depth for row in self._rows(target)}, {CapabilityDepth.CORE})
        self.assertEqual({row.depth for row in self._rows(bystander)}, {None})

    def test_a_deal_survives_the_plan_being_re_applied(self):
        # An uplift lives in its own table precisely so that re-applying the
        # tier underneath it does not take it away.
        from vs_config.services.capabilities import set_depth_grant

        self._create("Deal School", "ent-deal")
        school = School.objects.get(slug="ent-deal")
        set_depth_grant(
            capability=self.finance, tenant=school.tenant,
            depth=CapabilityDepth.ADVANCED, actor=self.vision_user,
            reason="Signed on the promise of payroll.",
        )
        self._apply()
        # The tier underneath is still Core, and the uplift still carries the
        # school past it to Advanced, which reaches Plus on the way.
        self.assertEqual(
            {row.depth for row in self._rows(school)}, {CapabilityDepth.CORE},
        )
        self.assertTrue(effective_capability(self.finance_plus, tenant=school.tenant))
        self.assertTrue(effective_capability(self.finance_advanced, tenant=school.tenant))

    def test_a_school_with_no_package_is_left_alone(self):
        school = make_school(slug="ent-nopackage")
        make_branch(school)
        self._apply()
        self.assertFalse(self._rows(school).exists())
