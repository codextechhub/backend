"""The development school builder, checked against what a real school is created with.

``build_school`` stands in for school creation in every seeder, so where it
builds a thinner school than the product does, every seeded school is wrong in
the same way and nobody can tell from the screens which gaps are the product's.
These tests pin the three places it once differed: the role catalogue, the
administrators' staff records, and the administrator's reach into finance and
procurement.
"""
from django.test import TestCase

from vs_rbac.models import (
    PrebuiltRoleTemplate,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)

from schools.vs_staff.models import StaffProfile

from .dev.fixtures import build_school
from .services.admin_provisioning import REQUIRED_ROLE_KEYS


def _seed_prebuilt_roles():
    """The five templates every school is provisioned from."""
    for key, name, scope in (
        ("school_admin", "School Admin", "institution"),
        ("branch_admin", "Branch Admin", "branch"),
        ("teacher", "Teacher", "institution"),
        ("finance_admin", "Finance Admin", "institution"),
        ("procurement_admin", "Procurement Admin", "institution"),
    ):
        PrebuiltRoleTemplate.objects.update_or_create(
            key=key, defaults={"name": name, "scope": scope, "tier": "A"},
        )


def _build():
    return build_school(
        slug="bright-star", name="Bright Star Schools",
        admin_name=("Tunde", "Adebayo"), branch_admin_name=("Chioma", "Okafor"),
        with_books=False, with_onboarding=False,
    )


class BuildSchoolMatchesARealSchoolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _seed_prebuilt_roles()
        cls.built = _build()

    def test_the_school_carries_every_role_a_real_school_is_created_with(self):
        keys = set(
            TenantRoleTemplate.objects.filter(tenant=self.built.tenant)
            .values_list("key", flat=True),
        )
        self.assertEqual(keys, set(REQUIRED_ROLE_KEYS))

    def test_both_administrators_have_a_staff_record(self):
        """An administrator without one is named on approval screens by number."""
        for user, title in (
            (self.built.admin, "Proprietor"),
            (self.built.branch_admin, "Branch Administrator"),
        ):
            with self.subTest(email=user.email):
                profile = StaffProfile.all_objects.get(user=user)
                self.assertEqual(profile.tenant_id, self.built.tenant.pk)
                self.assertEqual(profile.job_title, title)

    def test_the_branch_administrator_is_based_at_the_main_branch(self):
        profile = StaffProfile.all_objects.get(user=self.built.branch_admin)
        self.assertEqual(profile.branch_id, self.built.main_branch.pk)

    def test_the_administrator_can_reach_finance_and_procurement(self):
        """School Admin carries neither, and the seeded admin is who a developer signs in as."""
        keys = set(
            TenantUserRoleAssignment.objects.filter(
                tenant=self.built.tenant, user=self.built.admin,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role__key", flat=True),
        )
        self.assertEqual(keys, {"school_admin", "finance_admin", "procurement_admin"})

    def test_running_it_again_writes_no_second_record_or_grant(self):
        tenant = self.built.tenant
        before = (
            StaffProfile.all_objects.filter(tenant=tenant).count(),
            TenantUserRoleAssignment.objects.filter(tenant=tenant).count(),
        )
        _build()
        after = (
            StaffProfile.all_objects.filter(tenant=tenant).count(),
            TenantUserRoleAssignment.objects.filter(tenant=tenant).count(),
        )
        self.assertEqual(before, after)
