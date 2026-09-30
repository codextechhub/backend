"""Who may change what a role means, and whose exceptions a caller may set.

A role binds every holder of it, and its holders sit only inside its own
branches, so changing a role is changing a row whose branch set is the role's.
A school-wide role is therefore a whole-school caller's to change, an Ikeja
role an Ikeja caller's, and a branch-bound caller may neither create a
school-wide role nor widen one of theirs past their branches. One person's
exceptions follow that person wherever their access reaches, so they are judged
by the person's postings and reach together.

Bright Star runs Ikeja and Lekki. Mrs Bello administers Lekki: her Branch Admin
grant is pinned there and carries every role, field access and exception key.
Mr Okafor holds the same role school-wide and is the control. Every refusal is
a 403 ``SHARED_RECORD_READ_ONLY`` with nothing written.

Harbour Primary has one branch, and its administrator's grant pinned to that
branch reaches the whole school, so nothing here narrows her.
"""
from datetime import date

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.grant_reach import (
    EXCEPTION_SHARED,
    REACH_OTHER_BRANCHES,
    REACH_SHARED,
    ROLE_OTHER_BRANCHES,
    ROLE_SHARED,
)
from vs_rbac.models import (
    RoleFieldAccess,
    TenantRoleChangeRequest,
    TenantRolePermission,
    TenantRoleTemplate,
    UserFieldAccessOverride,
    UserPermissionOverride,
)
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
    make_staff_user,
)

ADMIN_KEYS = [
    "school.roles.view",
    "school.roles.create",
    "school.roles.update",
    "school.roles.delete",
    "school.roles.assign",
    "school.field_access.view",
    "school.field_access.update",
    "school.user_overrides.view",
    "school.user_overrides.create",
    "school.user_overrides.delete",
]
TARGET_KEY = "school.students.update"


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _refused(test, response, message):
    test.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
    body = response.json()
    test.assertEqual(body["error"]["code"], "SHARED_RECORD_READ_ONLY")
    test.assertEqual(body["message"], message)


class _BrightStar(TestCase):
    """Bright Star with its two administrators and three roles."""

    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="definition-reach", name="Bright Star School")
        cls.tenant = school.tenant
        cls.slug = cls.tenant.slug
        cls.ikeja = make_branch(school, name="Ikeja Branch")
        cls.lekki = make_branch(school, name="Lekki Branch", is_main=False)

        cls.branch_admin = make_role(cls.tenant, name="Branch Admin", key="branch-admin")
        for key in ADMIN_KEYS + [TARGET_KEY]:
            make_role_permission(cls.branch_admin, make_permission(key))

        cls.bello = make_school_admin(cls.lekki, email="bello@definition-reach.test")
        make_assignment(cls.tenant, cls.bello, cls.branch_admin, branch=cls.lekki)
        cls.okafor = make_school_admin(cls.ikeja, email="okafor@definition-reach.test")
        make_assignment(cls.tenant, cls.okafor, cls.branch_admin, branch=None)

        cls.teacher = make_role(cls.tenant, name="Teacher", key="teacher")
        cls.lekki_clerk = make_role(
            cls.tenant, name="Lekki Clerk", key="lekki-clerk", branch=cls.lekki,
        )
        cls.ikeja_clerk = make_role(
            cls.tenant, name="Ikeja Clerk", key="ikeja-clerk", branch=cls.ikeja,
        )

    def _url(self, name, **kwargs):
        return reverse(name, kwargs={"tenant_slug": self.slug, **kwargs}) + f"?tenant={self.slug}"


