"""Backend ownership and inspection of permission definitions and defaults."""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.models import (
    PermissionGroup,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
)

from .helpers import make_dependency, make_permission, make_vision_user


class RegistryEndpointsAreReadOnlyTests(TestCase):
    """Every global definition is readable through the API and written in code."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_vision_user(super_admin=True)
        cls.permission = make_permission("finance.invoice.view")
        update_permission = make_permission("finance.invoice.update")
        cls.dependency = make_dependency(
            update_permission.key, cls.permission.key,
        )
        cls.group = PermissionGroup.objects.create(
            name="Invoice Work",
            description="Work with customer invoices.",
            scope=PermissionScope.TENANT,
            is_system=True,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_definition_collections_refuse_creation(self):
        urls = (
            reverse("rbac-permission-module-list-create"),
            reverse("rbac-permission-resource-list-create"),
            reverse("rbac-permission-action-list-create"),
            reverse("rbac-permission-list-create"),
            reverse("rbac-permission-dependency-list-create"),
            reverse("rbac-permission-group-list-create"),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.post(url, {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_definition_details_refuse_changes(self):
        urls = (
            reverse(
                "rbac-permission-module-detail",
                kwargs={"name": self.permission.module_id},
            ),
            reverse(
                "rbac-permission-resource-detail",
                kwargs={"pk": self.permission.resource_id},
            ),
            reverse(
                "rbac-permission-action-detail",
                kwargs={"name": self.permission.action_id},
            ),
            reverse(
                "rbac-permission-detail", kwargs={"key": self.permission.key},
            ),
            reverse(
                "rbac-permission-dependency-detail",
                kwargs={"id": self.dependency.id},
            ),
            reverse(
                "rbac-permission-group-detail", kwargs={"id": self.group.id},
            ),
        )

        for url in urls:
            for method in ("put", "patch", "delete"):
                with self.subTest(url=url, method=method):
                    response = getattr(self.client, method)(url, {}, format="json")
                    self.assertEqual(
                        response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED,
                    )

    def test_permission_list_is_ordered_and_backend_labelled(self):
        make_permission("academics.class.update", description="Update classes")
        make_permission("academics.class.view", description="View classes")
        finance_module = self.permission.module
        finance_module.label = "Finance and Accounting"
        finance_module.save(update_fields=["label"])
        self.permission.resource.label = "Customer Invoices"
        self.permission.resource.save(update_fields=["label"])

        response = self.client.get(reverse("rbac-permission-list-create"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["data"]
        self.assertEqual(
            [row["key"] for row in rows],
            [
                "academics.class.update",
                "academics.class.view",
                "finance.invoice.update",
                "finance.invoice.view",
            ],
        )
        finance_row = next(
            row for row in rows if row["key"] == self.permission.key
        )
        self.assertEqual(finance_row["label"], "View invoice")
        self.assertEqual(finance_row["module_label"], "Finance and Accounting")
        self.assertEqual(finance_row["resource_label"], "Customer Invoices")

    def test_resource_list_uses_the_backend_module_label(self):
        module = self.permission.module
        module.label = "Finance and Accounting"
        module.save(update_fields=["label"])

        response = self.client.get(reverse("rbac-permission-resource-list-create"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice = next(
            row for row in response.data["data"] if row["name"] == "invoice"
        )
        self.assertEqual(invoice["module_label"], "Finance and Accounting")


class AccessRegistryOverviewTests(TestCase):
    def test_json_overview_groups_permissions_and_includes_defaults(self):
        permission = make_permission("finance.invoice.view")
        permission.description = "View customer invoices"
        permission.save(update_fields=["description"])
        retired = make_permission("finance.invoice.delete")
        retired.is_active = False
        retired.save(update_fields=["is_active"])
        group = PermissionGroup.objects.create(
            name="Invoice Readers",
            scope=PermissionScope.TENANT,
            is_system=True,
        )
        group.permissions.add(permission)
        group.permissions.add(retired)
        role = PrebuiltRoleTemplate.objects.create(
            key="finance_reader",
            name="Finance Reader",
            scope="institution",
            tier="B",
        )
        PrebuiltRolePermission.objects.create(
            prebuilt_role=role,
            permission=permission,
        )
        PrebuiltRolePermission.objects.create(
            prebuilt_role=role,
            permission=retired,
        )

        stdout = StringIO()
        call_command("show_access_registry", "--json", stdout=stdout)
        catalogue = json.loads(stdout.getvalue())

        finance = next(
            module for module in catalogue["modules"]
            if module["key"] == "finance"
        )
        invoice = next(
            resource for resource in finance["resources"]
            if resource["key"] == "invoice"
        )
        self.assertEqual(invoice["permissions"][0]["key"], permission.key)
        self.assertEqual(
            invoice["permissions"][0]["label"], "View customer invoices",
        )
        self.assertEqual(catalogue["default_groups"][0]["name"], "Invoice Readers")
        self.assertEqual(
            catalogue["default_groups"][0]["permissions"], [permission.key],
        )
        role_row = next(
            row for row in catalogue["default_roles"]
            if row["key"] == "finance_reader"
        )
        self.assertEqual(role_row["permissions"], [permission.key])

        stdout = StringIO()
        call_command(
            "show_access_registry", "--json", "--include-inactive", stdout=stdout,
        )
        catalogue_with_inactive = json.loads(stdout.getvalue())
        group_row = next(
            row for row in catalogue_with_inactive["default_groups"]
            if row["name"] == "Invoice Readers"
        )
        self.assertEqual(
            group_row["permissions"], [retired.key, permission.key],
        )
