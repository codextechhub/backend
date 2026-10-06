"""The permission registry names every module, resource, action and level in words.

The console's registry screens once fell back to a slug (``virtual_account``),
read an action as its slug with underscores swapped for spaces, and showed a
sensitivity level as the lower-cased enum member. Each row now carries the
words to show, and these tests hold it to words rather than code.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_config.tests_labels import HumanLabelAssertions
from vs_rbac.models import Permission

from .helpers import make_permission, make_vision_user


class RegistryReadableLabelsTests(HumanLabelAssertions, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_vision_user(super_admin=True)
        make_permission(
            "finance.virtual_account.bulk_export",
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _row(self, url_name, field, value):
        response = self.client.get(reverse(url_name), {"page_size": 500})
        self.assertEqual(response.status_code, 200, response.data)
        return next(row for row in response.data["data"] if row[field] == value)

    def test_a_module_with_no_stored_label_still_reads_as_words(self):
        row = self._row("rbac-permission-module-list-create", "name", "finance")
        self.assertHuman(row["readable_label"])

    def test_a_resource_with_no_stored_label_still_reads_as_words(self):
        row = self._row("rbac-permission-resource-list-create", "name", "virtual_account")
        self.assertEqual(row["readable_label"], "Virtual account")

    def test_an_action_reads_as_words(self):
        row = self._row("rbac-permission-action-list-create", "name", "bulk_export")
        self.assertEqual(row["readable_label"], "Bulk export")

    def test_a_permission_names_its_sensitivity_level(self):
        row = self._row(
            "rbac-permission-list-create", "key", "finance.virtual_account.bulk_export",
        )
        self.assertEqual(row["sensitivity_level"], "CRITICAL")
        self.assertEqual(row["sensitivity_label"], "Critical")
        for field in ("label", "module_label", "resource_label"):
            self.assertHuman(row[field], field)
