from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_import_data.models import (
    DatasetTypeChoices,
    ImportTemplate,
    TemplateStatusChoices,
)
from vs_rbac.models import TenantRolePermission, TenantRoleTemplate
from vs_tenants.models import Tenant


def _call(command, **options):
    call_command(command, stdout=StringIO(), stderr=StringIO(), **options)


class SeedImportConfigurationTests(TestCase):
    def test_school_and_branch_bulk_templates_are_seeded(self):
        _call("seed_import", dataset_type=DatasetTypeChoices.SCHOOLS)
        _call("seed_import", dataset_type=DatasetTypeChoices.BRANCHES)

        self.assertTrue(
            ImportTemplate.objects.filter(
                code="schools_master_v1",
                dataset_type=DatasetTypeChoices.SCHOOLS,
                status=TemplateStatusChoices.ACTIVE,
                is_download_enabled=True,
            ).exists()
        )
        self.assertTrue(
            ImportTemplate.objects.filter(
                code="branches_master_v1",
                dataset_type=DatasetTypeChoices.BRANCHES,
                status=TemplateStatusChoices.ACTIVE,
                is_download_enabled=True,
            ).exists()
        )

    def test_both_platform_admin_roles_receive_template_create_permission(self):
        _call("seed_actions")
        _call("seed_import_permissions")

        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        for role_key in ("xvs_super_admin", "xvs_platform_admin"):
            role = TenantRoleTemplate.objects.get(tenant=codex, key=role_key)
            self.assertTrue(
                TenantRolePermission.objects.filter(
                    role=role,
                    permission_id="import.templates.create",
                    granted=True,
                ).exists(),
                f"{role_key} should be able to create import templates.",
            )
