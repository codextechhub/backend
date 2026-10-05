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


class StatusReasonReadDefaultTests(TestCase):
    """The roles that run admissions and records read a status reason by default.

    ``school.students.status_reason`` is sensitive, so a role reads it only
    where a switch says so. School Admin and Branch Admin are the library roles
    holding the keys that move a pupil or an applicant (``.update``,
    ``.transition``, ``.transfer``, ``.suspend``, ``.reactivate``), so the field
    declares Read for them (``FieldSpec.read_by``) and the sync gives it to the
    library roles and, once, to every school's existing copy of them.

    Bright Star (two branches) and Sunrise (one) had their roles before the
    default existed, so they are provisioned before the full seed runs.
    """

    FIELD = "school.students.status_reason"

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_rbac.tests.helpers import (
            make_assignment,
            make_branch,
            make_school,
            make_school_admin,
        )

        for command in ("seed_actions", "seed_prebuilt_role_templates",
                        "seed_school_permissions"):
            call_command(command, verbosity=0, stdout=StringIO(), stderr=StringIO())

        bright_school = make_school(slug="reason-bright", name="Bright Star")
        cls.bright = bright_school.tenant
        lekki = make_branch(cls.bright, name="Lekki", is_main=True)
        ikeja = make_branch(cls.bright, name="Ikeja", is_main=False)
        sunrise = make_school(slug="reason-sunrise", name="Sunrise").tenant
        sunrise_main = make_branch(sunrise, name="Sunrise Main", is_main=True)

        def provision(tenant, key, branch=None):
            return provision_role_from_prebuilt(
                tenant=tenant, branch=branch, prebuilt_key=key,
            )

        cls.bright_admin = provision(cls.bright, "school_admin")
        cls.lekki_admin = provision(cls.bright, "branch_admin", lekki)
        cls.ikeja_admin = provision(cls.bright, "branch_admin", ikeja)
        cls.lekki_teacher = provision(cls.bright, "teacher", lekki)
        cls.sunrise_admin = provision(sunrise, "school_admin")
        cls.sunrise_branch_admin = provision(sunrise, "branch_admin", sunrise_main)
        cls.sunrise_teacher = provision(sunrise, "teacher", sunrise_main)

        cls.principal = make_school_admin(
            None, email="principal@reason-bright.test", tenant=cls.bright,
        )
        make_assignment(bright_school, cls.principal, cls.bright_admin, branch=None)
        cls.teacher_user = make_school_admin(
            None, email="teacher@reason-bright.test", tenant=cls.bright,
        )
        make_assignment(bright_school, cls.teacher_user, cls.lekki_teacher)

        call_command("seed_all_permissions", verbosity=0, stdout=StringIO())

    def reads(self, role):
        from vs_rbac.models import RoleFieldAccess

        return RoleFieldAccess.objects.filter(
            role=role, field_id=self.FIELD, can_read=True,
        ).exists()

    def test_every_existing_admin_copy_gains_read_and_no_teacher_does(self):
        for role in (self.bright_admin, self.lekki_admin, self.ikeja_admin,
                     self.sunrise_admin, self.sunrise_branch_admin):
            with self.subTest(role=role.key, tenant=role.tenant_id):
                self.assertTrue(self.reads(role))
        for role in (self.lekki_teacher, self.sunrise_teacher):
            with self.subTest(role=role.key, tenant=role.tenant_id):
                self.assertFalse(self.reads(role))

    def test_the_school_admin_reads_the_reason_and_the_teacher_does_not(self):
        from vs_rbac.field_evaluator import get_field_access

        self.assertTrue(
            get_field_access(self.principal, tenant=self.bright).can_read(self.FIELD),
        )
        self.assertFalse(
            get_field_access(self.teacher_user, tenant=self.bright).can_read(self.FIELD),
        )

    def test_a_school_that_turned_it_off_keeps_it_off(self):
        """Bright Star turns Read off for its School Admin, and Sunrise resets
        its Branch Admin to the field's default (closed). The next deploy's
        seeds leave both closed."""
        from vs_rbac.models import RoleFieldAccess

        RoleFieldAccess.objects.filter(
            role=self.bright_admin, field_id=self.FIELD,
        ).update(can_read=False, can_write=False)
        RoleFieldAccess.objects.filter(
            role=self.sunrise_branch_admin, field_id=self.FIELD,
        ).delete()

        call_command("seed_all_permissions", verbosity=0, stdout=StringIO())

        self.assertFalse(self.reads(self.bright_admin))
        self.assertFalse(self.reads(self.sunrise_branch_admin))
        self.assertTrue(self.reads(self.lekki_admin))

    def test_a_school_opened_afterwards_gets_it_when_its_roles_are_made(self):
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_rbac.tests.helpers import make_branch, make_school

        later = make_school(slug="reason-later", name="Greenfield").tenant
        main = make_branch(later, name="Greenfield Main", is_main=True)
        admin = provision_role_from_prebuilt(tenant=later, prebuilt_key="school_admin")
        teacher = provision_role_from_prebuilt(
            tenant=later, branch=main, prebuilt_key="teacher",
        )
        self.assertTrue(self.reads(admin))
        self.assertFalse(self.reads(teacher))
