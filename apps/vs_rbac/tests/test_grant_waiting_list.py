"""The list of role grants waiting for approval, as a role's screen reads it.

Bright Star runs Ikeja and Lekki. Mr Okafor administers the whole school and
Mrs Bello administers Lekki only. Tunde teaches at Ikeja and Ada at Lekki; both
have an Assistant Bursar grant waiting on the ladder. Greenfield is another
school with a waiting grant of its own, which nobody at Bright Star may see.
"""
from urllib.parse import urlencode

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.models import Permission, TenantRoleGrantRequest
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
)

ROLE_KEYS = ["school.roles.view", "school.roles.assign"]


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _waiting(tenant, *, requested_by, user, role, branch=None, **kwargs):
    return TenantRoleGrantRequest.objects.create(
        tenant=tenant, requested_by=requested_by, user=user, role=role,
        branch=branch, **kwargs,
    )


class GrantWaitingListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="grant-waiting", name="Bright Star School")
        cls.tenant = school.tenant
        cls.slug = cls.tenant.slug
        cls.ikeja = make_branch(school, name="Ikeja Branch")
        cls.lekki = make_branch(school, name="Lekki Branch", is_main=False)

        cls.admin_role = make_role(cls.tenant, name="Deputy Head", key="deputy-head")
        for key in ROLE_KEYS:
            make_role_permission(cls.admin_role, make_permission(key))
        cls.bursar = make_role(cls.tenant, name="Assistant Bursar", key="assistant-bursar")
        cls.teacher = make_role(cls.tenant, name="Teacher", key="teacher")

        cls.okafor = make_school_admin(cls.ikeja, email="okafor@grant-waiting.test")
        make_assignment(cls.tenant, cls.okafor, cls.admin_role, branch=None)
        cls.bello = make_school_admin(cls.lekki, email="bello@grant-waiting.test")
        make_assignment(cls.tenant, cls.bello, cls.admin_role, branch=cls.lekki)
        cls.plain = make_staff_user(cls.ikeja, email="plain@grant-waiting.test")

        cls.tunde = make_staff_user(cls.ikeja, email="tunde@grant-waiting.test")
        cls.ada = make_staff_user(cls.lekki, email="ada@grant-waiting.test")
        cls.tunde_waits = _waiting(
            cls.tenant, requested_by=cls.okafor, user=cls.tunde, role=cls.bursar,
            reason_note="Covering the bursar's leave",
        )
        cls.ada_waits = _waiting(
            cls.tenant, requested_by=cls.okafor, user=cls.ada, role=cls.bursar,
            branch=cls.lekki,
        )

    def _url(self, slug=None, **params):
        slug = slug or self.slug
        url = reverse("rbac-grant-waiting-list", kwargs={"tenant_slug": slug})
        return f"{url}?{urlencode({'tenant': slug, **params})}"

    def _ids(self, response):
        return {row["id"] for row in response.data["data"]}

    def test_lists_the_roles_waiting_grants_with_their_reach(self):
        resp = _client(self.okafor).get(self._url(role="assistant-bursar"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self._ids(resp), {self.tunde_waits.id, self.ada_waits.id})
        ada = next(row for row in resp.data["data"] if row["id"] == self.ada_waits.id)
        self.assertEqual(ada["user_id"], self.ada.id)
        self.assertEqual(ada["branch_id"], self.lekki.id)
        self.assertEqual(ada["role_key"], "assistant-bursar")
        self.assertEqual(ada["status"], "PENDING")

    def test_names_no_reason_and_no_requester(self):
        resp = _client(self.okafor).get(self._url(role="assistant-bursar"))

        self.assertEqual(
            set(resp.data["data"][0]),
            {"id", "status", "user_id", "role_key", "branch_id", "submitted_at"},
        )

    def test_filters_by_role_id_and_by_person(self):
        resp = _client(self.okafor).get(self._url(role=self.bursar.id, user=self.tunde.id))

        self.assertEqual(self._ids(resp), {self.tunde_waits.id})

    def test_a_person_that_is_not_an_id_is_refused(self):
        resp = _client(self.okafor).get(self._url(user="tunde"))

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_decided_request_is_not_waiting(self):
        _waiting(
            self.tenant, requested_by=self.okafor, user=self.plain, role=self.teacher,
            status=TenantRoleGrantRequest.Status.APPROVED,
        )
        _waiting(
            self.tenant, requested_by=self.okafor, user=self.tunde, role=self.teacher,
            status=TenantRoleGrantRequest.Status.DENIED,
        )

        resp = _client(self.okafor).get(self._url(role="teacher"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), set())

    def test_a_branch_bound_reader_sees_only_people_in_their_branches(self):
        resp = _client(self.bello).get(self._url(role="assistant-bursar"))

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self._ids(resp), {self.ada_waits.id})

    def test_somebody_who_may_not_see_roles_is_refused(self):
        resp = _client(self.plain).get(self._url(role="assistant-bursar"))

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_schools_waiting_grants_stay_out_of_reach(self):
        other = make_school(slug="greenfield-waiting", name="Greenfield School")
        other_branch = make_branch(other, name="Ikeja Branch")
        other_admin = make_school_admin(other_branch, email="admin@greenfield-waiting.test")
        other_role = make_role(other.tenant, name="Assistant Bursar", key="assistant-bursar")
        theirs = _waiting(
            other.tenant, requested_by=other_admin,
            user=make_staff_user(other_branch, email="kemi@greenfield-waiting.test"),
            role=other_role,
        )

        own = _client(self.okafor).get(self._url(role="assistant-bursar"))
        foreign = _client(self.okafor).get(self._url(slug=other.tenant.slug))

        self.assertNotIn(theirs.id, self._ids(own))
        self.assertIn(foreign.status_code, {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND})

    def test_a_grant_sent_for_approval_appears_in_the_list(self):
        restricted = make_permission(
            "payments.payout.create", is_restricted=True,
            sensitivity_level=Permission.Sensitivity.CRITICAL,
        )
        make_role_permission(self.teacher, restricted)
        url = reverse("rbac-assignment-list-create", kwargs={"tenant_slug": self.slug})

        sent = _client(self.okafor).post(
            f"{url}?tenant={self.slug}",
            {"user": self.plain.id, "role": self.teacher.id},
            format="json",
        )
        resp = _client(self.okafor).get(self._url(role="teacher"))

        self.assertEqual(sent.status_code, status.HTTP_202_ACCEPTED, sent.data)
        self.assertEqual(self._ids(resp), {sent.data["data"]["id"]})
        self.assertEqual(resp.data["data"][0]["user_id"], self.plain.id)
