"""Who may change a setting: :func:`vs_rbac.scoping.assert_caller_may_configure`.

A setting with no branch binds every branch of the tenant, so only a caller
whose reach is the whole tenant may change it. A setting keyed to a branch is
that branch's own, and a caller covering the branch may change it. The
permission key is a separate question and is not asked here.

Lagoon View runs Ikeja and Lekki. Ngozi is pinned to Ikeja, Emeka covers both
through a grant naming each, and Funmi holds a whole-tenant grant.
"""
from django.test import TestCase

from vs_rbac.exceptions import SharedRecordReadOnly
from vs_rbac.scoping import assert_caller_may_configure

from .helpers import (
    make_assignment,
    make_branch,
    make_role,
    make_school,
    make_staff_user,
)


class AssertCallerMayConfigureTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="lagoon-view-cfg", name="Lagoon View")
        cls.tenant = cls.school.tenant
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        role = make_role(cls.school, name="Settings Admin")

        cls.ngozi = make_staff_user(cls.ikeja, email="ngozi@lagoon-cfg.test")
        make_assignment(cls.school, cls.ngozi, role, branch=cls.ikeja)

        cls.emeka = make_staff_user(cls.ikeja, email="emeka@lagoon-cfg.test")
        make_assignment(cls.school, cls.emeka, role, branch=cls.ikeja)
        make_assignment(cls.school, cls.emeka, role, branch=cls.lekki)

        cls.funmi = make_staff_user(cls.ikeja, email="funmi@lagoon-cfg.test")
        make_assignment(cls.school, cls.funmi, role, branch=None)

    def test_a_branch_bound_caller_is_refused_the_tenants_own_setting(self):
        for user in (self.ngozi, self.emeka):
            with self.subTest(user=user.email):
                with self.assertRaises(SharedRecordReadOnly) as refused:
                    assert_caller_may_configure(
                        user, self.tenant, message="Only a school-wide administrator can.",
                    )
                self.assertEqual(refused.exception.message, "Only a school-wide administrator can.")
                self.assertEqual(refused.exception.http_status, 403)

    def test_the_refusal_falls_back_to_the_shared_row_sentence(self):
        with self.assertRaises(SharedRecordReadOnly) as refused:
            assert_caller_may_configure(self.ngozi, self.tenant)
        self.assertEqual(refused.exception.message, SharedRecordReadOnly.default_message)

    def test_a_whole_tenant_caller_may_change_the_tenants_setting_and_any_branchs(self):
        assert_caller_may_configure(self.funmi, self.tenant)
        assert_caller_may_configure(self.funmi, self.tenant, self.ikeja)
        assert_caller_may_configure(self.funmi, self.tenant, self.lekki.pk)

    def test_a_branch_bound_caller_may_change_their_own_branchs_setting(self):
        assert_caller_may_configure(self.ngozi, self.tenant, self.ikeja)
        assert_caller_may_configure(self.emeka, self.tenant, self.lekki)

    def test_a_branch_bound_caller_is_refused_another_branchs_setting(self):
        with self.assertRaises(SharedRecordReadOnly):
            assert_caller_may_configure(self.ngozi, self.tenant, self.lekki)
