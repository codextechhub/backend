"""Backend ownership and inspection of permission definitions and defaults."""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from vs_user.tokens import CodeXRefreshToken

from vs_rbac.models import (
    PermissionGroup,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    RBACAuditLog,
)

from .helpers import (
    make_assignment,
    make_dependency,
    make_permission,
    make_role,
    make_role_permission,
    make_vision_user,
)


class RegistryEndpointsAreReadOnlyTests(TestCase):
    """Every permission definition is readable through the API and written in code."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_vision_user(super_admin=True)
        cls.permission = make_permission("finance.invoice.view")
        update_permission = make_permission("finance.invoice.update")
        cls.dependency = make_dependency(
            update_permission.key, cls.permission.key,
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


class AdministratorPermissionGroupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = make_vision_user(email="group-manager@codex.test")
        cls.viewer = make_vision_user(email="group-viewer@codex.test")
        write_permissions = [
            make_permission(
                f"platform.permission_groups.{action}",
                scope=PermissionScope.PLATFORM,
            )
            for action in ("create", "update", "delete")
        ]
        make_permission(
            "platform.permissions.view",
            scope=PermissionScope.PLATFORM,
        )
        role = make_role(cls.manager.tenant, name="Permission Group Manager")
        for permission in write_permissions:
            make_role_permission(role, permission)
        make_role_permission(
            role, make_permission(
                "platform.permissions.view",
                scope=PermissionScope.PLATFORM,
            ),
        )
        make_assignment(cls.manager.tenant, cls.manager, role)
        viewer_role = make_role(cls.viewer.tenant, name="Permission Group Viewer")
        make_role_permission(
            viewer_role, make_permission(
                "platform.permissions.view",
                scope=PermissionScope.PLATFORM,
            ),
        )
        make_assignment(cls.viewer.tenant, cls.viewer, viewer_role)
        cls.view_permission = make_permission("finance.invoice.view")
        cls.update_permission = make_permission("finance.invoice.update")
        make_dependency(cls.update_permission.key, cls.view_permission.key)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.manager)

    def test_an_administrator_can_create_update_and_delete_a_group(self):
        list_url = reverse("rbac-permission-group-list-create")
        created = self.client.post(
            list_url,
            {
                "name": "Invoice Editors",
                "description": "Read and edit customer invoices.",
                "permission_keys": [
                    self.view_permission.key,
                    self.update_permission.key,
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        group = PermissionGroup.objects.get(name="Invoice Editors")
        self.assertFalse(group.is_system)
        self.assertEqual(group.scope, PermissionScope.TENANT)
        self.assertEqual(
            set(group.permissions.values_list("key", flat=True)),
            {self.view_permission.key, self.update_permission.key},
        )
        self.assertTrue(
            RBACAuditLog.objects.filter(
                action_type="CREATE",
                entity_type="permission_group",
                actor=self.manager,
                entity_id=str(group.pk),
            ).exists(),
        )

        detail_url = reverse(
            "rbac-permission-group-detail", kwargs={"id": group.pk},
        )
        updated = self.client.patch(
            detail_url,
            {
                "name": "Invoice Readers",
                "permission_keys": [self.view_permission.key],
            },
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK, updated.data)
        group.refresh_from_db()
        self.assertEqual(group.name, "Invoice Readers")
        self.assertEqual(
            list(group.permissions.values_list("key", flat=True)),
            [self.view_permission.key],
        )

        deleted = self.client.delete(detail_url)
        self.assertEqual(deleted.status_code, status.HTTP_200_OK, deleted.data)
        self.assertFalse(PermissionGroup.objects.filter(pk=group.pk).exists())
        self.assertTrue(
            RBACAuditLog.objects.filter(
                action_type="DELETE",
                entity_type="permission_group",
                actor=self.manager,
                entity_id=str(group.pk),
            ).exists(),
        )

    def test_a_group_must_include_permission_dependencies(self):
        response = self.client.post(
            reverse("rbac-permission-group-list-create"),
            {
                "name": "Broken Invoice Editors",
                "permission_keys": [self.update_permission.key],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.view_permission.key, str(response.data))

    def test_catalogue_reader_cannot_create_update_or_delete_a_group(self):
        group = PermissionGroup.objects.create(
            name="Existing Group",
            scope=PermissionScope.TENANT,
        )
        self.client.force_authenticate(user=self.viewer)

        listed = self.client.get(reverse("rbac-permission-group-list-create"))
        catalogue_client = APIClient()
        token = str(CodeXRefreshToken.for_user(self.viewer).access_token)
        catalogue_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        catalogue = catalogue_client.get(
            reverse(
                "rbac-tenant-access-catalogue",
                kwargs={"tenant_slug": self.viewer.tenant.slug},
            ),
            {"tenant": self.viewer.tenant.slug},
        )
        created = self.client.post(
            reverse("rbac-permission-group-list-create"),
            {"name": "Forbidden Group"},
            format="json",
        )
        detail_url = reverse(
            "rbac-permission-group-detail", kwargs={"id": group.pk},
        )
        updated = self.client.patch(
            detail_url, {"name": "Forbidden Rename"}, format="json",
        )
        deleted = self.client.delete(detail_url)

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(catalogue.status_code, status.HTTP_200_OK)
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(updated.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(deleted.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(PermissionGroup.objects.filter(pk=group.pk).exists())

    def test_backend_created_legacy_groups_are_hidden(self):
        PermissionGroup.objects.create(
            name="Legacy Backend Group",
            scope=PermissionScope.TENANT,
            is_system=True,
        )

        response = self.client.get(reverse("rbac-permission-group-list-create"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"])


class AccessRegistryOverviewTests(TestCase):
    def test_json_overview_groups_permissions_and_includes_default_roles(self):
        permission = make_permission("finance.invoice.view")
        permission.description = "View customer invoices"
        permission.save(update_fields=["description"])
        retired = make_permission("finance.invoice.delete")
        retired.is_active = False
        retired.save(update_fields=["is_active"])
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
        self.assertNotIn("default_groups", catalogue)
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
        role_row = next(
            row for row in catalogue_with_inactive["default_roles"]
            if row["key"] == "finance_reader"
        )
        self.assertEqual(role_row["permissions"], [retired.key, permission.key])
