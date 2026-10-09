"""Tenant-kind ownership for export datasets."""
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from vs_exports.catalogue import ALL_TENANT_KINDS, ScopeContext, all_datasets, get_dataset
from vs_exports.engine import may_export_dataset
from vs_exports.tests import _ExportFixture
from vs_tenants.models import Tenant


class DatasetKindDeclarationTests(SimpleTestCase):
    def test_product_specific_datasets_declare_their_console(self):
        school_only = {
            "school.students",
            "academics.sessions",
            "academics.departments",
            "academics.programs",
            "academics.levels",
            "academics.classes",
            "academics.subjects",
        }
        platform_only = {
            "platform.schools",
            "admin.school_users",
            "admin.org_units",
            "admin.positions",
            "admin.matrix_reports",
        }
        by_key = {dataset.key: dataset for dataset in all_datasets()}
        self.assertEqual(
            {key for key, dataset in by_key.items() if dataset.tenant_kinds == ("SCHOOL",)},
            school_only,
        )
        self.assertEqual(
            {key for key, dataset in by_key.items() if dataset.tenant_kinds == ("PLATFORM",)},
            platform_only,
        )
        for key, dataset in by_key.items():
            if key not in school_only | platform_only:
                self.assertEqual(dataset.tenant_kinds, ALL_TENANT_KINDS, key)

    @patch("vs_exports.engine._holds", return_value=True)
    def test_kind_boundary_cannot_be_bypassed_by_rbac(self, _holds):
        platform = type("Tenant", (), {"kind": "PLATFORM"})()
        tenant = type("Tenant", (), {"kind": "SCHOOL"})()
        self.assertFalse(
            may_export_dataset(object(), get_dataset("school.students"), platform)
        )
        self.assertFalse(
            may_export_dataset(object(), get_dataset("admin.positions"), tenant)
        )


class DatasetKindAPITests(_ExportFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_platform_permissions", verbosity=0)
        call_command("seed_school_permissions", verbosity=0)
        cls.customer_tenant = Tenant.objects.create(
            name="Bright Star", slug="bright-star-export-kind",
            kind=Tenant.Kind.SCHOOL, status=Tenant.Status.ACTIVE,
        )
        cls.customer_user = cls._user(
            "owner@bright-star.test", role="export_owner",
            tenant=cls.customer_tenant,
            keys=[
                "exports.catalogue.view", "exports.run.create",
                "platform.team.view", "school.students.export",
            ],
        )

    def test_platform_caller_cannot_see_or_run_customer_only_dataset(self):
        client = TenantAPIClient(user=self.admin)
        catalogue = client.get("/v1/exports/catalogue/")
        keys = {
            row["id"]
            for module in catalogue.data["data"]["modules"]
            for row in module["datasets"]
        }
        self.assertNotIn("school.students", keys)
        self.assertTrue(all(module["datasets"] for module in catalogue.data["data"]["modules"]))
        self.assertEqual(
            client.get("/v1/exports/catalogue/school.students/").status_code, 404,
        )
        response = client.post("/v1/exports/quick/", {
            "dataset_key": "school.students", "columns": ["id"],
            "filters": [], "sort": [], "format": "csv", "values_mode": "system",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_customer_caller_cannot_see_or_run_platform_only_dataset(self):
        client = TenantAPIClient(user=self.customer_user)
        catalogue = client.get("/v1/exports/catalogue/")
        keys = {
            row["id"]
            for module in catalogue.data["data"]["modules"]
            for row in module["datasets"]
        }
        self.assertNotIn("admin.positions", keys)
        self.assertTrue(all(module["datasets"] for module in catalogue.data["data"]["modules"]))
        self.assertEqual(
            client.get("/v1/exports/catalogue/admin.positions/").status_code, 404,
        )
        response = client.post("/v1/exports/quick/", {
            "dataset_key": "admin.positions", "columns": ["code"],
            "filters": [], "sort": [], "format": "csv", "values_mode": "system",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_admin_users_never_reads_another_tenant(self):
        rows = get_dataset("admin.users").base(ScopeContext(tenant=self.tenant))
        self.assertFalse(rows.exclude(tenant=self.tenant).exists())
        self.assertFalse(rows.filter(tenant=self.other_tenant).exists())