class RoleDefinitionReachTests(_BrightStar):

    def _create(self, caller, **body):
        body = {"name": "Exam Officer", "reason": "Exams.", **body}
        return _client(caller).post(self._url("rbac-role-list-create"), body, format="json")

    def _patch(self, caller, role, body):
        return _client(caller).patch(
            self._url("rbac-role-detail", key=role.key), body, format="json",
        )

    # -- create ---------------------------------------------------------------
    def test_a_branch_admin_cannot_create_a_school_wide_role(self):
        response = self._create(self.bello, branch_ids=[])
        _refused(self, response, REACH_SHARED)
        self.assertFalse(TenantRoleTemplate.objects.filter(name="Exam Officer").exists())

    def test_a_branch_admin_naming_no_branch_is_creating_a_school_wide_role(self):
        response = self._create(self.bello)
        _refused(self, response, REACH_SHARED)
        self.assertFalse(TenantRoleTemplate.objects.filter(name="Exam Officer").exists())

    def test_a_branch_admin_cannot_create_a_role_for_another_branch(self):
        response = self._create(self.bello, branch_ids=[self.ikeja.pk])
        _refused(self, response, REACH_OTHER_BRANCHES)
        self.assertFalse(TenantRoleTemplate.objects.filter(name="Exam Officer").exists())

    def test_a_branch_admin_may_create_a_role_for_their_own_branch(self):
        response = self._create(self.bello, branch_ids=[self.lekki.pk])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        role = TenantRoleTemplate.objects.get(name="Exam Officer")
        self.assertEqual(role.branch_ids, [self.lekki.pk])

    def test_the_whole_school_admin_may_create_a_school_wide_role(self):
        response = self._create(self.okafor, branch_ids=[])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(TenantRoleTemplate.objects.get(name="Exam Officer").branch_ids, [])

    # -- edit -----------------------------------------------------------------
    def test_a_branch_admin_cannot_rename_a_school_wide_role(self):
        response = self._patch(self.bello, self.teacher, {"name": "Class Teacher"})
        _refused(self, response, ROLE_SHARED.format(verb="do"))
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.name, "Teacher")

    def test_a_branch_admin_cannot_change_a_school_wide_roles_permissions(self):
        response = self._patch(self.bello, self.teacher, {
            "permission_keys": [TARGET_KEY], "reason": "More access.",
        })
        _refused(self, response, ROLE_SHARED.format(verb="do"))
        self.assertFalse(TenantRolePermission.objects.filter(role=self.teacher).exists())

    def test_a_branch_admin_cannot_retire_a_school_wide_role(self):
        response = self._patch(self.bello, self.teacher, {"status": "INACTIVE"})
        _refused(self, response, ROLE_SHARED.format(verb="do"))
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.status, "ACTIVE")

    def test_a_branch_admin_cannot_change_another_branchs_role(self):
        response = self._patch(self.bello, self.ikeja_clerk, {"name": "Ikeja Registrar"})
        _refused(self, response, ROLE_OTHER_BRANCHES.format(verb="do"))
        self.ikeja_clerk.refresh_from_db()
        self.assertEqual(self.ikeja_clerk.name, "Ikeja Clerk")

    def test_a_branch_admin_may_change_their_own_branchs_role(self):
        response = self._patch(self.bello, self.lekki_clerk, {
            "name": "Lekki Registrar", "permission_keys": [TARGET_KEY], "reason": "Cover.",
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.lekki_clerk.refresh_from_db()
        self.assertEqual(self.lekki_clerk.name, "Lekki Registrar")
        self.assertTrue(TenantRolePermission.objects.filter(
            role=self.lekki_clerk, permission_id=TARGET_KEY, granted=True,
        ).exists())

    def test_a_branch_admin_cannot_widen_their_own_role_to_the_whole_school(self):
        response = self._patch(self.bello, self.lekki_clerk, {
            "branch_ids": [], "reason": "Everyone.",
        })
        _refused(self, response, REACH_SHARED)
        self.lekki_clerk.refresh_from_db()
        self.assertEqual(self.lekki_clerk.branch_ids, [self.lekki.pk])

    def test_a_branch_admin_cannot_widen_their_own_role_to_another_branch(self):
        response = self._patch(self.bello, self.lekki_clerk, {
            "branch_ids": [self.lekki.pk, self.ikeja.pk], "reason": "Both.",
        })
        _refused(self, response, REACH_OTHER_BRANCHES)
        self.lekki_clerk.refresh_from_db()
        self.assertEqual(self.lekki_clerk.branch_ids, [self.lekki.pk])

    def test_the_whole_school_admin_may_change_a_school_wide_role(self):
        response = self._patch(self.okafor, self.teacher, {"name": "Class Teacher"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.name, "Class Teacher")

    # -- delete ---------------------------------------------------------------
    def test_a_branch_admin_cannot_delete_a_school_wide_role(self):
        response = _client(self.bello).delete(self._url("rbac-role-detail", key="teacher"))
        _refused(self, response, ROLE_SHARED.format(verb="do"))
        self.assertTrue(TenantRoleTemplate.objects.filter(pk=self.teacher.pk).exists())

    def test_a_branch_admin_cannot_delete_another_branchs_role(self):
        response = _client(self.bello).delete(
            self._url("rbac-role-detail", key="ikeja-clerk"),
        )
        _refused(self, response, ROLE_OTHER_BRANCHES.format(verb="do"))
        self.assertTrue(TenantRoleTemplate.objects.filter(pk=self.ikeja_clerk.pk).exists())

    def test_a_branch_admin_may_delete_their_own_branchs_unused_role(self):
        response = _client(self.bello).delete(
            self._url("rbac-role-detail", key="lekki-clerk"),
        )
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(TenantRoleTemplate.objects.filter(pk=self.lekki_clerk.pk).exists())

    def test_the_whole_school_admin_may_delete_a_school_wide_unused_role(self):
        response = _client(self.okafor).delete(self._url("rbac-role-detail", key="teacher"))
        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT))
        self.assertFalse(TenantRoleTemplate.objects.filter(pk=self.teacher.pk).exists())

    # -- change requests ------------------------------------------------------
    def test_a_branch_admin_cannot_ask_to_change_a_school_wide_role(self):
        response = _client(self.bello).post(
            self._url("rbac-role-change-request-list-create"),
            {
                "target_role": self.teacher.pk,
                "justification": "More access.",
                "delta_items": [{"permission_key": TARGET_KEY, "operation": "ADD"}],
            },
            format="json",
        )
        _refused(self, response, ROLE_SHARED.format(verb="do"))
        self.assertFalse(TenantRoleChangeRequest.objects.filter(
            target_role=self.teacher,
        ).exists())


class RoleCanEditTests(_BrightStar):
    """``can_edit`` on the role list, detail and Field Access responses."""

    def _listed(self, caller):
        response = _client(caller).get(self._url("rbac-role-list-create") + "&page_size=100")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return {row["key"]: row["can_edit"] for row in response.json()["data"]}

    def test_the_list_marks_what_a_branch_admin_may_change(self):
        flags = self._listed(self.bello)
        self.assertFalse(flags["teacher"])
        self.assertFalse(flags["ikeja-clerk"])
        self.assertTrue(flags["lekki-clerk"])

    def test_the_whole_school_admin_may_change_every_role(self):
        self.assertTrue(all(self._listed(self.okafor).values()))

    def test_the_detail_and_field_access_agree_with_the_list(self):
        for key, expected in (("teacher", False), ("lekki-clerk", True)):
            with self.subTest(role=key):
                detail = _client(self.bello).get(self._url("rbac-role-detail", key=key))
                self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.content)
                self.assertIs(detail.json()["data"]["can_edit"], expected)
                fields = _client(self.bello).get(
                    self._url("rbac-role-field-access", key=key),
                )
                self.assertEqual(fields.status_code, status.HTTP_200_OK, fields.content)
                self.assertIs(fields.json()["data"]["role"]["can_edit"], expected)


