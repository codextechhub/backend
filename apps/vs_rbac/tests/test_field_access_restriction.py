"""Changing field access switches is a restricted power; seeing them is not.

A switch opens a field the moment it is saved, with nobody approving it. So
``school.field_access.manage`` must not be something a person can give
themselves. At Bright Star School, Mr Okafor holds Deputy Head, which carries
``school.roles.update``. If manage were unrestricted he could add it to Deputy
Head, save, and turn on Read for a supplier's bank account number on his own
role. Restricted, the first step goes to the role change ladder, and the detour
through a role he does not hold is closed by the assignment ceiling.

``school.field_access.view`` only shows switches, so it stays groupable.

Each rule is checked in a single-branch school and a multi-branch one, because
Mr Okafor's Deputy Head grant pinned to one branch is still a role he holds.
School Admin, provisioned or backfilled, must still end up with manage: that is
the trusted bootstrap path for restricted keys.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from core.management.commands.seed_school_permission_groups import (
    SCHOOL_PERMISSION_GROUPS,
)
from vs_rbac.models import (
    GroupPermission,
    Permission,
    PermissionGroup,
    RoleFieldAccess,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.services import provision_role_from_prebuilt
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

VIEW = "school.field_access.view"
MANAGE = "school.field_access.manage"
ROLE_KEYS = [
    "school.roles.view",
    "school.roles.create",
    "school.roles.update",
    "school.roles.delete",
    "school.roles.assign",
]
ROLES_GROUP = "School Roles and Permissions"


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _q(url, slug):
    return f"{url}?tenant={slug}"


def _granted(role):
    return set(
        TenantRolePermission.objects.filter(role=role, granted=True)
        .values_list("permission_id", flat=True)
    )


def _call(command):
    call_command(command, stdout=StringIO(), stderr=StringIO())


class _SchoolShape:
    """Builds Bright Star with one branch, or with Ikeja and Lekki."""

    multi_branch = False

    def _school(self, slug):
        school = make_school(slug=slug, name="Bright Star School")
        self.ikeja = make_branch(school, name="Ikeja Branch")
        self.lekki = (
            make_branch(school, name="Lekki Branch", is_main=False)
            if self.multi_branch else None
        )
        return school


class _RestrictionRules(_SchoolShape):
    def setUp(self):
        shape = "multi" if self.multi_branch else "single"
        self.school = self._school(f"far-{shape}")
        self.tenant = self.school.tenant
        self.slug = self.tenant.slug
        make_permission(MANAGE, is_restricted=True, sensitivity_level="CRITICAL")
        make_permission(VIEW, sensitivity_level="CRITICAL")

        self.okafor = make_school_admin(self.ikeja, email=f"far-okafor-{shape}@test.com")
        self.deputy_head = make_role(self.tenant, name="Deputy Head", key="deputy-head")
        for key in ROLE_KEYS:
            make_role_permission(self.deputy_head, make_permission(key))
        # In the multi-branch school his grant is pinned to Lekki.
        make_assignment(self.tenant, self.okafor, self.deputy_head, branch=self.lekki)

        self.storekeeper = make_role(self.tenant, name="Storekeeper", key="storekeeper")
        make_role_permission(self.storekeeper, make_permission(VIEW))

    def _save_role(self, role, keys):
        url = reverse("rbac-role-detail", kwargs={"tenant_slug": self.slug, "key": role.key})
        return _client(self.okafor).patch(
            _q(url, self.slug),
            {"permission_keys": keys, "reason": "Field access cover."},
            format="json",
        )

    def test_adding_manage_to_a_role_you_hold_needs_approval(self):
        response = self._save_role(self.deputy_head, ROLE_KEYS + [MANAGE])
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["error"]["code"], "RESTRICTED_NEEDS_APPROVAL")
        self.assertNotIn(MANAGE, _granted(self.deputy_head))

    def test_adding_view_to_a_role_you_hold_saves(self):
        response = self._save_role(self.deputy_head, ROLE_KEYS + [VIEW])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(VIEW, _granted(self.deputy_head))

    def test_adding_manage_to_a_role_you_do_not_hold_saves(self):
        response = self._save_role(self.storekeeper, [VIEW, MANAGE])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(MANAGE, _granted(self.storekeeper))

    def test_giving_yourself_that_role_is_refused_by_the_ceiling(self):
        self.assertEqual(
            self._save_role(self.storekeeper, [VIEW, MANAGE]).status_code,
            status.HTTP_200_OK,
        )
        url = reverse("rbac-assignment-list-create", kwargs={"tenant_slug": self.slug})
        response = _client(self.okafor).post(
            _q(url, self.slug),
            {"user": self.okafor.pk, "role": self.storekeeper.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertIn("grant authority", str(response.data["message"]))
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user=self.okafor, role=self.storekeeper,
            ).exists()
        )


class SingleBranchRestrictionTests(_RestrictionRules, TestCase):
    multi_branch = False


class MultiBranchRestrictionTests(_RestrictionRules, TestCase):
    multi_branch = True


class _SeededSchoolAdmin(_SchoolShape):
    """The real seeds, and the School Admin they provision or backfill."""

    @classmethod
    def setUpTestData(cls):
        for command in (
            "seed_actions",
            "seed_prebuilt_role_templates",
            "seed_school_permissions",
            "seed_school_permission_groups",
        ):
            _call(command)

    def _slug(self, what):
        return f"far-{what}-{'multi' if self.multi_branch else 'single'}"

    def test_the_seed_registers_view_open_and_manage_restricted(self):
        self.assertFalse(Permission.objects.get(key=VIEW).is_restricted)
        self.assertTrue(Permission.objects.get(key=MANAGE).is_restricted)

    def test_manage_is_in_no_group_and_view_is_in_the_roles_group(self):
        self.assertFalse(GroupPermission.objects.filter(permission_id=MANAGE).exists())
        for _name, _reach, _description, keys in SCHOOL_PERMISSION_GROUPS:
            self.assertNotIn(MANAGE, keys)
        roles_group = PermissionGroup.objects.get(name=ROLES_GROUP)
        self.assertIn(VIEW, set(roles_group.permissions.values_list("key", flat=True)))

    def test_a_newly_provisioned_school_admin_holds_manage_and_can_change_switches(self):
        school = self._school(self._slug("provisioned"))
        tenant = school.tenant
        role = provision_role_from_prebuilt(tenant=tenant, prebuilt_key="school_admin")
        self.assertIn(MANAGE, _granted(role))
        self.assertIn(VIEW, _granted(role))

        admin = make_school_admin(self.ikeja, email=f"{self._slug('head')}@test.com")
        make_assignment(tenant, admin, role, branch=None)
        field = make_field_definition(
            "farseed.vendor.bank_account_number", "Bank account number", sensitive=True,
        )
        storekeeper = make_role(tenant, name="Storekeeper", key="storekeeper")

        url = reverse(
            "rbac-role-field-access", kwargs={"tenant_slug": tenant.slug, "key": storekeeper.key},
        )
        response = _client(admin).patch(
            _q(url, tenant.slug),
            {"changes": [{"field": field.key, "read": True}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(
            RoleFieldAccess.objects.filter(role=storekeeper, field=field, can_read=True).exists()
        )

    def test_the_seed_backfill_gives_an_existing_school_admin_role_manage(self):
        school = self._school(self._slug("backfill"))
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="school_admin", name="School Admin",
            is_system_role=True,
        )
        self.assertNotIn(MANAGE, _granted(role))
        _call("seed_school_permissions")
        self.assertIn(MANAGE, _granted(role))
        self.assertIn(VIEW, _granted(role))


class SingleBranchSeededSchoolAdminTests(_SeededSchoolAdmin, TestCase):
    multi_branch = False


class MultiBranchSeededSchoolAdminTests(_SeededSchoolAdmin, TestCase):
    multi_branch = True
