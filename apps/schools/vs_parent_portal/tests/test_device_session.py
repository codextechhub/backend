"""A parent stays signed in to the parent app for weeks, on a device.

Mrs. Adaeze Nwosu is a parent at Brightfield with no staff role. She is offered
a device session in the parent app, lasting as long as Brightfield's
``parents.portal.session_days`` says, so the parent store app can sign her in
and she stops signing in again every day. Brightfield's administrator is a
parent too, of her niece Zainab, and keeps the staff app: she can also sign in
for a device from the parent app, but her browser sign-in is not sent there
and her staff pushes still open the staff app. A member of staff who is nobody's
parent is offered no parent app, and a pupil keeps the student app and the
student setting. Sunrise has one branch and its head is a parent too, so the
rules are checked at both shapes of school.
"""
from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APIClient

from schools.vs_students.constants import Relationship
from schools.vs_students.models import Student
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_notifications.models import PushSubscription
from vs_notifications.notify import send_notification
from vs_notifications.services import push
from vs_rbac.tests.helpers import make_assignment, make_school_admin
from vs_user.models import LoginSession
from vs_user.services import device_sessions
from vs_user.test_browser_session import BROWSER_ORIGIN_REGEXES, BROWSER_ORIGINS, HARDENED_COOKIE

from ..sign_in import parent_device_session, portal_session_days
from .base import ParentFixture

LOGIN = "/v1/user/auth/login/"
PASSWORD = "testpass123"
NATIVE = "capacitor://localhost"
SCHOOL_PAGE = "http://localhost:5174"
DAY = 86400
PARENT_KEY = "parents.portal.session_days"
PUPIL_KEY = "students.portal.session_days"
KEYS = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": "BPublicKeyForTests",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "privateKeyForTests",
    "WEB_PUSH_CONTACT": "mailto:dev@example.com",
}
NOTICE = {"notice_title": "Inter-house sports", "notice_excerpt": "Friday.",
          "notice_body": "Friday.", "audience_label": "JSS1 A", "tenant_name": "Brightfield"}


