"""A role's grants name each permission in words.

A school's role page lists what a role holds from the school's catalogue. A
grant the catalogue does not list (retired, or outside the school's plan) was
shown as its key with the dots and underscores swapped for spaces. Each grant
row carries the permission's readable wording, so the page never has to.
"""
from django.test import TestCase

from vs_rbac.models import TenantRolePermission
from vs_rbac.serializers.tenant import TenantRolePermissionSerializer

from .helpers import make_permission, make_role, make_role_permission, make_school


class RoleGrantLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="grant-labels", name="Lagoon View")
        cls.role = make_role(cls.school, name="Assistant Bursar", key="assistant-bursar")
        make_role_permission(cls.role, make_permission("finance.virtual_account.bulk_export"))

    def test_a_grant_row_carries_the_permissions_wording(self):
        row = TenantRolePermission.objects.select_related("permission__resource").get(role=self.role)
        data = TenantRolePermissionSerializer(row).data
        self.assertEqual(data["permission_key"], "finance.virtual_account.bulk_export")
        self.assertEqual(data["permission_label"], row.permission.readable_label)
        self.assertNotIn(".", data["permission_label"])
        self.assertNotIn("_", data["permission_label"])
