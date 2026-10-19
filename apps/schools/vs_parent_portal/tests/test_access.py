"""Who the parent portal admits, and what it says to everybody else.

Mrs. Nwosu reaches her own children and nobody else's. A member of staff with
no guardian record, a parent of another school, a school that has not gone
live and a child who has left are each turned away, and a child that is not
hers answers exactly as a child that does not exist.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Relationship, StudentStatus
from schools.vs_students.models import Guardian
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin

from .base import ParentFixture

ME, CHILD = "parent-portal-me", "parent-portal-child"


class ParentAdmissionTests(ParentFixture):
    def child_url(self, pupil):
        return reverse(CHILD, kwargs={"child_id": pupil.pk})

    def test_a_member_of_staff_with_no_guardian_record_gets_404(self):
        response = TenantAPIClient(self.admin).get(reverse(ME))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_A_PARENT")

    def test_a_staff_member_gets_404_on_a_child_route_too(self):
        response = TenantAPIClient(self.admin).get(self.child_url(self.chiamaka))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_A_PARENT")

    def test_no_credentials_is_refused(self):
        from rest_framework.test import APIClient

        response = APIClient().get(reverse(ME), {"tenant": self.tenant.slug})
        self.assertIn(response.status_code, (401, 403))

    def test_another_familys_child_and_a_missing_child_answer_the_same(self):
        client = TenantAPIClient(self.nwosu_user)
        theirs = client.get(self.child_url(self.zainab))
        missing = client.get(reverse(CHILD, kwargs={"child_id": 9_999_999}))
        self.assertEqual(theirs.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(theirs.content, missing.content)

    def test_every_child_route_refuses_another_familys_child_alike(self):
        client = TenantAPIClient(self.nwosu_user)
        for name in (
            "parent-portal-child", "parent-portal-timetable", "parent-portal-attendance",
            "parent-portal-results", "parent-portal-results-session",
            "parent-portal-report-card",
        ):
            with self.subTest(route=name):
                theirs = client.get(reverse(name, kwargs={"child_id": self.zainab.pk}))
                missing = client.get(reverse(name, kwargs={"child_id": 9_999_999}))
                self.assertEqual((theirs.status_code, missing.status_code), (404, 404))
                self.assertEqual(theirs.content, missing.content)

    def test_a_parent_of_one_school_cannot_assert_another(self):
        client = TenantAPIClient(self.nwosu_user, tenant_slug=self.solo.slug)
        response = client.get(reverse(ME))
        self.assertEqual(response.status_code, 404)

    def test_a_school_that_has_not_gone_live_is_closed(self):
        pending = make_school(slug="pending-school", name="Pending School", status="PENDING")
        branch = make_branch(pending, name="Main", is_main=True)
        user = make_school_admin(None, email="p@pending.test", tenant=pending.tenant)
        pupil = self.student(
            first="Kemi", last="Pend", number="P/1", tenant=pending.tenant, branch=branch,
        )
        guardian = Guardian.all_objects.create(
            tenant=pending.tenant, first_name="Pat", last_name="Pend",
            phone="08110000001", user=user,
        )
        self.link(pupil, guardian)
        response = TenantAPIClient(user).get(reverse(ME))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_a_teacher_whose_own_child_attends_is_admitted_like_any_parent(self):
        teacher = self.lekki_head
        guardian = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Head", last_name="Teacher",
            phone="08110000002", user=teacher,
        )
        ife = self.child("Ife", "Teacher", "BFS/201", self.lekki, self.lekki_class, dob=(2014, 3, 3))
        self.link(ife, guardian, relationship=Relationship.FATHER)

        client = TenantAPIClient(teacher)
        me = client.get(reverse(ME))
        self.assertEqual(me.status_code, 200, me.data)
        self.assertEqual([c["first_name"] for c in me.data["data"]["children"]], ["Ife"])
        self.assertEqual(client.get(self.child_url(ife)).status_code, 200)
        self.assertEqual(client.get(reverse("student-list")).status_code, 200)

    def test_a_child_who_has_left_is_not_listed_and_answers_404(self):
        client = TenantAPIClient(self.nwosu_user)
        names = [c["first_name"] for c in client.get(reverse(ME)).data["data"]["children"]]
        self.assertNotIn("Dayo", names)
        self.assertEqual(client.get(self.child_url(self.left)).status_code, 404)

    def test_a_suspended_child_is_still_served(self):
        self.chiamaka.status = StudentStatus.SUSPENDED
        self.chiamaka.save(update_fields=["status"])
        self.assertEqual(
            TenantAPIClient(self.nwosu_user).get(self.child_url(self.chiamaka)).status_code, 200,
        )

    def test_unlinking_the_account_ends_access_on_the_next_request(self):
        client = TenantAPIClient(self.yusuf_user)
        self.assertEqual(client.get(reverse(ME)).status_code, 200)
        Guardian.all_objects.filter(pk=self.yusuf.pk).update(user=None)
        response = client.get(reverse(ME))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_A_PARENT")

    def test_a_guardian_with_no_child_on_the_roll_sees_an_empty_list(self):
        self.zainab.status = StudentStatus.WITHDRAWN
        self.zainab.save(update_fields=["status"])
        data = TenantAPIClient(self.yusuf_user).get(reverse(ME)).data["data"]
        self.assertEqual(data["children"], [])

    def test_the_session_and_term_are_the_schools_current_ones(self):
        data = TenantAPIClient(self.nwosu_user).get(reverse(ME)).data["data"]
        self.assertEqual(data["session"]["name"], "2025/2026")
        self.assertEqual(data["parent"]["initials"], "AN")
        self.assertEqual(data["parent"]["first_name"], "Adaeze")
        self.assertIn("name", data["school"])