@override_settings(
    AUTH_BROWSER_ALLOWED_ORIGINS=BROWSER_ORIGINS + [NATIVE],
    AUTH_BROWSER_ALLOWED_ORIGIN_REGEXES=BROWSER_ORIGIN_REGEXES,
    NATIVE_APP_ORIGINS=[NATIVE],
    CSRF_TRUSTED_ORIGINS=BROWSER_ORIGINS + ["http://*.localhost:5174"],
    AUTH_DEVICE_COOKIE_PREFIX="device_session_",
    AUTH_DEVICE_COOKIE_PATH="/v1/user/auth/",
    **HARDENED_COOKIE,
    **KEYS,
)
class _DeviceFixture(ParentFixture):
    """The shared parents, plus teacher-parents at both schools and a pupil who is also a guardian."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        aunt = cls.parent(cls.admin, "Ngozi", "Admin")
        cls.link(cls, cls.zainab, aunt, primary=False, relationship=Relationship.OTHER)
        cls.solo_head_guardian = cls.parent(
            cls.solo_admin, "Head", "Sunrise", tenant=cls.solo.tenant,
        )
        cls.link(cls, cls.bola, cls.solo_head_guardian, primary=False, relationship=Relationship.OTHER)
        cls.stranger = make_school_admin(
            None, email="nobodys.parent@example.ng", tenant=cls.tenant,
            first_name="Kunle", last_name="Ojo",
        )
        make_assignment(cls.school, cls.stranger, cls.role, branch=None)
        cls.pupil_user = make_school_admin(
            None, email="chiamaka.pupil@example.ng", tenant=cls.tenant,
            first_name="Chiamaka", last_name="Nwosu",
        )
        Student.all_objects.filter(pk=cls.chiamaka.pk).update(user=cls.pupil_user)
        pupil_as_guardian = cls.parent(cls.pupil_user, "Chiamaka", "Nwosu")
        cls.link(cls, cls.tobi, pupil_as_guardian, primary=False, relationship=Relationship.OTHER)

    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)

    def set_days(self, key, days, tenant=None):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=key), value=days,
            actor=self.admin, tenant=tenant or self.tenant, reason="Test",
        )

    def sign_in(self, user, *, origin=SCHOOL_PAGE, session=None):
        cache.clear()
        body = {"identifier": user.email, "password": PASSWORD, "tenant": user.tenant.slug}
        if session:
            body["session"] = session
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return self.client.post(LOGIN, body, format="json", **extra)

    def native_sign_in(self, user):
        response = self.sign_in(user, origin=NATIVE, session="device")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def lifetime_days(self, refresh):
        return round(device_sessions.cookie_max_age(refresh) / DAY)


class ParentOnlyDeviceSessionTests(_DeviceFixture):
    def test_a_parents_browser_sign_in_names_the_parent_app(self):
        response = self.sign_in(self.nwosu_user)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["device_app"], {"path": "/parent/"})
        self.assertIsNone(data["home_app"])
        self.assertEqual(data["other_apps"], [])

    def test_a_parents_device_sign_in_lasts_thirty_days_by_default(self):
        response = self.sign_in(self.nwosu_user, session="device")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        cookie = response.cookies[f"device_session_{data['account']}"]
        self.assertGreater(int(cookie["max-age"]), 29 * DAY)
        self.assertLessEqual(int(cookie["max-age"]), 30 * DAY)

    def test_the_schools_setting_decides_how_long_and_applies_to_that_school_only(self):
        self.set_days(PARENT_KEY, 7)
        self.assertEqual(self.lifetime_days(self.native_sign_in(self.nwosu_user)["refresh"]), 7)
        self.assertEqual(self.lifetime_days(self.native_sign_in(self.ade_user)["refresh"]), 30)

    def test_the_lifetime_is_read_again_on_each_refresh(self):
        data = self.native_sign_in(self.nwosu_user)
        self.set_days(PARENT_KEY, 3)
        refreshed = self.client.post(
            "/v1/user/auth/device/refresh/",
            {"account": data["account"], "refresh": data["refresh"]},
            format="json", HTTP_ORIGIN=NATIVE,
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        self.assertEqual(self.lifetime_days(refreshed.json()["data"]["refresh"]), 3)

    def test_a_native_sign_in_returns_the_credential_in_the_body(self):
        response = self.sign_in(self.nwosu_user, origin=NATIVE, session="device")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["refresh"])
        self.assertEqual(list(response.cookies), [])

    def test_a_parent_at_a_one_branch_school_is_offered_the_same(self):
        self.assertEqual(
            self.sign_in(self.ade_user).json()["data"]["device_app"], {"path": "/parent/"},
        )
        self.assertEqual(self.lifetime_days(self.native_sign_in(self.ade_user)["refresh"]), 30)

    def test_a_setting_outside_one_to_ninety_falls_back_to_thirty(self):
        for stored in (0, 91, 7.5, "week", True, None):
            with self.subTest(stored=stored), mock.patch(
                "schools.vs_students.services.rules.resolve_many",
                return_value={PARENT_KEY: (stored, None)},
            ):
                self.assertEqual(portal_session_days(self.tenant), 30)

    def test_a_setting_inside_the_range_is_used(self):
        for stored in (1, 45, 90):
            with self.subTest(stored=stored), mock.patch(
                "schools.vs_students.services.rules.resolve_many",
                return_value={PARENT_KEY: (stored, None)},
            ):
                self.assertEqual(portal_session_days(self.tenant), stored)


class TeacherParentDeviceSessionTests(_DeviceFixture):
    """Brightfield's administrator keeps the staff app and can still use the parent app on a device."""

    def test_their_browser_sign_in_is_not_sent_to_the_parent_app(self):
        data = self.sign_in(self.admin).json()["data"]
        self.assertIsNone(data["device_app"])
        self.assertIsNone(data["home_app"])
        self.assertEqual(data["other_apps"], [{"key": "parent_app", "path": "/parent/"}])

    def test_the_same_at_a_one_branch_school(self):
        data = self.sign_in(self.solo_admin).json()["data"]
        self.assertIsNone(data["device_app"])
        self.assertEqual(data["other_apps"], [{"key": "parent_app", "path": "/parent/"}])

    def test_they_can_sign_in_for_a_device_with_the_schools_lifetime(self):
        self.set_days(PARENT_KEY, 14)
        response = self.sign_in(self.admin, session="device")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("account", response.json()["data"])
        self.assertEqual(self.lifetime_days(self.native_sign_in(self.admin)["refresh"]), 14)

    def test_they_can_sign_in_from_the_native_parent_app_at_a_one_branch_school(self):
        data = self.native_sign_in(self.solo_admin)
        self.assertEqual(self.lifetime_days(data["refresh"]), 30)

    def test_their_activation_page_does_not_send_them_to_the_parent_app(self):
        from vs_user.serializers import ActivationPreviewSerializer

        shown = ActivationPreviewSerializer(self.admin).data
        self.assertIsNone(shown["device_app"])
        self.assertIsNone(shown["home_app"])
        parent = ActivationPreviewSerializer(self.nwosu_user).data
        self.assertEqual(parent["device_app"], {"path": "/parent/"})
        self.assertIsNone(parent["home_app"])

    def test_a_member_of_staff_whose_only_child_has_left_is_offered_no_device_session(self):
        left = make_school_admin(
            None, email="former.parent@example.ng", tenant=self.tenant,
            first_name="Tayo", last_name="Bello",
        )
        make_assignment(self.school, left, self.role, branch=None)
        guardian = self.parent(left, "Tayo", "Bello")
        self.link(self.left, guardian, primary=False, relationship=Relationship.FATHER)
        self.assertIsNone(parent_device_session(left))


