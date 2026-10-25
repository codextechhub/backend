"""Push notifications to phones, through Firebase Cloud Messaging.

Tunde and his sister Bola are signed in on their mother's phone in the parent
app, each with their own device session. Both register the phone's Firebase
token, so the phone holds two devices, one per child. A notice published for
Tunde reaches his device only, with his first name in front and his account in
the data, so tapping it opens the app as him. Signing Tunde out of the phone
removes his devices and leaves Bola's. Bola cannot remove Tunde's device even
holding the same token, and a parent at another school who happens to register
the same token gets a device of their own that the first school never sees.
An account keeps at most four phones: when Tunde registers a fifth, the one he
used least recently is dropped, and Bola's rows on the same token are never
counted or touched. A token Firebase has forgotten is deleted; a Firebase outage or a refusal for
any other reason keeps it, and the family's other devices still get the push.
Firebase and Google are replaced by one patched function, so nothing here
touches the network, while the access-token signing runs for real against a
throwaway key.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vs_user.models import LoginSession, User

from .constants import ChannelChoices, NotificationStatus
from .models import NativePushDevice, Notification, PushSubscription
from .notify import send_notification
from .services import fcm, native_push, push
from .tests import _NotifFixture

TOKEN_URL = fcm.TOKEN_URL
SEND_URL = fcm.SEND_URL.format(project_id="xvs-test-project")
CLIENT_EMAIL = "push@xvs-test-project.iam.gserviceaccount.com"
TUNDE_PHONE = "fcm-token-for-the-family-phone"
WEB_PUSH = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": "BPublicKeyForTests",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "privateKeyForTests",
    "WEB_PUSH_CONTACT": "mailto:dev@example.com",
}
WEBPUSH = "pywebpush.webpush"
NOTICE = {"notice_title": "Inter-house sports", "notice_excerpt": "Friday.",
          "notice_body": "Friday.", "audience_label": "JSS1 A", "tenant_name": "Alpha"}
UNREGISTERED = {"error": {"code": 404, "status": "NOT_FOUND", "details": [
    {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"},
]}}


def device_body(token=TUNDE_PHONE, **extra):
    body = {"token": token, "platform": "android", "app": "com.codexng.xvs.parent"}
    body.update(extra)
    return body


def _service_account_json(private_pem: str) -> str:
    return json.dumps({
        "type": "service_account", "project_id": "xvs-test-project",
        "client_email": CLIENT_EMAIL, "private_key": private_pem,
    })


class _PhoneFixture(_NotifFixture):
    """Two children and a parent at Alpha, a parent at Beta, and a throwaway Firebase key."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        cls.public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        cls.key_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.key_dir.cleanup)
        cls.key_path = str(Path(cls.key_dir.name) / "service-account.json")
        Path(cls.key_path).write_text(_service_account_json(cls.private_pem), encoding="utf-8")

        cls.tunde = cls.plain_a
        cls.tunde.first_name = "Tunde"
        cls.tunde.save(update_fields=["first_name"])
        cls.bola = cls.admin_a
        cls.bola.first_name = "Bola"
        cls.bola.save(update_fields=["first_name"])
        cls.tunde_session = LoginSession.all_objects.create(
            user=cls.tunde, tenant=cls.tunde.tenant, device_key="a" * 32, is_active=True,
        )
        cls.bola_session = LoginSession.all_objects.create(
            user=cls.bola, tenant=cls.bola.tenant, device_key="b" * 32, is_active=True,
        )
        cls.chidi = User.objects.create_user(
            email="chidi@beta.test", password="x", status="ACTIVE",
            first_name="Chidi", last_name="Parent", tenant=cls.school_b.tenant,
        )

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            FCM_SERVICE_ACCOUNT_FILE=self.key_path,
            WEB_PUSH_VAPID_PUBLIC_KEY="", WEB_PUSH_VAPID_PRIVATE_KEY="",
        ))
        fcm.reset_caches()
        cache.clear()
        self.addCleanup(fcm.reset_caches)
        self.addCleanup(cache.clear)
        self.token_calls = []
        self.sent = []
        self.answers = {}
        patcher = mock.patch.object(fcm, "_request", side_effect=self.fake_google)
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def fake_google(self, method, url, *, headers, body=None, form=None):
        """Google's token endpoint, which checks the assertion, and Firebase's send endpoint."""
        if url == TOKEN_URL:
            claims = jwt.decode(
                form["assertion"], self.public_pem, algorithms=["RS256"], audience=TOKEN_URL,
            )
            self.token_calls.append({"form": form, "claims": claims})
            return 200, {"access_token": f"access-{len(self.token_calls)}", "expires_in": 3600}
        self.assertEqual(url, SEND_URL)
        self.sent.append({"headers": headers, "body": body["message"]})
        outcome = self.answers.get(body["message"]["token"], (200, {"name": "projects/x/messages/1"}))
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def devices(self, user):
        return NativePushDevice.all_objects.filter(user=user)

    def register(self, user=None, **kwargs):
        return self._client(user or self.tunde).post(
            "/v1/notify/push/devices/", device_body(**kwargs), format="json",
        )

    def unregister(self, user, token=TUNDE_PHONE):
        return self._client(user).delete(
            "/v1/notify/push/devices/", {"token": token}, format="json",
        )

    def notify(self, *recipients):
        with self.captureOnCommitCallbacks(execute=True):
            send_notification(
                event_key="notices.published", context=NOTICE,
                recipients=list(recipients), school=self.school_a,
            )


