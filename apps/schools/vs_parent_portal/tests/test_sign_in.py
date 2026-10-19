"""A parent who signs in on the school's own page is sent to the parent app.

Mrs. Adaeze Nwosu is a parent at Brightfield with no staff role, so her sign-in
answers ``home_app`` ``/parent/``, and so do the pages that set her password.
Brightfield's administrator is a guardian too, of her niece, but holds a staff
role, so she keeps the staff app. A member of staff who is nobody's parent is
sent nowhere new, and neither is a parent of another school's child.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework.test import APIClient

from schools.vs_students.constants import Relationship
from vs_rbac.tests.helpers import make_school_admin

from ..sign_in import parent_home_app
from .base import ParentFixture

LOGIN_URL = "/v1/user/auth/login/"
PASSWORD = "testpass123"


class ParentSignInTests(ParentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        aunt = cls.parent(cls.admin, "Ngozi", "Admin")
        cls.link(cls, cls.zainab, aunt, primary=False, relationship=Relationship.OTHER)
        cls.stranger = make_school_admin(
            None, email="nobodys.parent@example.ng", tenant=cls.tenant,
            first_name="Kunle", last_name="Ojo",
        )

    def sign_in(self, email):
        cache.clear()
        return APIClient().post(
            LOGIN_URL, {"identifier": email, "password": PASSWORD, "tenant": "brightfield"},
            format="json",
        )

    def test_a_parent_is_sent_to_the_parent_app(self):
        response = self.sign_in(self.nwosu_user.email)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["home_app"], {"path": "/parent/"})
        self.assertIsNone(data["device_app"])

    def test_a_member_of_staff_who_is_also_a_parent_keeps_the_staff_app(self):
        self.assertIsNone(parent_home_app(self.admin))

    def test_somebody_who_is_nobodys_parent_is_sent_nowhere_new(self):
        self.assertIsNone(parent_home_app(self.stranger))
        response = self.sign_in(self.stranger.email)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["home_app"])

    def test_a_parent_at_another_school_is_a_parent_there_only(self):
        self.assertEqual(parent_home_app(self.ade_user), "/parent/")
        self.assertEqual(self.ade_user.tenant_id, self.solo.tenant.pk)

    def test_the_pages_that_set_a_password_send_a_parent_on_too(self):
        from vs_user.serializers import ActivationPreviewSerializer, PasswordResetPreviewSerializer

        for serializer in (ActivationPreviewSerializer, PasswordResetPreviewSerializer):
            with self.subTest(page=serializer.__name__):
                self.assertEqual(serializer(self.nwosu_user).data["home_app"], {"path": "/parent/"})
                self.assertIsNone(serializer(self.admin).data["home_app"])