class NotOfferedTests(_DeviceFixture):
    def test_a_member_of_staff_with_no_child_is_refused_a_device_session(self):
        response = self.sign_in(self.stranger, session="device")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "DEVICE_SESSION_NOT_OFFERED")
        self.assertFalse(LoginSession.all_objects.filter(user=self.stranger).exists())

    def test_a_native_sign_in_by_a_member_of_staff_with_no_child_is_refused(self):
        response = self.sign_in(self.stranger, origin=NATIVE, session="device")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "DEVICE_SESSION_NOT_OFFERED")

    def test_a_member_of_staff_with_no_child_keeps_the_staff_sign_in(self):
        data = self.sign_in(self.stranger).json()["data"]
        self.assertIsNone(data["device_app"])
        self.assertEqual(data["other_apps"], [])
        self.assertIsNone(parent_device_session(self.stranger))


class PupilKeepsTheStudentAppTests(_DeviceFixture):
    """Chiamaka's account is a pupil's and, by an oversight, a guardian's too: the pupil policy answers."""

    def test_the_pupil_policy_is_the_one_in_force(self):
        self.set_days(PARENT_KEY, 7)
        self.set_days(PUPIL_KEY, 21)
        self.assertIsNone(parent_device_session(self.pupil_user))
        policy = device_sessions.device_session_policy(self.pupil_user)
        self.assertEqual(policy.app_path, "/student/")
        self.assertEqual(policy.lifetime.days, 21)
        self.assertTrue(policy.sends_browser_sign_in_to_app)

    def test_the_pupils_browser_sign_in_names_the_student_app(self):
        data = self.sign_in(self.pupil_user).json()["data"]
        self.assertEqual(data["device_app"], {"path": "/student/"})

    def test_the_pupils_device_session_lasts_the_student_setting(self):
        self.set_days(PARENT_KEY, 7)
        self.set_days(PUPIL_KEY, 21)
        self.assertEqual(self.lifetime_days(self.native_sign_in(self.pupil_user)["refresh"]), 21)

    def test_a_pupil_who_is_not_a_guardian_is_unchanged(self):
        self.assertEqual(device_sessions.device_session_policy(self.pupil_user).app_path, "/student/")


class ParentPushTests(_DeviceFixture):
    """Where a push opens: the staff app on a staff browser, the parent app on the parent device."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)
        cls.parent_device = LoginSession.all_objects.create(
            user=cls.admin, tenant=cls.tenant, device_key="b" * 32, is_active=True,
        )
        cls.nwosu_device = LoginSession.all_objects.create(
            user=cls.nwosu_user, tenant=cls.tenant, device_key="c" * 32, is_active=True,
        )
        for user, endpoint, key in (
            (cls.admin, "https://push.example.com/admin-staff", ""),
            (cls.admin, "https://push.example.com/admin-parent", "b" * 32),
            (cls.nwosu_user, "https://push.example.com/nwosu-parent", "c" * 32),
            (cls.nwosu_user, "https://push.example.com/nwosu-browser", ""),
        ):
            PushSubscription.all_objects.create(
                tenant=cls.tenant, user=user, endpoint=endpoint,
                endpoint_hash=push.endpoint_hash(endpoint), p256dh="k", auth="a", device_key=key,
            )

    def opens(self, user):
        with mock.patch("pywebpush.webpush") as webpush, self.captureOnCommitCallbacks(execute=True):
            send_notification(
                event_key="notices.published", context=NOTICE,
                recipients=[user], tenant=self.tenant,
            )
        return {
            call.kwargs["subscription_info"]["endpoint"].rsplit("/", 1)[1]:
                json.loads(call.kwargs["data"])["url"]
            for call in webpush.call_args_list
        }

    def test_a_teacher_parents_staff_push_opens_the_staff_app_and_the_parent_device_opens_the_parent_app(self):
        self.assertEqual(
            self.opens(self.admin), {"admin-staff": "/", "admin-parent": "/parent/"},
        )

    def test_a_parent_only_account_opens_the_parent_app_on_its_device_session_device(self):
        self.assertEqual(
            self.opens(self.nwosu_user), {"nwosu-parent": "/parent/", "nwosu-browser": "/"},
        )

    def test_a_staff_member_who_is_nobodys_parent_opens_the_root(self):
        PushSubscription.all_objects.create(
            tenant=self.tenant, user=self.stranger, endpoint="https://push.example.com/kunle",
            endpoint_hash=push.endpoint_hash("https://push.example.com/kunle"),
            p256dh="k", auth="a", device_key="",
        )
        self.assertEqual(self.opens(self.stranger), {"kunle": "/"})