class NativeDeviceEndpointTests(_PhoneFixture):
    def test_registering_twice_keeps_one_row_and_refreshes_it(self):
        self.assertEqual(self.register(app_version="1.0.0").status_code, 201)
        NativePushDevice.all_objects.update(last_seen_at=timezone.now() - timedelta(days=30))
        self.assertEqual(self.register(app_version="1.1.0").status_code, 201)
        row = self.devices(self.tunde).get()
        self.assertEqual((row.app_version, row.platform, row.tenant_id),
                         ("1.1.0", "android", self.tunde.tenant_id))
        self.assertGreater(row.last_seen_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(row.token_hash, native_push.token_hash(TUNDE_PHONE))

    def test_the_answer_carries_a_message_and_no_device_data(self):
        response = self.register()
        self.assertEqual(response.data["message"], "Notifications are on for this device.")
        self.assertEqual(response.data["data"], {})
        self.assertNotIn(TUNDE_PHONE, json.dumps(response.data))
        deleted = self.unregister(self.tunde)
        self.assertEqual(deleted.data["message"], "Notifications are off for this device.")
        self.assertFalse(self.devices(self.tunde).exists())

    def test_an_anonymous_caller_is_turned_away(self):
        for call in (APIClient().post, APIClient().delete):
            response = call("/v1/notify/push/devices/", device_body(), format="json")
            self.assertIn(response.status_code, (401, 403))
        self.assertFalse(NativePushDevice.all_objects.exists())

    def test_bola_cannot_remove_tundes_phone(self):
        self.register(self.tunde)
        self.unregister(self.bola, TUNDE_PHONE)
        self.assertTrue(self.devices(self.tunde).exists())

    def test_a_parent_at_another_school_gets_a_device_of_their_own(self):
        self.register(self.tunde)
        self.assertEqual(self.register(self.chidi).status_code, 201)
        self.assertEqual(NativePushDevice.all_objects.filter(token_hash=native_push.token_hash(TUNDE_PHONE)).count(), 2)
        self.assertEqual(self.devices(self.chidi).get().tenant_id, self.school_b.tenant_id)

        self.unregister(self.chidi)
        self.assertFalse(self.devices(self.chidi).exists())
        self.assertEqual(self.devices(self.tunde).get().tenant_id, self.school_a.tenant_id)

    def test_an_account_that_is_not_the_callers_session_is_dropped(self):
        self.register(self.bola, account=self.tunde_session.device_key)
        self.assertEqual(self.devices(self.bola).get().device_key, "")

    def test_an_account_the_caller_holds_is_recorded(self):
        self.register(self.tunde, account=self.tunde_session.device_key)
        self.assertEqual(self.devices(self.tunde).get().device_key, self.tunde_session.device_key)

    def test_an_ended_session_is_not_an_account(self):
        LoginSession.all_objects.filter(pk=self.tunde_session.pk).update(is_active=False)
        self.register(self.tunde, account=self.tunde_session.device_key)
        self.assertEqual(self.devices(self.tunde).get().device_key, "")

    def test_bad_requests_are_refused(self):
        cases = {
            "platform": device_body(platform="windows"),
            "empty token": device_body(token=""),
            "long token": device_body(token="t" * 4097),
            "no app": {"token": "t", "platform": "ios"},
            "app without a domain": device_body(app="parent"),
            "app with a space": device_body(app="com.codexng.xvs parent"),
            "app too long": device_body(app="com." + "a" * 100),
        }
        for label, body in cases.items():
            with self.subTest(label):
                response = self._client(self.tunde).post("/v1/notify/push/devices/", body, format="json")
                self.assertEqual(response.status_code, 400)
        self.assertFalse(NativePushDevice.all_objects.exists())

    def test_the_longest_token_is_accepted(self):
        self.assertEqual(self.register(token="t" * 4096, platform="ios").status_code, 201)

    @override_settings(FCM_SERVICE_ACCOUNT_FILE="")
    def test_without_a_firebase_key_registering_is_unavailable(self):
        response = self.register()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "PUSH_NOT_CONFIGURED")
        self.assertFalse(NativePushDevice.all_objects.exists())

    @override_settings(FCM_SERVICE_ACCOUNT_FILE="")
    def test_forgetting_a_phone_still_works_without_a_firebase_key(self):
        NativePushDevice.all_objects.create(
            tenant=self.tunde.tenant, user=self.tunde, platform="android",
            app_id="com.codexng.xvs.parent", token="t", token_hash=native_push.token_hash("t"),
        )
        self.assertEqual(self.unregister(self.tunde, "t").status_code, 200)
        self.assertFalse(self.devices(self.tunde).exists())


