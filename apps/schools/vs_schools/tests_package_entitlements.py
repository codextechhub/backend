"""School package setup, and the entitlements it grants through vs_config.

The onboarding wizard's package step is the only place in the school app that
grants capabilities. It used to write ``CapabilityEntitlement`` rows itself,
with a ``school=`` field the model does not have and a ``school:<pk>`` scope
key nothing else looks for. Both halves were broken:

* the read (``get_enabled_modules``) raised ``FieldError``, so school detail
  blew up for any school that had a package setup at all;
* the write raised before it could store anything.

The write now goes through ``vs_config.services.capabilities.set_entitlement``,
which owns the ``tenant:<pk>`` scope key and the audit trail. These tests pin
the thing that actually matters: a grant written by school onboarding has to be
the same row vs_config's own evaluation reads back. Asserting only "a row
exists" would have passed against the old scope key too.

Creating a school with ``package_setup_data`` is the main creation path, so it
has to be exercised here rather than assumed.
"""
from datetime import timedelta

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
from vs_tenants.models import BranchStatus

from .models import PackagePlan, School, SchoolPackageSetup


class SchoolPackageEntitlementTests(TestCase):
    """Creating a school with a package, and reading it back."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="package-entitlements@example.com", super_admin=True
        )

        cls.plan = PackagePlan.objects.create(
            name="Entitlement Test Plan",
            code="entitlement-test",
            max_students=5000,
            max_teachers=500,
            max_admins=50,
        )

        # procurement requires finance: the wizard may send only procurement,
        # and finance still has to end up granted or procurement would be
        # entitled-but-off.
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

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _payload(self, name, slug, modules, branches=None):
        payload = {
            "name": name,
            "slug": slug,
            "package_setup_data": {
                "package_plan": self.plan.code,
                "enabled_modules": modules,
                "student_capacity": 100,
                "teacher_capacity": 10,
                "admin_capacity": 5,
            },
            # Every school is created with its main branch. These tests are
            # about entitlements, so the branch is scenery, but it has to be
            # there for the payload to be accepted at all.
            "branches": branches if branches is not None else [{
                "name": f"{name} Main Branch",
                "_type": "Main",
                "state": "Lagos",
                "is_main": True,
                "primary_admin_data": {
                    "full_name": f"{name} Head",
                    "email": f"head@{slug}.test",
                },
            }],
        }
        return payload

    def _create(self, *args, **kwargs):
        response = self._client().post(
            reverse("school-create"), self._payload(*args, **kwargs), format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response

    # --- the write --------------------------------------------------------

    def test_creating_a_school_with_a_package_grants_its_modules(self):
        """The path that used to raise before it wrote anything."""
        self._create("Grant School", "ent-grant", ["ent-students"])

        school = School.objects.get(slug="ent-grant")
        rows = CapabilityEntitlement.all_objects.filter(tenant=school.tenant)

        self.assertEqual(
            {row.capability.key for row in rows}, {"ent-students"},
        )
        row = rows.get()
        self.assertEqual(row.state, CapabilityEntitlement.State.GRANTED)
        self.assertEqual(row.source, CapabilityEntitlement.Source.PACKAGE)

    def test_the_grant_uses_the_tenant_scope_key_vs_config_looks_for(self):
        """The assertion the old ``school:<pk>`` key would have failed.

        A row with the wrong scope key still exists and still has the right
        capability, so counting rows proves nothing. This is the check that
        the grant is visible to the module that owns entitlements.
        """
        self._create("Scope School", "ent-scope", ["ent-students"])
        school = School.objects.get(slug="ent-scope")

        row = CapabilityEntitlement.all_objects.get(tenant=school.tenant)
        self.assertEqual(row.scope_key, f"tenant:{school.tenant.pk}")

    def test_vs_config_evaluates_the_granted_capability_as_effective(self):
        """End to end: onboarding grants it, vs_config switches it on."""
        self._create("Effective School", "ent-effective", ["ent-students"])
        school = School.objects.get(slug="ent-effective")

        self.assertTrue(
            effective_capability(self.students, tenant=school.tenant)
        )
        # A module that was never in the package stays off.
        self.assertFalse(
            effective_capability(self.finance, tenant=school.tenant)
        )

        states = {
            item["key"]: item["enabled"]
            for item in bulk_effective_capabilities(tenant=school.tenant)
        }
        self.assertTrue(states["ent-students"])
        self.assertFalse(states["ent-finance"])

    def test_the_package_setup_row_is_still_created(self):
        self._create("Setup School", "ent-setup", ["ent-students"])
        school = School.objects.get(slug="ent-setup")

        setup = SchoolPackageSetup.objects.get(school=school)
        self.assertEqual(setup.package_plan, self.plan)
        self.assertEqual(setup.student_capacity, 100)
        # Not supplied, so it defaults a year out rather than staying null.
        self.assertIsNotNone(setup.subscription_expires_at)

    def test_a_grant_is_audited_by_the_service(self):
        """Routing through ``set_entitlement`` is what produces this row;
        the hand-rolled ``update_or_create`` never wrote one."""
        from vs_config.models import ConfigurationAuditEvent

        self._create("Audit School", "ent-audit", ["ent-students"])
        school = School.objects.get(slug="ent-audit")

        self.assertTrue(
            ConfigurationAuditEvent.objects.filter(
                action="config.entitlement.updated", tenant=school.tenant,
            ).exists()
        )

    # --- dependency expansion ---------------------------------------------

    def test_a_required_capability_is_granted_even_when_not_picked(self):
        """procurement was ticked, finance was not; both must be entitled."""
        self._create("Dependency School", "ent-dependency", ["ent-procurement"])
        school = School.objects.get(slug="ent-dependency")

        granted = {
            row.capability.key
            for row in CapabilityEntitlement.all_objects.filter(tenant=school.tenant)
        }
        self.assertEqual(granted, {"ent-procurement", "ent-finance"})

    def test_the_dependent_capability_evaluates_as_effective(self):
        """The point of expanding: procurement is actually usable, which it
        would not be if finance had been left ungranted."""
        self._create("Usable School", "ent-usable", ["ent-procurement"])
        school = School.objects.get(slug="ent-usable")

        self.assertTrue(
            effective_capability(self.procurement, tenant=school.tenant)
        )

    def test_an_empty_module_list_grants_nothing(self):
        self._create("Bare School", "ent-bare", [])
        school = School.objects.get(slug="ent-bare")

        self.assertFalse(
            CapabilityEntitlement.all_objects.filter(tenant=school.tenant).exists()
        )

    # --- the read ---------------------------------------------------------

    def test_school_detail_returns_the_enabled_modules(self):
        """The read that raised ``FieldError`` for every school with a
        package. A 200 here is the whole regression."""
        self._create("Detail School", "ent-detail", ["ent-students"])

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-detail"})
        )

        self.assertEqual(response.status_code, 200, response.data)
        modules = response.data["data"]["package_setup"]["enabled_modules"]
        self.assertEqual({row["key"] for row in modules}, {"ent-students"})

    def test_school_detail_lists_dependency_expanded_modules_too(self):
        self._create("Detail Dep School", "ent-detail-dep", ["ent-procurement"])

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-detail-dep"})
        )

        self.assertEqual(response.status_code, 200, response.data)
        modules = response.data["data"]["package_setup"]["enabled_modules"]
        self.assertEqual(
            {row["key"] for row in modules}, {"ent-procurement", "ent-finance"},
        )

    def test_school_detail_without_a_package_still_works(self):
        """The branch that was never broken, kept honest: no package setup
        means no nested payload and certainly no crash."""
        response = self._client().post(
            reverse("school-create"),
            {
                "name": "No Package School",
                "slug": "ent-nopackage",
                "branches": [{
                    "name": "No Package Main", "_type": "Main", "state": "Lagos",
                    "is_main": True,
                    "primary_admin_data": {
                        "full_name": "No Package Head",
                        "email": "head@ent-nopackage.test",
                    },
                }],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-nopackage"})
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["package_setup"])

    # --- who gets recorded as the actor -----------------------------------

    def test_the_actor_is_recorded_on_the_entitlement(self):
        self._create("Actor School", "ent-actor", ["ent-students"])
        school = School.objects.get(slug="ent-actor")

        row = CapabilityEntitlement.all_objects.get(tenant=school.tenant)
        self.assertEqual(row.updated_by, self.vision_user)

    def test_a_string_actor_id_in_context_does_not_reach_updated_by(self):
        """``context["actor_id"]`` is not reliably a user.

        The API views put a User object in it, but the bulk school importer
        (``vs_import_data.services.import_executor``) puts ``str(user.id)``.
        ``updated_by`` is a FK, so the old ``updated_by=context["actor_id"]``
        would raise ValueError on every imported school with a package and
        roll the import back. The actor now comes from ``request.user``,
        which is a real user on both paths.
        """
        from types import SimpleNamespace

        from .serializers import SchoolCreateSerializer

        serializer = SchoolCreateSerializer(
            data=self._payload("Import School", "ent-import", ["ent-students"]),
            context={
                "request": SimpleNamespace(user=self.vision_user),
                "actor_id": str(self.vision_user.id),
            },
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        school = serializer.save()

        row = CapabilityEntitlement.all_objects.get(tenant=school.tenant)
        self.assertEqual(row.updated_by, self.vision_user)
        self.assertEqual(row.scope_key, f"tenant:{school.tenant.pk}")

        # Same root cause, second victim: emit_audit_event never raises, so a
        # string actor silently produced no school-creation audit row at all.
        from vs_audit.models import AuditEvent

        self.assertTrue(
            AuditEvent.objects.filter(
                entity_type="School", entity_id=str(school.pk),
                actor_user=self.vision_user,
            ).exists()
        )

    # --- tenant isolation -------------------------------------------------

    def test_one_schools_detail_never_shows_another_schools_modules(self):
        """Entitlements are keyed by tenant, and the read filters on the
        school's own tenant. Two schools with different packages must not
        bleed into each other - and ``all_objects`` is unscoped, so nothing
        but that explicit filter is holding the line.
        """
        self._create("Isolation A", "ent-iso-a", ["ent-students"])
        self._create("Isolation B", "ent-iso-b", ["ent-procurement"])

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-iso-a"})
        )

        self.assertEqual(response.status_code, 200, response.data)
        modules = {
            row["key"]
            for row in response.data["data"]["package_setup"]["enabled_modules"]
        }
        self.assertEqual(modules, {"ent-students"})
        self.assertNotIn("ent-procurement", modules)
        self.assertNotIn("ent-finance", modules)

    def test_a_platform_wide_grant_is_not_reported_as_this_schools_package(self):
        """A NULL-tenant row means "every tenant", not "this school bought
        it". The package read must stay tenant-specific or it would claim
        platform grants as the school's own."""
        self._create("Platform School", "ent-platform", ["ent-students"])

        CapabilityEntitlement.objects.create(
            capability=self.finance, tenant=None,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
        )

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-platform"})
        )

        self.assertEqual(response.status_code, 200, response.data)
        modules = {
            row["key"]
            for row in response.data["data"]["package_setup"]["enabled_modules"]
        }
        self.assertEqual(modules, {"ent-students"})

    # --- both tenant shapes -----------------------------------------------

    def test_a_multi_branch_school_grants_and_reads_the_same_way(self):
        """Entitlements sit at the tenant, so branches must not change the
        answer - the multi-branch shape has to behave like the bare one."""
        self._create(
            "Multi Branch School", "ent-multi", ["ent-students"],
            branches=[
                {
                    "name": "HQ", "_type": "Secondary", "is_main": True,
                    "primary_admin_data": {
                        "full_name": "HQ Head",
                        "email": "ent-multi-hq@example.com",
                    },
                },
                {
                    "name": "Annex", "_type": "Secondary", "is_main": False,
                    "primary_admin_data": {
                        "full_name": "Annex Head",
                        "email": "ent-multi-annex@example.com",
                    },
                },
            ],
        )
        school = School.objects.get(slug="ent-multi")
        self.assertEqual(school.tenant.branches.count(), 2)

        row = CapabilityEntitlement.all_objects.get(tenant=school.tenant)
        self.assertEqual(row.scope_key, f"tenant:{school.tenant.pk}")

        response = self._client().get(
            reverse("school-detail", kwargs={"slug": "ent-multi"})
        )
        self.assertEqual(response.status_code, 200, response.data)
        modules = response.data["data"]["package_setup"]["enabled_modules"]
        self.assertEqual({row["key"] for row in modules}, {"ent-students"})

    def test_a_branch_scoped_capability_check_inherits_the_tenant_grant(self):
        """A branch of an entitled tenant is entitled: nothing about the
        grant is branch-specific."""
        self._create("Branch Check School", "ent-branchcheck", ["ent-students"])
        school = School.objects.get(slug="ent-branchcheck")
        # Not main: the school already has the main branch it was created with.
        branch = make_branch(
            school, name="Later Branch", is_main=False, status=BranchStatus.ACTIVE,
        )

        self.assertTrue(
            effective_capability(
                self.students, tenant=school.tenant, branch=branch,
            )
        )

    def test_creating_a_second_school_does_not_disturb_the_first(self):
        """``set_entitlement`` upserts on (capability, scope_key). Two
        tenants picking the same module must get two rows, not one
        overwritten one - the unique constraint spans the scope key, and a
        scope key that ignored the tenant would collapse them."""
        self._create("First School", "ent-first", ["ent-students"])
        self._create("Second School", "ent-second", ["ent-students"])

        first = School.objects.get(slug="ent-first")
        second = School.objects.get(slug="ent-second")

        self.assertEqual(
            CapabilityEntitlement.all_objects.filter(
                capability=self.students,
            ).count(),
            2,
        )
        self.assertTrue(effective_capability(self.students, tenant=first.tenant))
        self.assertTrue(effective_capability(self.students, tenant=second.tenant))