class RoleFieldAccessReachTests(_BrightStar):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bank = make_field_definition(
            "defreach.vendor.bank_account_number", "Bank account number", sensitive=True,
        )

    def _patch(self, caller, role):
        return _client(caller).patch(
            self._url("rbac-role-field-access", key=role.key),
            {"changes": [{"field": self.bank.key, "read": True}]},
            format="json",
        )

    def test_a_branch_admin_cannot_change_a_school_wide_roles_field_access(self):
        response = self._patch(self.bello, self.teacher)
        _refused(self, response, ROLE_SHARED.format(verb="see"))
        self.assertFalse(RoleFieldAccess.objects.filter(role=self.teacher).exists())

    def test_a_branch_admin_cannot_change_another_branchs_roles_field_access(self):
        response = self._patch(self.bello, self.ikeja_clerk)
        _refused(self, response, ROLE_OTHER_BRANCHES.format(verb="see"))
        self.assertFalse(RoleFieldAccess.objects.filter(role=self.ikeja_clerk).exists())

    def test_a_branch_admin_may_change_their_own_branchs_roles_field_access(self):
        response = self._patch(self.bello, self.lekki_clerk)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(RoleFieldAccess.objects.filter(
            role=self.lekki_clerk, field=self.bank, can_read=True,
        ).exists())

    def test_the_whole_school_admin_may_change_a_school_wide_roles_field_access(self):
        response = self._patch(self.okafor, self.teacher)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(RoleFieldAccess.objects.filter(
            role=self.teacher, field=self.bank, can_read=True,
        ).exists())