class NativeDeviceCapTests(_PhoneFixture):
    CAP = push.MAX_DEVICES_PER_ACCOUNT

    def tokens(self, user):
        return set(self.devices(user).values_list("token", flat=True))

    def names(self, count, start=1):
        return [f"phone-{n}" for n in range(start, start + count)]

    def test_a_phone_past_the_cap_drops_the_least_recently_seen_and_keeps_the_rest(self):
        for token in self.names(self.CAP + 1):
            self.assertEqual(self.register(token=token).status_code, 201)
        self.assertEqual(self.tokens(self.tunde), set(self.names(self.CAP, start=2)))

    def test_registering_an_older_phone_again_refreshes_it(self):
        for token in self.names(self.CAP):
            self.register(token=token)
        self.register(token="phone-1")
        self.register(token="phone-new")
        self.assertEqual(self.tokens(self.tunde), {"phone-1", "phone-new", *self.names(self.CAP - 2, start=3)})

    def test_a_full_set_of_phones_and_browsers_coexist(self):
        for token in self.names(self.CAP):
            self.register(token=token)
        for n in range(self.CAP):
            push.subscribe(self.tunde, endpoint=f"https://push.example.com/{n}", p256dh="k", auth="a")
        self.assertEqual(self.devices(self.tunde).count(), self.CAP)
        self.assertEqual(PushSubscription.all_objects.filter(user=self.tunde).count(), self.CAP)

        self.register(token="phone-new")
        self.assertEqual(self.devices(self.tunde).count(), self.CAP)
        self.assertEqual(PushSubscription.all_objects.filter(user=self.tunde).count(), self.CAP)

    def test_tunde_registering_more_phones_never_touches_bolas_rows_on_the_same_token(self):
        shared = self.names(self.CAP)
        for token in shared:
            self.register(self.bola, token=token)
        for token in shared + self.names(2, start=self.CAP + 1):
            self.register(self.tunde, token=token)
        self.assertEqual(self.tokens(self.bola), set(shared))
        self.assertEqual(self.devices(self.tunde).count(), self.CAP)

    def test_a_surplus_left_by_a_race_is_trimmed_by_the_next_registration(self):
        now = timezone.now()
        for age, token in enumerate(self.names(self.CAP + 1)):
            NativePushDevice.all_objects.create(
                tenant=self.tunde.tenant, user=self.tunde, platform="android",
                app_id="com.codexng.xvs.parent", token=token, token_hash=native_push.token_hash(token),
                last_seen_at=now - timedelta(days=age),
            )
        self.register(token="phone-new")
        self.assertEqual(self.tokens(self.tunde), {"phone-new", *self.names(self.CAP - 1)})


