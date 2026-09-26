"""A branch-bound caller cannot hand out, change or remove reach they do not hold.

Bright Star runs Ikeja and Lekki. Mrs Bello administers Lekki: her Deputy Head
grant, which carries ``school.roles.assign``, is pinned there. Tunde teaches at
Lekki; Mrs Nwankwo is the school-wide registrar. Mr Okafor is the whole-school
administrator and the control: nothing here narrows him.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.models import TenantUserRoleAssignment
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

ROLE_KEYS = ["school.roles.view", "school.roles.assign"]


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


class GrantReachTests(TestCase):
    def setUp(self):
        school = make_school(slug="grant-reach", name="Bright Star School")
        self.tenant = school.tenant
        self.slug = self.tenant.slug
        self.ikeja = make_branch(school, name="Ikeja Branch")
        self.lekki = make_branch(school, name="Lekki Branch", is_main=False)

        self.deputy = make_role(self.tenant, name="Deputy Head", key="deputy-head")
        for key in ROLE_KEYS:
            make_role_permission(self.deputy, make_permission(key))
        self.teacher = make_role(self.tenant, name="Teacher", key="teacher")

        self.bello = make_school_admin(self.lekki, email="bello@grant-reach.test")
        make_assignment(self.tenant, self.bello, self.deputy, branch=self.lekki)
        self.okafor = make_school_admin(self.ikeja, email="okafor@grant-reach.test")
        make_assignment(self.tenant, self.okafor, self.deputy, branch=None)

        self.tunde = make_school_admin(self.lekki, email="tunde@grant-reach.test")
        self.sule = make_school_admin(self.ikeja, email="sule@grant-reach.test")
        self.nwankwo = make_school_admin(
            None, email="nwankwo@grant-reach.test", tenant=self.tenant,
        )
        self.registrar_grant = make_assignment(
            self.tenant, self.nwankwo, self.teacher, branch=None,
        )

    def _url(self, name="rbac-assignment-list-create", **kwargs):
        return reverse(name, kwargs={"tenant_slug": self.slug, **kwargs}) + f"?tenant={self.slug}"

    def _grant(self, caller, user, branch=None):
        body = {"user": user.pk, "role": self.teacher.pk}
        if branch is not None:
            body["branch"] = branch.pk
        return _client(caller).post(self._url(), body, format="json")

    def test_a_branch_admin_may_grant_at_their_own_branch(self):
        response = self._grant(self.bello, self.tunde, branch=self.lekki)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_school_wide_grant_from_a_branch_admin_is_refused(self):
        response = self._grant(self.bello, self.tunde)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(TenantUserRoleAssignment.objects.filter(
            user=self.tunde, role=self.teacher,
        ).exists())

    def test_a_grant_at_another_branch_is_refused(self):
        response = self._grant(self.bello, self.tunde, branch=self.ikeja)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_a_grant_to_the_school_wide_registrar_is_refused(self):
        response = self._grant(self.bello, self.nwankwo, branch=self.lekki)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn("user", response.data.get("errors", response.data).__str__())

    def test_revoking_the_registrars_school_wide_role_is_refused(self):
        response = _client(self.bello).post(
            self._url("rbac-assignment-revoke", id=self.registrar_grant.pk),
            {"reason_note": "Tidying up"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.registrar_grant.refresh_from_db()
        self.assertEqual(self.registrar_grant.assignment_status, "ACTIVE")

    def test_the_whole_school_admin_is_not_narrowed(self):
        response = self._grant(self.okafor, self.sule)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_the_holder_list_shows_a_branch_admin_their_people_and_the_school_wide(self):
        make_assignment(self.tenant, self.sule, self.teacher, branch=self.ikeja)
        make_assignment(self.tenant, self.tunde, self.teacher, branch=self.lekki)
        response = _client(self.bello).get(self._url() + "&role=teacher")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        holders = {str(row["user_id"]) for row in response.data["data"]}
        self.assertIn(str(self.tunde.pk), holders)
        self.assertIn(str(self.nwankwo.pk), holders)
        self.assertNotIn(str(self.sule.pk), holders)
