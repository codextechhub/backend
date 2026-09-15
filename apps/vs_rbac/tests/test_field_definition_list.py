"""The platform's read-only view of the field registry.

Code owns the registry, so the endpoint lists and filters and never writes.
Only a caller holding ``platform.permissions.view`` may read it.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import PermissionScope
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
)

URL = "rbac-field-definition-list"


class FieldDefinitionListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.platform = platform_tenant()
        registry_view = make_permission("platform.permissions.view")
        reader_role = make_platform_role(name="Registry Reader", key="registry_reader")
        make_platform_role_permission(reader_role, registry_view)
        cls.viewer = make_vision_user(email="reader@codexng.test")
        make_platform_assignment(cls.viewer, reader_role)

        cls.outsider = make_vision_user(email="outsider@codexng.test")

        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        school_role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(school_role, make_permission("school.roles.view"))
        cls.school_admin = make_school_admin(
            None, email="admin@bright-star.test", tenant=cls.school.tenant,
        )
        make_assignment(cls.school, cls.school_admin, school_role, branch=None)

        make_permission("procurement.vendor.view")
        make_permission("school.students.view")
        make_field_definition(
            "procurement.vendor.bank_account_number", "Bank account number",
            group="Banking", sensitive=True,
        )
        make_field_definition(
            "procurement.vendor.payment_terms", "Payment terms", group="Terms",
        )
        make_field_definition(
            "procurement.vendor.retired", "Retired field", is_active=False,
        )
        make_field_definition(
            "school.students.allergies", "Allergies", group="Medical", sensitive=True,
            description="What a pupil reacts to.",
        )
        make_field_definition(
            "platform.staff_profile.account_number", "Account number",
            sensitive=True, scope=PermissionScope.PLATFORM,
        )

    def _get(self, user, tenant, **params):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client.get(reverse(URL), {"tenant": tenant.slug, **params})

    def _keys(self, **params):
        response = self._get(self.viewer, self.platform, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["key"] for row in response.data["data"]}

    def test_a_platform_user_without_the_registry_key_is_refused(self):
        response = self._get(self.outsider, self.platform)
        self.assertEqual(response.status_code, 403)

    def test_a_school_administrator_is_refused(self):
        response = self._get(self.school_admin, self.school.tenant)
        self.assertEqual(response.status_code, 403)

    def test_it_lists_every_field_with_its_place_and_default(self):
        response = self._get(self.viewer, self.platform)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["pagination"]["totalItems"], 5)
        row = next(
            row for row in response.data["data"]
            if row["key"] == "procurement.vendor.bank_account_number"
        )
        self.assertEqual(row["module"], "procurement")
        self.assertEqual(row["module_label"], "Procurement")
        self.assertEqual(row["resource"], "vendor")
        self.assertEqual(row["resource_label"], "Vendor")
        self.assertEqual(row["api_names"], ["bank_account_number"])
        self.assertEqual(row["scope"], PermissionScope.TENANT)
        self.assertEqual(row["default"], {"read": False, "write": False})

    def test_filters(self):
        self.assertEqual(
            self._keys(module="school"), {"school.students.allergies"},
        )
        self.assertEqual(
            self._keys(module="procurement", resource="vendor"),
            {
                "procurement.vendor.bank_account_number",
                "procurement.vendor.payment_terms",
                "procurement.vendor.retired",
            },
        )
        self.assertEqual(
            self._keys(sensitive="false"),
            {"procurement.vendor.payment_terms", "procurement.vendor.retired"},
        )
        self.assertEqual(self._keys(is_active="false"), {"procurement.vendor.retired"})
        self.assertEqual(self._keys(search="reacts"), {"school.students.allergies"})
        self.assertEqual(self._keys(search="nothing matches"), set())

    def test_no_write_verb_is_offered(self):
        token = str(CodeXRefreshToken.for_user(self.viewer).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.post(
            f"{reverse(URL)}?tenant={self.platform.slug}",
            {"key": "procurement.vendor.email", "label": "Email"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)