class NativeDeliveryTests(_PhoneFixture):
    def phone(self, user, token=TUNDE_PHONE, account=""):
        return native_push.register(
            user, token=token, platform="android", app_id="com.codexng.xvs.parent",
            device_key=account,
        )

    def test_two_children_on_one_phone_each_get_only_their_own_push(self):
        self.phone(self.tunde, account=self.tunde_session.device_key)
        self.phone(self.bola, account=self.bola_session.device_key)
        self.notify(self.tunde)

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]["body"]
        self.assertEqual(message["notification"]["title"], "Tunde: New notice: Inter-house sports")
        self.assertEqual(message["data"]["account"], self.tunde_session.device_key)
        push_record = Notification.all_objects.get(channel=ChannelChoices.PUSH)
        self.assertEqual((push_record.recipient_id, push_record.status),
                         (self.tunde.pk, NotificationStatus.SENT))

    def test_signing_tunde_out_removes_his_devices_of_both_kinds_only(self):
        from vs_user.services.device_sessions import _end

        key = self.tunde_session.device_key
        self.phone(self.tunde, account=key)
        self.phone(self.bola, account=self.bola_session.device_key)
        push.subscribe(self.tunde, endpoint="https://push.example.com/t", p256dh="k", auth="a", device_key=key)
        push.subscribe(self.bola, endpoint="https://push.example.com/b", p256dh="k", auth="a",
                       device_key=self.bola_session.device_key)
        _end(self.tunde_session, reason="LOGOUT")

        self.assertFalse(self.devices(self.tunde).exists())
        self.assertFalse(PushSubscription.all_objects.filter(user=self.tunde).exists())
        self.assertTrue(self.devices(self.bola).exists())
        self.assertTrue(PushSubscription.all_objects.filter(user=self.bola).exists())

    def test_a_phone_with_no_device_session_names_no_account_and_no_first_name(self):
        self.phone(self.tunde)
        self.notify(self.tunde)
        message = self.sent[0]["body"]
        self.assertEqual(message["notification"]["title"], "New notice: Inter-house sports")
        self.assertNotIn("account", message["data"])

    def test_a_delivered_push_stamps_the_device(self):
        row = self.phone(self.tunde)
        self.assertIsNone(row.last_sent_at)
        self.notify(self.tunde)
        row.refresh_from_db()
        self.assertIsNotNone(row.last_sent_at)

    def test_a_registered_destination_sets_where_the_push_opens_and_its_child(self):
        self.phone(self.tunde)
        resolver = mock.Mock(return_value=push.PushDestination(url="/parent/results?child=7", child=7))
        with mock.patch.object(push, "_DESTINATIONS", [resolver]):
            self.notify(self.tunde)
        data = self.sent[0]["body"]["data"]
        self.assertEqual((data["url"], data["child"]), ("/parent/results?child=7", "7"))

    def test_a_token_firebase_has_forgotten_is_deleted(self):
        for label, answer in (
            ("unregistered", (400, UNREGISTERED)),
            ("not found", (404, {"error": {"code": 404, "status": "NOT_FOUND"}})),
        ):
            with self.subTest(label):
                self.phone(self.tunde)
                self.answers[TUNDE_PHONE] = answer
                self.notify(self.tunde)
                self.assertFalse(self.devices(self.tunde).exists())
                failed = Notification.all_objects.filter(channel=ChannelChoices.PUSH).latest("created_at")
                self.assertEqual(failed.status, NotificationStatus.FAILED)
                Notification.all_objects.all().delete()

    def test_an_outage_or_a_refusal_keeps_the_device(self):
        for label, answer in (
            ("server error", (500, {"error": {"code": 500, "status": "INTERNAL"}})),
            ("bad argument", (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})),
            ("transport", OSError("connection reset")),
        ):
            with self.subTest(label):
                self.phone(self.tunde)
                self.answers[TUNDE_PHONE] = answer
                with self.assertLogs("vs_notifications.fcm", level="WARNING"):
                    self.notify(self.tunde)
                self.assertTrue(self.devices(self.tunde).exists())
                failed = Notification.all_objects.filter(channel=ChannelChoices.PUSH).latest("created_at")
                self.assertEqual(failed.status, NotificationStatus.FAILED)
                Notification.all_objects.all().delete()

    def test_one_device_failing_does_not_stop_the_others(self):
        self.phone(self.tunde, token="dead-token")
        self.phone(self.tunde, token="down-token")
        self.phone(self.tunde, token="good-token")
        self.answers["dead-token"] = (404, UNREGISTERED)
        self.answers["down-token"] = (500, {})
        with mock.patch(WEBPUSH) as webpush, override_settings(**WEB_PUSH), self.assertLogs(
            "vs_notifications.fcm", level="WARNING",
        ):
            push.subscribe(self.tunde, endpoint="https://push.example.com/t", p256dh="k", auth="a")
            self.notify(self.tunde)
        self.assertEqual({s["body"]["token"] for s in self.sent}, {"dead-token", "down-token", "good-token"})
        self.assertEqual(sorted(self.devices(self.tunde).values_list("token", flat=True)),
                         ["down-token", "good-token"])
        self.assertTrue(webpush.called)
        self.assertEqual(Notification.all_objects.get(channel=ChannelChoices.PUSH).status,
                         NotificationStatus.SENT)

    def test_the_browser_still_gets_its_push_when_the_phone_is_refused(self):
        self.phone(self.tunde)
        self.answers[TUNDE_PHONE] = (500, {})
        with mock.patch(WEBPUSH) as webpush, override_settings(**WEB_PUSH), self.assertLogs(
            "vs_notifications.fcm", level="WARNING",
        ):
            push.subscribe(self.tunde, endpoint="https://push.example.com/t", p256dh="k", auth="a")
            self.notify(self.tunde)
        self.assertEqual(webpush.call_count, 1)
        self.assertEqual(Notification.all_objects.get(channel=ChannelChoices.PUSH).status,
                         NotificationStatus.SENT)

    def test_browser_and_phone_are_sent_the_same_message(self):
        self.phone(self.tunde, account=self.tunde_session.device_key)
        with mock.patch(WEBPUSH) as webpush, override_settings(**WEB_PUSH):
            push.subscribe(self.tunde, endpoint="https://push.example.com/t", p256dh="k", auth="a",
                           device_key=self.tunde_session.device_key)
            self.notify(self.tunde)
        browser = json.loads(webpush.call_args.kwargs["data"])
        phone = self.sent[0]["body"]
        self.assertEqual(phone["notification"], {"title": browser.pop("title"), "body": browser.pop("body")})
        self.assertEqual(phone["data"], {key: str(value) for key, value in browser.items()})

    def test_a_person_with_only_a_phone_is_counted_and_gets_a_push_record(self):
        self.phone(self.tunde)
        self.assertEqual(push.subscribed_user_ids([self.tunde.pk, self.bola.pk]), {self.tunde.pk})
        self.notify(self.tunde, self.bola)
        recipients = Notification.all_objects.filter(channel=ChannelChoices.PUSH).values_list("recipient_id", flat=True)
        self.assertEqual(list(recipients), [self.tunde.pk])

    @override_settings(FCM_SERVICE_ACCOUNT_FILE="")
    def test_without_a_firebase_key_a_phone_is_not_counted_and_nothing_is_sent(self):
        self.phone(self.tunde)
        self.assertEqual(push.subscribed_user_ids([self.tunde.pk]), set())
        self.notify(self.tunde)
        self.assertFalse(Notification.all_objects.filter(channel=ChannelChoices.PUSH).exists())
        self.assertEqual(self.sent, [])

    def test_a_browser_only_person_is_not_counted_without_web_push_keys(self):
        push.subscribe(self.tunde, endpoint="https://push.example.com/t", p256dh="k", auth="a")
        self.assertEqual(push.subscribed_user_ids([self.tunde.pk]), set())
        with override_settings(**WEB_PUSH):
            self.assertEqual(push.subscribed_user_ids([self.tunde.pk]), {self.tunde.pk})

    def test_a_deactivated_person_is_not_counted(self):
        self.phone(self.tunde)
        User.objects.filter(pk=self.tunde.pk).update(is_active=False)
        self.assertEqual(push.subscribed_user_ids([self.tunde.pk]), set())


