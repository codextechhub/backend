"""The access catalogue: permissions and fields as one Module, Resource tree.

Two promises carry the screen. It shows a tenant nothing the older permission
catalogue would hide, and nothing more: flattened, its permission entries are
exactly that catalogue's. And a school never learns that a platform-only
permission or field exists.
"""
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import (
    Permission,
    PermissionModule,
    PermissionResource,
    PermissionScope,
)
from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
)


def _client(user):
    token = str(CodeXRefreshToken.for_user(user).access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _get(user, tenant, url_name="rbac-tenant-access-catalogue", *, slug=None, **params):
    return _client(user).get(
        reverse(url_name, kwargs={"tenant_slug": slug or tenant.slug}),
        {"tenant": tenant.slug, **params},
    )


def _reader(tenant, email, view_perm, key="catalogue_reader"):
    role = make_role(tenant, name="Role Reader", key=key)
    make_role_permission(role, view_perm)
    user = make_school_admin(None, email=email, tenant=tenant)
    make_assignment(tenant, user, role, branch=None)
    return user


def _grant_capability(tenant, key, label):
    from vs_config.models import Capability, CapabilityEntitlement

    capability, _ = Capability.objects.get_or_create(
        key=key, defaults={"label": label, "kind": Capability.Kind.MODULE},
    )
    if tenant is not None:
        CapabilityEntitlement.objects.create(
            tenant=tenant,
            capability=capability,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
        )
    return capability


def _flat_permissions(data):
    return sorted(
        (entry for module in data for resource in module["resources"]
         for entry in resource["permissions"]),
        key=lambda entry: entry["key"],
    )


def _resource(data, module, resource):
    for node in data:
        if node["module"] == module:
            for row in node["resources"]:
                if row["resource"] == resource:
                    return row
    return None


class AccessCatalogueTests(TestCase):
    """Bright Star has one branch; Greenfield has three."""

    @classmethod
    def setUpTestData(cls):
        cls.view_perm = make_permission("school.roles.view")
        for key in (
            "procurement.vendor.view",
            "procurement.vendor.create",
            "procurement.vendor.update",
            "finance.invoice.view",
            "finance.invoice.create",
            "school.students.view",
        ):
            make_permission(key)
        make_permission(
            "platform.impersonation.start_school", scope=PermissionScope.PLATFORM,
        )
        make_permission("platform.staff_profile.view", scope=PermissionScope.PLATFORM)
        PermissionModule.objects.filter(name="procurement").update(label="Procurement")
        PermissionResource.objects.filter(
            module_id="procurement", name="vendor",
        ).update(label="Vendors")

        make_field_definition(
            "procurement.vendor.bank_account_number", "Bank account number",
            group="Banking", sensitive=True, sort_order=40,
        )
        make_field_definition(
            "procurement.vendor.phone", "Phone", group="Contact", sensitive=True,
        )
        make_field_definition(
            "procurement.vendor.payment_terms", "Payment terms", group="Terms",
        )
        make_field_definition(
            "procurement.vendor.kyc_score", "KYC score", group="Terms",
            writable=False, sort_order=5,
        )
        make_field_definition(
            "procurement.vendor.retired", "Retired field", group="Terms",
            is_active=False,
        )
        make_field_definition("procurement.vendor_kyc.document", "KYC document")
        make_field_definition(
            "platform.staff_profile.account_number", "Account number",
            group="Banking", sensitive=True, scope=PermissionScope.PLATFORM,
        )

        cls.bright_star = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.bright_star, name="Main Branch")
        cls.admin = _reader(cls.bright_star.tenant, "admin@bright-star.test", cls.view_perm)

        cls.greenfield = make_school(slug="greenfield", name="Greenfield")
        make_branch(cls.greenfield, name="Ikeja Branch")
        make_branch(cls.greenfield, name="Lekki Branch", is_main=False)
        make_branch(cls.greenfield, name="Yaba Branch", is_main=False)
        cls.greenfield_admin = _reader(
            cls.greenfield.tenant, "admin@greenfield.test", cls.view_perm,
        )

        # Bright Star has bought Finance only, so its procurement keys are
        # closed. Greenfield has no grants at all, which reads as unprovisioned
        # and leaves everything open. The two shapes give the flattening test
        # both kinds of entry.
        _grant_capability(cls.bright_star.tenant, "finance", "Finance")
        _grant_capability(None, "procurement", "Procurement")

        clerk_role = make_role(cls.bright_star, name="Clerk", key="clerk")
        make_role_permission(
            clerk_role, make_permission("procurement.vendor.view"),
        )
        cls.clerk = make_school_admin(
            None, email="clerk@bright-star.test", tenant=cls.bright_star.tenant,
        )
        make_assignment(cls.bright_star, cls.clerk, clerk_role, branch=None)

        cls.platform = platform_tenant()
        platform_view_perm = make_permission(
            "platform.roles.view", scope=PermissionScope.PLATFORM,
        )
        cls.staff = _reader(
            cls.platform, "reader@codexng.test", platform_view_perm,
            key="platform_reader",
        )

    def _data(self, user=None, tenant=None, **params):
        response = _get(user or self.admin, tenant or self.bright_star.tenant, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    # Security

    def test_a_caller_without_a_role_view_key_is_refused(self):
        response = _get(self.clerk, self.bright_star.tenant)
        self.assertEqual(response.status_code, 403)

    def test_another_tenants_slug_is_refused_like_the_old_catalogue(self):
        new = _get(self.admin, self.bright_star.tenant, slug=self.greenfield.tenant.slug)
        old = _get(
            self.admin, self.bright_star.tenant, "rbac-tenant-permission-catalogue",
            slug=self.greenfield.tenant.slug,
        )
        self.assertEqual(new.status_code, 404)
        self.assertEqual(new.status_code, old.status_code)

    def test_a_platform_role_reader_can_read_a_school_catalogue(self):
        data = self._data(self.staff, self.bright_star.tenant)
        self.assertNotIn("platform", {node["module"] for node in data})
        field_keys = {
            field["key"] for node in data for row in node["resources"]
            for field in row["fields"]
        }
        self.assertNotIn("platform.staff_profile.account_number", field_keys)

    def test_a_school_sees_no_platform_field_and_no_platform_permission(self):
        data = self._data()
        self.assertNotIn("platform", {node["module"] for node in data})
        keys = {entry["key"] for entry in _flat_permissions(data)}
        self.assertNotIn("platform.impersonation.start_school", keys)
        field_keys = {
            field["key"] for node in data for row in node["resources"]
            for field in row["fields"]
        }
        self.assertNotIn("platform.staff_profile.account_number", field_keys)

    def test_the_platform_tenant_sees_both_scopes(self):
        data = self._data(self.staff, self.platform)
        keys = {entry["key"] for entry in _flat_permissions(data)}
        self.assertIn("platform.impersonation.start_school", keys)
        profile = _resource(data, "platform", "staff_profile")
        self.assertEqual(
            [field["key"] for field in profile["fields"]],
            ["platform.staff_profile.account_number"],
        )

    # Agreement with the older catalogue

    def _assert_flattened_matches_the_old_catalogue(self, user, tenant):
        new = self._data(user, tenant)
        response = _get(user, tenant, "rbac-tenant-permission-catalogue")
        self.assertEqual(response.status_code, 200, response.data)
        old = sorted(
            (entry for group in response.data["data"] for entry in group["permissions"]),
            key=lambda entry: entry["key"],
        )
        self.assertEqual(_flat_permissions(new), old)
        return old

    def test_flattened_it_is_the_old_catalogue_in_a_one_branch_school(self):
        old = self._assert_flattened_matches_the_old_catalogue(
            self.admin, self.bright_star.tenant,
        )
        self.assertIn(False, {entry["available"] for entry in old})

    def test_flattened_it_is_the_old_catalogue_in_a_multi_branch_school(self):
        self._assert_flattened_matches_the_old_catalogue(
            self.greenfield_admin, self.greenfield.tenant,
        )

    # Shape

    def test_the_tree_is_ordered_and_labelled(self):
        data = self._data()
        self.assertEqual(
            [node["module"] for node in data], sorted(node["module"] for node in data),
        )
        procurement = next(node for node in data if node["module"] == "procurement")
        self.assertEqual(procurement["label"], "Procurement")
        self.assertEqual(
            [row["label"] for row in procurement["resources"]], ["Vendor kyc", "Vendors"],
        )

        vendor = _resource(data, "procurement", "vendor")
        self.assertEqual(vendor["permissions"][0]["action"], "view")
        self.assertEqual(
            [field["name"] for field in vendor["fields"]],
            ["bank_account_number", "phone", "payment_terms", "kyc_score"],
        )

        finance = next(node for node in data if node["module"] == "finance")
        self.assertEqual(finance["label"], "Finance")
        self.assertEqual(_resource(data, "finance", "invoice")["label"], "Invoice")

    def test_field_entries_carry_their_defaults(self):
        vendor = _resource(self._data(), "procurement", "vendor")
        fields = {field["name"]: field for field in vendor["fields"]}
        self.assertEqual(fields["bank_account_number"], {
            "key": "procurement.vendor.bank_account_number",
            "name": "bank_account_number",
            "api_names": ["bank_account_number"],
            "label": "Bank account number",
            "group": "Banking",
            "description": "",
            "sensitive": True,
            "writable": True,
            "default": {"read": False, "write": False},
        })
        self.assertEqual(fields["payment_terms"]["default"], {"read": True, "write": True})
        self.assertEqual(fields["kyc_score"]["default"], {"read": True, "write": False})
        self.assertNotIn("retired", fields)

    def test_availability_follows_the_plan_and_a_field_only_resource_is_open(self):
        data = self._data()
        vendor = _resource(data, "procurement", "vendor")
        self.assertFalse(vendor["available"])
        kyc = _resource(data, "procurement", "vendor_kyc")
        self.assertEqual(kyc["permissions"], [])
        self.assertTrue(kyc["available"])
        self.assertTrue(_resource(data, "finance", "invoice")["available"])

    # Filters

    def test_module_narrows_the_tree(self):
        data = self._data(module="procurement")
        self.assertEqual([node["module"] for node in data], ["procurement"])

    def test_module_and_resource_narrow_to_one_resource(self):
        data = self._data(module="procurement", resource="vendor")
        self.assertEqual(len(data), 1)
        self.assertEqual([row["resource"] for row in data[0]["resources"]], ["vendor"])

    def test_resource_alone_matches_that_slug_in_any_module(self):
        data = self._data(resource="invoice")
        self.assertEqual([node["module"] for node in data], ["finance"])

    def test_a_search_match_returns_its_whole_resource(self):
        data = self._data(search="BANK ACCOUNT")
        self.assertEqual([node["module"] for node in data], ["procurement"])
        vendor = _resource(data, "procurement", "vendor")
        self.assertEqual(len(data[0]["resources"]), 1)
        self.assertIn("procurement.vendor.view", {p["key"] for p in vendor["permissions"]})
        self.assertEqual(len(vendor["fields"]), 4)

    def test_a_search_can_match_a_permission_label(self):
        data = self._data(search="invoice")
        self.assertEqual([node["module"] for node in data], ["finance"])

    def test_no_match_is_an_empty_list(self):
        for params in ({"search": "nothing matches this"}, {"module": "nosuchmodule"}):
            with self.subTest(params=params):
                data = self._data(**params)
                self.assertIsInstance(data, list)
                self.assertEqual(data, [])

    # Onboarding

    def test_a_pending_school_can_read_it(self):
        Tenant.objects.filter(pk=self.bright_star.tenant.pk).update(
            status=Tenant.Status.PENDING,
        )
        response = _get(self.admin, self.bright_star.tenant)
        self.assertEqual(response.status_code, 200, response.data)


class AccessCatalogueQueryCostTests(TestCase):
    """The query count does not grow with the size of the tree."""

    @classmethod
    def setUpTestData(cls):
        cls.view_perm = make_permission("school.roles.view")
        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        cls.admin = _reader(cls.school.tenant, "admin@bright-star.test", cls.view_perm)

    def _add_resources(self, count, start):
        for index in range(start, start + count):
            make_permission(f"zzscale.thing{index}.view")
            make_field_definition(f"zzscale.thing{index}.serial", f"Serial {index}")

    def _count(self):
        with CaptureQueriesContext(connection) as queries:
            response = _get(self.admin, self.school.tenant)
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries.captured_queries), response.data["data"]

    def test_ten_times_the_resources_costs_the_same_queries(self):
        self._add_resources(2, 0)
        _get(self.admin, self.school.tenant)
        small, small_data = self._count()
        self._add_resources(18, 2)
        large, large_data = self._count()
        self.assertEqual(len(_resource_rows(small_data)), 2)
        self.assertEqual(len(_resource_rows(large_data)), 20)
        self.assertEqual(large, small)


