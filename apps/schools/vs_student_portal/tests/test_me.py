"""Who the signed-in pupil is, and who is turned away from the portal.

Chiamaka is a pupil at Brightfield's Lekki branch (two branches, so the branch
shows); Bola is a pupil at Sunrise (one branch, so it recedes). A member of
staff opening a portal address gets 404, as does a pupil whose access was taken
away.
"""
from __future__ import annotations

from schools.vs_students.constants import Relationship
from schools.vs_students.services import portal_access
from schools.vs_students.tests.base import StudentsFixture
from vs_user.models import User

ME = "student-portal-me"


class MeTests(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chiamaka = cls.student(cls, number="BFS/2025/0142", email="chiamaka.n@example.ng")
        cls.link(
            cls, cls.chiamaka,
            cls.guardian(cls, name="Mrs. Adaeze Nwosu", email="adaeze.nwosu@example.ng"),
            relationship=Relationship.MOTHER,
        )
        cls.place(cls, cls.chiamaka)
        portal_access.issue(cls.chiamaka, actor=cls.admin)

        cls.bola = cls.student(
            cls, first="Bola", last="Ade", number="SUN/0001",
            tenant=cls.solo.tenant, branch=cls.solo_branch,
            email="bola@example.ng",
        )
        portal_access.issue(cls.bola, actor=cls.solo_admin)

    def setUp(self):
        for pupil in (self.chiamaka, self.bola):
            pupil.refresh_from_db()
            user = pupil.user
            user.status = User.Status.ACTIVE
            user.is_active = True
            user.save(update_fields=["status", "is_active", "updated_at"])

    def test_a_linked_pupil_sees_themselves(self):
        response = self.get(self.chiamaka.user, ME)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["student"]["full_name"], "Chiamaka Nwosu")
        self.assertEqual(data["student"]["initials"], "CN")
        self.assertEqual(data["student"]["admission_number"], "BFS/2025/0142")
        self.assertEqual(data["student"]["class_name"], "JSS1 A")
        self.assertEqual(data["session"]["name"], "2025/2026")

    def test_a_two_branch_school_shows_the_branch(self):
        data = self.get(self.chiamaka.user, ME).data["data"]
        self.assertTrue(data["show_branch"])
        self.assertEqual(data["branch"]["name"], "Lekki")

    def test_a_one_branch_school_leaves_the_branch_out(self):
        data = self.get(self.bola.user, ME).data["data"]
        self.assertFalse(data["show_branch"])
        self.assertNotIn("branch", data)

    def test_staff_get_404(self):
        response = self.get(self.admin, ME)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_A_STUDENT")

    def test_a_revoked_pupil_is_turned_away(self):
        portal_access.revoke(self.chiamaka, actor=self.admin)
        self.chiamaka.user.refresh_from_db()
        response = self.get(self.chiamaka.user, ME)
        self.assertIn(response.status_code, (401, 403, 404))

    def test_a_pupil_cannot_use_their_token_at_another_school(self):
        client = self.client_for(self.chiamaka.user)
        from django.urls import reverse

        response = client.get(reverse(ME), {"tenant": "sunrise"})
        self.assertNotEqual(response.status_code, 200)

    def test_a_pupil_holds_no_staff_route(self):
        response = self.get(self.chiamaka.user, "student-list")
        self.assertEqual(response.status_code, 403)
