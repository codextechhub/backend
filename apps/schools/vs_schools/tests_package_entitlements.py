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

No test previously created a school with ``package_setup_data``, which is why a
crash on the main creation path went unnoticed.
"""
from django.test import TestCase
from django.urls import reverse
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
from vs_rbac.tests.helpers import make_branch, make_vision_user
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
                "name": f"{name} Main Campus",
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
                entity_type="School", entity_id=school.slug,
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
