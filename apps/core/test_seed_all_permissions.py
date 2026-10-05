from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.seed_all_permissions import Command
from vs_rbac.models import (
    RBACAuditLog,
    TenantRolePermission,
    TenantRoleTemplate,
)
from vs_rbac.tests.helpers import make_permission
from vs_tenants.models import Tenant


class SuperAdminPermissionReconciliationTests(TestCase):
    def test_super_admin_gets_every_active_permission_without_expanding_platform_admin(self):
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        super_admin, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=codex,
            key="xvs_super_admin",
            defaults={"name": "XVS Super Admin", "is_system_role": True},
        )
        platform_admin, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=codex,
            key="xvs_platform_admin",
            defaults={"name": "XVS Platform Admin", "is_system_role": True},
        )
        first = make_permission("new_module.first.generate")
        second = make_permission("new_module.second.update")
        inactive = make_permission(
            "new_module.retired.update",
            is_active=False,
        )
        TenantRolePermission.objects.create(
            role=super_admin,
            permission=first,
            granted=False,
        )
        TenantRolePermission.objects.create(
            role=super_admin,
            permission=inactive,
            granted=False,
        )

        Command()._ensure_super_admin_has_every_permission()

        self.assertEqual(
            TenantRolePermission.objects.filter(
                role=super_admin,
                permission__in=(first, second),
                granted=True,
            ).count(),
            2,
        )
        self.assertFalse(
            TenantRolePermission.objects.filter(
                role=platform_admin,
                permission__in=(first, second),
                granted=True,
            ).exists()
        )
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=super_admin,
                permission=inactive,
                granted=False,
            ).exists()
        )
        log = RBACAuditLog.objects.filter(
            entity_type="TenantRoleTemplate",
            entity_id=str(super_admin.pk),
        ).latest("created_at")
        self.assertEqual(
            log.metadata["source"],
            "permission_seed_reconciliation",
        )
        self.assertIn(first.key, log.diff_data["direct_permission_keys"]["after"])
        self.assertIn(inactive.key, log.diff_data["denied_permission_keys"]["after"])


class SchoolRoleDecisionsSurviveEverySeedTests(TestCase):
    """No module seed puts back a default a school took off its own role.

    Seven seeds attach defaults to the School Admin, Branch Admin and Teacher
    library roles: school and academics, communication, tickets, the Export
    Centre, onboarding, imports and workflow. Each offers a default to the
    schools' copies once, when the library role gains it, so the next full run
    leaves every removal standing.

    Bright Star takes one key from each seed off its roles. None of the keys is
    a ``view`` key, because a view key a role still needs for a write it holds
    is restored by ``seed_permission_dependencies`` on purpose.
    """

    #: ``(role attribute, key)``: one removal per seed, across all three roles.
    TAKEN_OFF = (
        ("admin", "school.students.import"),
        ("admin", "communication.message_activity.audit"),
        ("admin", "tickets.ticket.escalate"),
        ("admin", "exports.schedule.suspend"),
        ("branch_admin", "exports.run.cancel"),
        ("admin", "onboarding.go_live.submit"),
        ("admin", "import.batches.run"),
        ("admin", "workflow.instance.cancel"),
        ("teacher", "tickets.attachment.create"),
    )

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_rbac.tests.helpers import make_branch, make_school

        call_command("seed_all_permissions", verbosity=0, stdout=StringIO())
        bright = make_school(slug="decisions-bright", name="Bright Star").tenant
        lekki = make_branch(bright, name="Lekki", is_main=True)
        make_branch(bright, name="Ikeja", is_main=False)
        cls.admin = provision_role_from_prebuilt(tenant=bright, prebuilt_key="school_admin")
        cls.branch_admin = provision_role_from_prebuilt(
            tenant=bright, branch=lekki, prebuilt_key="branch_admin",
        )
        cls.teacher = provision_role_from_prebuilt(
            tenant=bright, branch=lekki, prebuilt_key="teacher",
        )

    def test_a_full_run_leaves_every_removal_standing(self):
        for attribute, key in self.TAKEN_OFF:
            role = getattr(self, attribute)
            self.assertTrue(
                TenantRolePermission.objects.filter(role=role, permission_id=key).exists(),
                f"{role.key} was provisioned without {key}",
            )
            TenantRolePermission.objects.filter(role=role, permission_id=key).delete()

        call_command("seed_all_permissions", verbosity=0, stdout=StringIO())

        put_back = [
            f"{getattr(self, attribute).key}: {key}"
            for attribute, key in self.TAKEN_OFF
            if TenantRolePermission.objects.filter(
                role=getattr(self, attribute), permission_id=key,
            ).exists()
        ]
        self.assertEqual(put_back, [])