def _resource_rows(data):
    return [
        row for node in data if node["module"] == "zzscale" for row in node["resources"]
    ]


class TreeLabelsComeFromBackendDefinitionsTests(TestCase):
    """A backend-defined label is shown, while a blank one falls back."""

    @classmethod
    def setUpTestData(cls):
        view_perm = make_permission("school.roles.view")
        make_permission("finance.invoice.view")
        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        cls.admin = _reader(cls.school.tenant, "admin@bright-star.test", view_perm)
        cls.operator = make_vision_user(email="labels@codexng.test", super_admin=True)

    def test_a_stored_label_is_shown_and_a_blank_one_falls_back(self):
        resource = PermissionResource.objects.get(module_id="finance", name="invoice")
        PermissionModule.objects.filter(name="finance").update(label="Money")
        PermissionResource.objects.filter(pk=resource.pk).update(label="")

        self.assertEqual(PermissionModule.objects.get(name="finance").label, "Money")
        resource.refresh_from_db()
        self.assertEqual(resource.label, "")

        response = _get(self.admin, self.school.tenant)
        self.assertEqual(response.status_code, 200, response.data)
        finance = next(node for node in response.data["data"] if node["module"] == "finance")
        self.assertEqual(finance["label"], "Money")
        self.assertEqual(finance["resources"][0]["label"], "Invoice")


