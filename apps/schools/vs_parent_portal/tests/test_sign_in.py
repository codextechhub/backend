"""A parent who signs in on the school's own page is sent to the parent app.

Mrs. Adaeze Nwosu is a parent at Brightfield with no staff role, so her sign-in
answers ``device_app`` ``/parent/`` (the parent app's device session), and so
do the pages that set her password.
Brightfield's administrator is a guardian too, of her niece, but holds a staff
role, so she keeps the staff app. A member of staff who is nobody's parent is
sent nowhere new, and neither is a parent of another school's child.

The administrator is offered the parent app beside the staff app, as one of
her ``other_apps``, on sign-in and on every ``me/``. Mrs. Nwosu is not, since
the parent app is already her home, and neither is a teacher whose only
child has left the roll, since the parent app would have nothing to show.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework.test import APIClient

from schools.vs_students.constants import Relationship
from vs_rbac.tests.helpers import make_school_admin

from ..sign_in import parent_app_beside_staff, parent_home_app
from .base import ParentFixture

LOGIN_URL = "/v1/user/auth/login/"
PASSWORD = "testpass123"


class _SignInFixture(ParentFixture):
    """The shared parents, plus an aunt on the staff, a stranger and a teacher whose child has left."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        aunt = cls.parent(cls.admin, "Ngozi", "Admin")
        cls.link(cls, cls.zainab, aunt, primary=False, relationship=Relationship.OTHER)
        cls.stranger = make_school_admin(
            None, email="nobodys.parent@example.ng", tenant=cls.tenant,
            first_name="Kunle", last_name="Ojo",
        )
        cls.former_parent = make_school_admin(
            None, email="former.parent@example.ng", tenant=cls.tenant,
            first_name="Tayo", last_name="Bello",
        )
        former = cls.parent(cls.former_parent, "Tayo", "Bello")
        cls.link(cls, cls.left, former, primary=False, relationship=Relationship.FATHER)

    def sign_in(self, email):
        cache.clear()
        return APIClient().post(
            LOGIN_URL, {"identifier": email, "password": PASSWORD, "tenant": "brightfield"},
            format="json",
        )


class ParentSignInTests(_SignInFixture):
    def test_a_parent_is_sent_to_the_parent_app(self):
        response = self.sign_in(self.nwosu_user.email)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["device_app"], {"path": "/parent/"})
        self.assertIsNone(data["home_app"])

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
                self.assertEqual(serializer(self.nwosu_user).data["device_app"], {"path": "/parent/"})
                self.assertIsNone(serializer(self.nwosu_user).data["home_app"])
                self.assertIsNone(serializer(self.admin).data["device_app"])
                self.assertIsNone(serializer(self.admin).data["home_app"])


PARENT_APP = [{"key": "parent_app", "path": "/parent/"}]


class ParentAppBesideStaffTests(_SignInFixture):
    """The staff app offers the parent app to a member of staff who is a parent too."""

    def me(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.get("/v1/user/auth/me/", {"tenant": "brightfield"})

    def test_a_member_of_staff_who_is_a_parent_is_offered_the_parent_app(self):
        response = self.sign_in(self.admin.email)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["other_apps"], PARENT_APP)
        me = self.me(self.admin)
        self.assertEqual(me.status_code, 200, me.data)
        self.assertEqual(me.data["data"]["other_apps"], PARENT_APP)

    def test_a_parent_whose_home_is_the_parent_app_is_not_offered_it_again(self):
        response = self.sign_in(self.nwosu_user.email)
        self.assertEqual(response.data["data"]["other_apps"], [])
        self.assertIsNone(parent_app_beside_staff(self.nwosu_user))

    def test_a_member_of_staff_who_is_nobodys_parent_is_offered_nothing(self):
        self.assertEqual(self.me(self.stranger).data["data"]["other_apps"], [])

    def test_a_member_of_staff_whose_children_have_left_is_offered_nothing(self):
        self.assertIsNone(parent_app_beside_staff(self.former_parent))
        self.assertEqual(self.me(self.former_parent).data["data"]["other_apps"], [])
