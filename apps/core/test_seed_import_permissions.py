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
        """Every template a deploy is supposed to leave behind, and no others.

        ``calendar_events`` and ``students`` are the two on this list a SCHOOL
        can use. If either stops being seeded, the school import screen goes
        back to showing an empty table and nothing else in the suite would
        notice.
        """
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
                DatasetTypeChoices.BANK_STATEMENTS,
                DatasetTypeChoices.CALENDAR_EVENTS,
                DatasetTypeChoices.STUDENTS,
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


class SeedImportPermissionGroupScopeTests(TestCase):
    """The import bundles must be usable by the tenants they are built for.

    ``PermissionGroup.scope`` has no default, deliberately, so every creation
    path has to declare it. Migration 0007 classified the groups that already
    existed; this seeder creates them, so a fresh database seeded after that
    migration produced three unclassified bundles that ``TenantRoleGroup``
    refuses to attach to any role inside a tenant. Nobody noticed because the
    dev database had been classified by the migration.
    """

    def test_seeded_groups_are_tenant_scoped(self):
        from vs_rbac.models import PermissionGroup, PermissionScope

        _call("seed_actions")
        _call("seed_import_permissions")

        groups = PermissionGroup.objects.filter(
            name__in=["Data Import - all", "Import Batch - all", "Import Template - all"],
        )
        self.assertEqual(groups.count(), 3)
        for group in groups:
            self.assertEqual(group.scope, PermissionScope.TENANT, group.name)

    def test_a_seeded_group_can_be_attached_to_a_school_role(self):
        from vs_rbac.models import PermissionGroup, TenantRoleGroup
        from vs_rbac.tests.helpers import make_school

        _call("seed_actions")
        _call("seed_import_permissions")

        school = make_school(slug="bright-star", name="Bright Star School")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="data-officer", name="Data Officer",
        )
        group = PermissionGroup.objects.get(name="Import Batch - all")

        link = TenantRoleGroup(role=role, group=group)
        link.full_clean()
        link.save()

        self.assertTrue(
            TenantRoleGroup.objects.filter(role=role, group=group).exists(),
        )
