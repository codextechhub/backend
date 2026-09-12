"""A branch's own role reads the name that branch has now.

Each branch carries its own copy of Branch Admin, and the copy's name is
composed from the branch's name when the role is provisioned. Nothing
re-composed it afterwards, so Brightfield renaming Ikeja to Yaba was left with
"Branch Admin - Ikeja" on its roles screen and no way to correct it: the name
is a stored column, and the only thing that had ever rewritten one was a
migration.

Both shapes of school, because the rule reads differently in each. Where a
school has several branches the name says which; where it has one the name
carries no suffix at all, and a rename there must not introduce one.
"""
from django.test import TestCase

from vs_rbac.models import PrebuiltRoleTemplate, TenantRoleTemplate
from vs_rbac.services import provision_role_from_prebuilt

from .helpers import make_branch, make_school


class BranchRolesFollowTheBranchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        PrebuiltRoleTemplate.objects.update_or_create(
            key="branch_admin",
            defaults={"name": "Branch Admin", "scope": "branch", "tier": "A"},
        )

    def role_of(self, branch):
        return TenantRoleTemplate.objects.get(
            tenant_id=branch.tenant_id, key=f"branch_admin-{branch.pk}",
        )

    def two_branch_school(self, slug):
        """A school with Ikeja and Lekki, each holding its own Branch Admin."""
        school = make_school(slug=slug, name=slug.replace("-", " ").title())
        ikeja = make_branch(school, name="Ikeja", is_main=True)
        lekki = make_branch(school, name="Lekki", is_main=False)
        for branch in (ikeja, lekki):
            provision_role_from_prebuilt(
                tenant=school.tenant, branch=branch, prebuilt_key="branch_admin",
            )
        return school, ikeja, lekki

    def test_renaming_a_branch_renames_the_role_named_after_it(self):
        school, ikeja, lekki = self.two_branch_school("brightfield-rename")
        self.assertEqual(self.role_of(ikeja).name, "Branch Admin - Ikeja")

        ikeja.name = "Yaba"
        ikeja.save()

        self.assertEqual(self.role_of(ikeja).name, "Branch Admin - Yaba")

    def test_the_other_branches_roles_are_left_alone(self):
        school, ikeja, lekki = self.two_branch_school("brightfield-siblings")

        ikeja.name = "Yaba"
        ikeja.save()

        self.assertEqual(self.role_of(lekki).name, "Branch Admin - Lekki")

    def test_a_school_with_one_branch_gains_no_suffix_when_it_renames(self):
        """The dimension recedes, and renaming the only site does not bring it back."""
        school = make_school(slug="sunrise-rename", name="Sunrise Academy")
        main = make_branch(school, name="Main Branch", is_main=True)
        provision_role_from_prebuilt(
            tenant=school.tenant, branch=main, prebuilt_key="branch_admin",
        )
        self.assertEqual(self.role_of(main).name, "Branch Admin")

        main.name = "Sunrise Central"
        main.save()

        self.assertEqual(self.role_of(main).name, "Branch Admin")

    def test_a_name_the_school_chose_itself_survives_the_rename(self):
        """Renaming a branch is not permission to rename what a school named."""
        school, ikeja, lekki = self.two_branch_school("brightfield-own-name")
        role = self.role_of(ikeja)
        role.name = "Ikeja Head"
        role.save(update_fields=["name"])

        ikeja.name = "Yaba"
        ikeja.save()

        self.assertEqual(self.role_of(ikeja).name, "Ikeja Head")

    def test_a_name_another_role_already_holds_does_not_fail_the_rename(self):
        """Two branches under one name is the school's arrangement to settle.

        The column is unique per tenant, so composing the same name twice would
        raise. The rename is what the school asked for and it stands; the role
        keeps the name it can still be told apart by.
        """
        school, ikeja, lekki = self.two_branch_school("brightfield-clash")

        lekki.name = "Ikeja"
        lekki.save()

        self.assertEqual(self.role_of(lekki).name, "Branch Admin - Lekki")
        self.assertEqual(self.role_of(ikeja).name, "Branch Admin - Ikeja")

    def test_an_edit_that_is_not_a_rename_leaves_the_roles_alone(self):
        school, ikeja, lekki = self.two_branch_school("brightfield-other-edit")

        ikeja.state = "Ogun"
        ikeja.save()

        self.assertEqual(self.role_of(ikeja).name, "Branch Admin - Ikeja")