class RealFieldOnlyResourceTests(TestCase):
    """The guardians resource as the seeds and the field registry really build it.

    The synthetic case above proves the tree can carry a resource with fields
    and no permissions. This one proves the product has one: a school can turn
    a guardian's phone number off for a role without anybody minting a
    ``school.guardians.*`` key, and the screen has a readable name to draw.
    """

    @classmethod
    def setUpTestData(cls):
        # Every module seed, because the sync refuses the whole registry when
        # one declared module is missing.
        for command in (
            "seed_actions",
            "seed_prebuilt_role_templates",
            "seed_school_permissions",
            "seed_platform_permissions",
            "seed_import_permissions",
            "seed_finance_permissions",
            "seed_procurement_permissions",
            "seed_payments_permissions",
        ):
            call_command(command, verbosity=0, stdout=StringIO())
        call_command("sync_field_registry", stdout=StringIO())

        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        cls.admin = _reader(
            cls.school.tenant, "admin@bright-star.test",
            Permission.objects.get(key="school.roles.view"),
        )

    def _school_resource(self, name):
        response = _get(self.admin, self.school.tenant, module="school")
        self.assertEqual(response.status_code, 200, response.data)
        return _resource(response.data["data"], "school", name)

    def test_guardians_carries_its_fields_and_no_permissions(self):
        guardians = self._school_resource("guardians")
        self.assertIsNotNone(guardians)
        self.assertEqual(guardians["permissions"], [])
        self.assertTrue(guardians["available"])
        self.assertEqual(guardians["label"], "Guardians")
        self.assertEqual(
            {field["name"] for field in guardians["fields"]},
            {"phone", "email", "address", "occupation"},
        )

    def test_a_guardians_contact_detail_starts_readable_and_writable(self):
        """Nothing withholds it today, so no role loses it on release day."""
        guardians = self._school_resource("guardians")
        phone = next(f for f in guardians["fields"] if f["name"] == "phone")
        self.assertFalse(phone["sensitive"])
        self.assertEqual(phone["default"], {"read": True, "write": True})
        self.assertEqual(phone["group"], "Contact")

    def test_staff_personal_details_hang_off_the_staff_register(self):
        """The register, not the certificates: those are sold at another depth."""
        teachers = self._school_resource("teachers")
        self.assertEqual(teachers["label"], "Staff")
        self.assertEqual(
            {field["name"] for field in teachers["fields"]},
            {"date_of_birth", "gender", "phone", "email"},
        )
        self.assertTrue(teachers["permissions"])
        self.assertEqual(self._school_resource("staff_records")["fields"], [])