class PlanBranchCeilingTests(TestCase):
    """``PackagePlan.max_branch``, and the creation paths that must read it.

    ``seed_package`` fills the column with real numbers - Starter 1, Standard 5,
    Premium 20 - and the plans screen shows them, so a creation path that does
    not look lets Bright Star sign for one site and open four.
    That is not a limit a school worked around, it is a promise the product made
    on its own pricing page and then broke by itself.

    Both ways in are covered, because they fail differently. The standalone
    endpoint adds a site to a school that already has some; school creation
    arrives with the whole list at once and could walk straight past the ceiling
    in a single request without ever adding a second branch to anything.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="branch-ceiling@example.com", super_admin=True,
        )
        cls.starter = PackagePlan.objects.create(
            name="Starter", code="ceiling-starter",
            max_students=500, max_teachers=50, max_admins=5, max_branch=1,
        )
        cls.standard = PackagePlan.objects.create(
            name="Standard", code="ceiling-standard",
            max_students=5000, max_teachers=500, max_admins=50, max_branch=3,
        )
        cls.unlimited = PackagePlan.objects.create(
            name="Enterprise", code="ceiling-enterprise",
            max_students=None, max_teachers=None, max_admins=None, max_branch=None,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _school_on(self, plan, *, slug, name, branches=("Ikeja",)):
        """An ACTIVE school with a plan and some sites already open.

        Built through the ORM rather than the create endpoint because that
        endpoint leaves a school PENDING and the branch endpoint serves only
        ACTIVE ones - which is the state a school is in for every branch it
        opens after onboarding, and therefore the state this ceiling is
        actually enforced in.
        """
        school = make_school(slug=slug, name=name)
        for index, branch_name in enumerate(branches):
            make_branch(
                school, name=branch_name, is_main=index == 0,
                status=BranchStatus.ACTIVE,
            )
        if plan is not None:
            SchoolPackageSetup.objects.create(
                school=school,
                package_plan=plan,
                student_capacity=100,
                teacher_capacity=10,
                admin_capacity=5,
                subscription_expires_at=timezone.localdate() + timedelta(days=365),
            )
        return school

    def _add_branch(self, school, *, name, expect):
        response = self._client().post(
            reverse("branch-create", kwargs={"slug": school.slug}),
            {
                "name": name,
                "state": "Lagos",
                "is_main": False,
                "primary_admin_data": {
                    "full_name": f"{name} Head",
                    "email": f"{name.lower().replace(' ', '-')}@{school.slug}.test",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def _onboard(self, *, slug, plan, branch_names, expect):
        payload = {
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "branches": [
                {
                    "name": name,
                    "state": "Lagos",
                    "is_main": index == 0,
                    "primary_admin_data": {
                        "full_name": f"{name} Head",
                        "email": f"head-{index}@{slug}.test",
                    },
                }
                for index, name in enumerate(branch_names)
            ],
        }
        if plan is not None:
            payload["package_setup_data"] = {
                "package_plan": plan.code,
                "enabled_modules": [],
                "student_capacity": 100,
                "teacher_capacity": 10,
                "admin_capacity": 5,
            }
        response = self._client().post(
            reverse("school-create"), payload, format="json",
        )
        self.assertEqual(response.status_code, expect, response.data)
        return response

    # --- the standalone endpoint ------------------------------------------

    def test_a_starter_school_cannot_open_a_second_branch(self):
        school = self._school_on(
            self.starter, slug="ceiling-bright-star", name="Ceiling Bright Star",
        )

        response = self._add_branch(school, name="Lekki", expect=400)

        self.assertIn("Starter allows 1 branch", str(response.data))
        self.assertEqual(school.tenant.branches.count(), 1)

    def test_a_standard_school_may_open_branches_up_to_its_ceiling(self):
        """The refusal must not be a blanket one."""
        school = self._school_on(
            self.standard, slug="ceiling-greenfield", name="Ceiling Greenfield",
        )

        self._add_branch(school, name="Lekki", expect=201)
        self._add_branch(school, name="Yaba", expect=201)
        self._add_branch(school, name="Surulere", expect=400)

        self.assertEqual(school.tenant.branches.count(), 3)

    def test_an_unlimited_plan_has_no_ceiling(self):
        """``max_branch=None`` is what Enterprise means by unlimited."""
        school = self._school_on(
            self.unlimited, slug="ceiling-corona", name="Ceiling Corona",
        )

        for name in ["Lekki", "Yaba", "Surulere", "Apapa"]:
            self._add_branch(school, name=name, expect=201)

        self.assertEqual(school.tenant.branches.count(), 5)

    def test_a_school_with_no_package_setup_has_no_ceiling(self):
        """A school onboarded before its plan is chosen is not on plan zero."""
        school = self._school_on(
            None, slug="ceiling-nosetup", name="Ceiling No Setup",
        )

        self._add_branch(school, name="Lekki", expect=201)

    def test_a_closed_branch_gives_its_seat_back(self):
        """Closing Ikeja and opening Yaba is a replacement, not growth.

        CLOSED is terminal - the site is gone. Counting it would mean a school
        on a one-site plan that shuts its only site can never open another,
        only ever buy a bigger plan.
        """
        school = self._school_on(
            self.starter, slug="ceiling-closed", name="Ceiling Closed",
        )
        make_branch(
            school, name="Old Yaba", is_main=False, status=BranchStatus.CLOSED,
        )

        response = self._add_branch(school, name="New Yaba", expect=400)
        self.assertIn("Starter allows 1 branch", str(response.data))

        school.tenant.branches.filter(name="Ikeja").update(
            status=BranchStatus.CLOSED,
        )
        self._add_branch(school, name="New Yaba", expect=201)

    def test_a_suspended_branch_keeps_its_seat(self):
        """A site expected back is still a site the school is paying for."""
        school = self._school_on(
            self.starter, slug="ceiling-suspended", name="Ceiling Suspended",
        )
        school.tenant.branches.update(status=BranchStatus.SUSPENDED)

        self._add_branch(school, name="Lekki", expect=400)

    # --- onboarding, where the whole list arrives at once -------------------

    def test_a_starter_school_cannot_be_onboarded_with_two_branches(self):
        """The loophole that made the endpoint check alone worth nothing."""
        response = self._onboard(
            slug="ceiling-multi", plan=self.starter,
            branch_names=["Ikeja", "Lekki"], expect=400,
        )

        self.assertIn("Starter allows 1 branch", str(response.data))
        self.assertFalse(School.objects.filter(slug="ceiling-multi").exists())

    def test_a_standard_school_may_be_onboarded_with_three(self):
        self._onboard(
            slug="ceiling-three", plan=self.standard,
            branch_names=["Ikeja", "Lekki", "Yaba"], expect=201,
        )

        school = School.objects.get(slug="ceiling-three")
        self.assertEqual(school.tenant.branches.count(), 3)

    def test_a_school_onboarded_without_a_package_is_not_refused(self):
        """The package step is optional; no plan is no ceiling, not zero."""
        self._onboard(
            slug="ceiling-planless", plan=None,
            branch_names=["Ikeja", "Lekki"], expect=201,
        )