class ExceptionReachTests(_BrightStar):
    """Tunde is Lekki's own; Nwankwo is the school-wide registrar; Sule is
    Ikeja's; Ada is posted at Lekki but holds a school-wide Bursar role.

    Mrs Bello may read Tunde's, Nwankwo's and Ada's exceptions and change only
    Tunde's. Sule is posted only outside her branch, so to her he is nobody:
    404 on every verb, as the staff directory answers for him.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bank = make_field_definition(
            "exreach.vendor.bank_account_number", "Bank account number", sensitive=True,
        )
        cls.tunde = make_staff_user(cls.lekki, email="tunde@definition-reach.test")
        make_assignment(cls.tenant, cls.tunde, cls.lekki_clerk)
        cls.nwankwo = make_staff_user(
            None, email="nwankwo@definition-reach.test", tenant=cls.tenant,
        )
        make_assignment(cls.tenant, cls.nwankwo, cls.teacher)
        cls.sule = make_staff_user(cls.ikeja, email="sule@definition-reach.test")
        make_assignment(cls.tenant, cls.sule, cls.ikeja_clerk)
        cls.ada = make_staff_user(cls.lekki, email="ada@definition-reach.test")
        make_assignment(cls.tenant, cls.ada, cls.teacher, branch=None)

    def _field_exception(self, caller, person):
        return _client(caller).post(
            self._url("rbac-user-field-access-override-list-create", user_id=person.pk),
            {"field": self.bank.key, "access": "READ", "mode": "ALLOW", "reason": "Cover."},
            format="json",
        )

    def _permission_exception(self, caller, person):
        return _client(caller).post(
            self._url("rbac-user-permission-override-list-create", user_id=person.pk),
            {"permission": TARGET_KEY, "mode": "DENY", "reason": "Cover."},
            format="json",
        )

    def test_a_branch_admin_may_set_exceptions_on_their_own_branchs_person(self):
        response = self._field_exception(self.bello, self.tunde)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        response = self._permission_exception(self.bello, self.tunde)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_a_branch_admin_is_refused_everybody_whose_access_reaches_further(self):
        for person in (self.nwankwo, self.ada):
            with self.subTest(person=person.email):
                _refused(self, self._field_exception(self.bello, person), EXCEPTION_SHARED)
                _refused(self, self._permission_exception(self.bello, person), EXCEPTION_SHARED)
                self.assertFalse(UserFieldAccessOverride.objects.filter(user=person).exists())
                self.assertFalse(UserPermissionOverride.objects.filter(user=person).exists())

    def test_another_branchs_person_is_nobody_to_a_branch_admin(self):
        """Sule is posted only at Ikeja, so Lekki cannot even see his exceptions."""
        row = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.sule, field=self.bank, access="READ",
            mode="DENY", reason="Set by Okafor.", created_by=self.okafor,
        )
        client = _client(self.bello)
        for name in (
            "rbac-user-field-access-override-list-create",
            "rbac-user-permission-override-list-create",
        ):
            with self.subTest(read=name):
                response = client.get(self._url(name, user_id=self.sule.pk))
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self._field_exception(self.bello, self.sule)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = client.delete(self._url(
            "rbac-user-field-access-override-detail", user_id=self.sule.pk, id=row.pk,
        ))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(UserFieldAccessOverride.objects.filter(pk=row.pk).exists())
        self.assertFalse(UserPermissionOverride.objects.filter(user=self.sule).exists())

    def _flag(self, caller, person, name="rbac-user-field-access-override-list-create"):
        response = _client(caller).get(self._url(name, user_id=person.pk))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()["can_change_exceptions"]

    def test_the_list_says_whether_the_reader_may_change_this_persons_exceptions(self):
        for name in (
            "rbac-user-field-access-override-list-create",
            "rbac-user-permission-override-list-create",
        ):
            with self.subTest(read=name):
                self.assertTrue(self._flag(self.bello, self.tunde, name))
                # Readable, because they are posted at Lekki or school-wide,
                # and not changeable, because their access reaches Ikeja.
                self.assertFalse(self._flag(self.bello, self.ada, name))
                self.assertFalse(self._flag(self.bello, self.nwankwo, name))
                self.assertTrue(self._flag(self.okafor, self.sule, name))
                self.assertTrue(self._flag(self.okafor, self.ada, name))

    def test_the_flag_rides_the_as_at_list_too(self):
        response = _client(self.bello).get(
            self._url("rbac-user-field-access-override-list-create", user_id=self.tunde.pk)
            + "&as_at=" + date.today().isoformat(),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertTrue(response.json()["can_change_exceptions"])

    def test_the_whole_school_admin_may_set_exceptions_on_anybody(self):
        for person in (self.nwankwo, self.sule, self.ada):
            with self.subTest(person=person.email):
                response = self._field_exception(self.okafor, person)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
                response = self._permission_exception(self.okafor, person)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_a_branch_admin_cannot_lift_an_exception_on_a_school_wide_person(self):
        field_row = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.ada, field=self.bank, access="READ",
            mode="DENY", reason="Set by Okafor.", created_by=self.okafor,
        )
        permission_row = UserPermissionOverride.objects.create(
            tenant=self.tenant, user=self.ada, permission_id=TARGET_KEY,
            mode="DENY", reason="Set by Okafor.", created_by=self.okafor,
        )
        response = _client(self.bello).delete(self._url(
            "rbac-user-field-access-override-detail", user_id=self.ada.pk, id=field_row.pk,
        ))
        _refused(self, response, EXCEPTION_SHARED)
        response = _client(self.bello).delete(self._url(
            "rbac-user-permission-override-detail", user_id=self.ada.pk, id=permission_row.pk,
        ))
        _refused(self, response, EXCEPTION_SHARED)
        self.assertTrue(UserFieldAccessOverride.objects.filter(pk=field_row.pk).exists())
        self.assertTrue(UserPermissionOverride.objects.filter(pk=permission_row.pk).exists())

    def test_a_branch_admin_may_lift_an_exception_on_their_own_branchs_person(self):
        row = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.tunde, field=self.bank, access="READ",
            mode="DENY", reason="Set by Okafor.", created_by=self.okafor,
        )
        response = _client(self.bello).delete(self._url(
            "rbac-user-field-access-override-detail", user_id=self.tunde.pk, id=row.pk,
        ))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertFalse(UserFieldAccessOverride.objects.filter(pk=row.pk).exists())


class OneBranchSchoolTests(TestCase):
    """Harbour Primary's administrator is pinned to Main, its only branch."""

    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="definition-reach-one", name="Harbour Primary")
        cls.tenant = school.tenant
        cls.slug = cls.tenant.slug
        cls.main = make_branch(school, name="Main Branch")
        role = make_role(cls.tenant, name="Branch Admin", key="branch-admin")
        for key in ADMIN_KEYS + [TARGET_KEY]:
            make_role_permission(role, make_permission(key))
        cls.tolu = make_school_admin(cls.main, email="tolu@definition-reach-one.test")
        make_assignment(cls.tenant, cls.tolu, role, branch=cls.main)
        cls.teacher = make_role(cls.tenant, name="Teacher", key="teacher")
        cls.kemi = make_staff_user(None, email="kemi@definition-reach-one.test", tenant=cls.tenant)
        make_assignment(cls.tenant, cls.kemi, cls.teacher)

    def _url(self, name, **kwargs):
        return reverse(name, kwargs={"tenant_slug": self.slug, **kwargs}) + f"?tenant={self.slug}"

    def test_she_may_create_and_change_school_wide_roles(self):
        response = _client(self.tolu).post(
            self._url("rbac-role-list-create"),
            {"name": "Exam Officer", "branch_ids": [], "reason": "Exams."}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        response = _client(self.tolu).patch(
            self._url("rbac-role-detail", key="teacher"), {"name": "Class Teacher"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_she_sees_every_role_and_person_as_hers_to_change(self):
        response = _client(self.tolu).get(self._url("rbac-role-list-create"))
        self.assertTrue(all(row["can_edit"] for row in response.json()["data"]))
        response = _client(self.tolu).get(
            self._url("rbac-user-permission-override-list-create", user_id=self.kemi.pk),
        )
        self.assertTrue(response.json()["can_change_exceptions"])

    def test_she_may_set_an_exception_on_a_school_wide_person(self):
        response = _client(self.tolu).post(
            self._url("rbac-user-permission-override-list-create", user_id=self.kemi.pk),
            {"permission": TARGET_KEY, "mode": "DENY", "reason": "Cover."}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
