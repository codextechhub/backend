from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_import_data.models import (
    DatasetTypeChoices,
    ImportTemplate,
    TemplateStatusChoices,
)
from vs_rbac.models import Permission, TenantRolePermission, TenantRoleTemplate
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

    def test_master_seed_includes_required_bulk_templates(self):
        _call("seed_all_permissions")

        self.assertEqual(
            set(
                ImportTemplate.objects.filter(
                    status=TemplateStatusChoices.ACTIVE,
                    is_download_enabled=True,
                ).values_list("dataset_type", flat=True)
            ),
            {
                DatasetTypeChoices.SCHOOLS,
                DatasetTypeChoices.BRANCHES,
                DatasetTypeChoices.CX_USERS,
            },
        )

    def test_super_admin_gets_all_import_permissions_and_platform_admin_gets_templates(self):
        _call("seed_actions")
        _call("seed_import_permissions")

        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        super_admin = TenantRoleTemplate.objects.get(
            tenant=codex, key="xvs_super_admin"
        )
        platform_admin = TenantRoleTemplate.objects.get(
            tenant=codex, key="xvs_platform_admin"
        )
        all_import_keys = set(
            Permission.objects.filter(key__startswith="import.").values_list(
                "key", flat=True
            )
        )
        template_keys = {
            key for key in all_import_keys if key.startswith("import.templates.")
        }

        self.assertEqual(
            set(
                TenantRolePermission.objects.filter(
                    role=super_admin,
                    permission__key__startswith="import.",
                    granted=True,
                ).values_list("permission_id", flat=True)
            ),
            all_import_keys,
        )
        self.assertEqual(
            set(
                TenantRolePermission.objects.filter(
                    role=platform_admin,
                    permission__key__startswith="import.",
                    granted=True,
                ).values_list("permission_id", flat=True)
            ),
            template_keys,
        )

    def test_seed_removes_legacy_excess_platform_admin_import_grants(self):
        _call("seed_actions")
        _call("seed_import_permissions")
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        platform_admin = TenantRoleTemplate.objects.get(
            tenant=codex, key="xvs_platform_admin"
        )
        rollback_permission = Permission.objects.get(key="import.rollbacks.run")
        TenantRolePermission.objects.update_or_create(
            role=platform_admin,
            permission=rollback_permission,
            defaults={"granted": True},
        )

        _call("seed_import_permissions")

        self.assertFalse(
            TenantRolePermission.objects.filter(
                role=platform_admin,
                permission=rollback_permission,
                granted=True,
            ).exists()
        )