class FcmSenderTests(_PhoneFixture):
    def send(self, token="phone-token", **overrides):
        arguments = {
            "title": "Tunde: New notice", "body": "Friday.", "tag": "notices.published",
            "data": {"url": "/parent/notices", "child": 7, "notification_id": "n-1"},
            "ttl_seconds": 86400,
        }
        arguments.update(overrides)
        return fcm.send(token, **arguments)

    def test_the_message_asks_for_urgent_delivery_and_a_one_day_life(self):
        before = int(time.time())
        self.assertIs(self.send(), fcm.Outcome.DELIVERED)
        message = self.sent[0]["body"]
        self.assertEqual(message["token"], "phone-token")
        self.assertEqual(message["notification"], {"title": "Tunde: New notice", "body": "Friday."})
        self.assertEqual(message["data"], {"url": "/parent/notices", "child": "7", "notification_id": "n-1"})
        self.assertTrue(all(isinstance(value, str) for value in message["data"].values()))
        self.assertEqual(message["android"],
                         {"priority": "HIGH", "ttl": "86400s", "notification": {"tag": "notices.published"}})
        apns = message["apns"]
        self.assertEqual(apns["headers"]["apns-collapse-id"], "notices.published")
        self.assertGreaterEqual(int(apns["headers"]["apns-expiration"]), before + 86400)
        self.assertEqual(apns["payload"], {"aps": {"sound": "default"}})

    def test_a_long_tag_is_cut_to_the_collapse_id_limit(self):
        self.send(tag="t" * 90)
        self.assertEqual(self.sent[0]["body"]["apns"]["headers"]["apns-collapse-id"], "t" * 64)

    def test_the_access_token_is_fetched_once_and_reused(self):
        self.send()
        self.send(token="another-phone")
        self.assertEqual(len(self.token_calls), 1)
        self.assertEqual({call["headers"]["Authorization"] for call in self.sent}, {"Bearer access-1"})

    def test_the_assertion_is_signed_for_the_messaging_scope(self):
        self.send()
        form, claims = self.token_calls[0]["form"], self.token_calls[0]["claims"]
        self.assertEqual(form["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        self.assertEqual(claims["iss"], CLIENT_EMAIL)
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/firebase.messaging")
        self.assertEqual(claims["exp"] - claims["iat"], 3600)

    def test_an_access_token_firebase_rejects_is_replaced_once(self):
        self.answers["phone-token"] = [(401, {"error": {"status": "UNAUTHENTICATED"}}), (200, {})]
        self.assertIs(self.send(), fcm.Outcome.DELIVERED)
        self.assertEqual(len(self.token_calls), 2)
        self.assertEqual([call["headers"]["Authorization"] for call in self.sent],
                         ["Bearer access-1", "Bearer access-2"])

    def test_google_refusing_the_assertion_is_a_failure_not_an_exception(self):
        def refuse(method, url, *, headers, body=None, form=None):
            return 400, {"error": "invalid_grant"}

        self.request.side_effect = refuse
        with self.assertLogs("vs_notifications.fcm", level="WARNING"):
            self.assertIs(self.send(), fcm.Outcome.FAILED)

    def test_an_unconfigured_sender_fails_quietly(self):
        with override_settings(FCM_SERVICE_ACCOUNT_FILE=""):
            self.assertFalse(fcm.configured())
            self.assertIs(self.send(), fcm.Outcome.FAILED)
        self.assertEqual(self.sent, [])


class FcmKeyFileTests(SimpleTestCase):
    def tearDown(self):
        fcm.reset_caches()

    def configured_with(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.json"
            if content is not None:
                path.write_text(content, encoding="utf-8")
            fcm.reset_caches()
            with override_settings(FCM_SERVICE_ACCOUNT_FILE=str(path)), mock.patch.object(fcm, "logger"):
                return fcm.configured()

    def test_an_unreadable_or_incomplete_key_file_is_not_configured(self):
        good = json.loads(_service_account_json("pem"))
        cases = {
            "missing file": None,
            "not json": "not json",
            "not an object": "[]",
            "no project id": json.dumps({**good, "project_id": ""}),
            "no client email": json.dumps({k: v for k, v in good.items() if k != "client_email"}),
            "no private key": json.dumps({**good, "private_key": ""}),
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.assertFalse(self.configured_with(content))

    def test_an_unset_path_is_not_configured(self):
        with override_settings(FCM_SERVICE_ACCOUNT_FILE=""):
            self.assertFalse(fcm.configured())

    def test_a_complete_key_file_is_configured_and_read_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.json"
            path.write_text(_service_account_json("pem"), encoding="utf-8")
            fcm.reset_caches()
            with override_settings(FCM_SERVICE_ACCOUNT_FILE=str(path)):
                self.assertTrue(fcm.configured())
                path.unlink()
                self.assertTrue(fcm.configured())
