"""Push notifications to the devices a person allowed them on.

Tunde turns notifications on in the student app on his mother's phone; his
sister Bola does too, on the same phone. A notice published for Tunde reaches
his device with his first name in front, and nothing is pushed to Peter, who
has no device. A push service that has forgotten a device has its
subscription deleted. Signing Tunde out of the phone stops his pushes there
and leaves Bola's. What a payment push says can show on a locked screen, so
it carries no amount.
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import override_settings
from pywebpush import WebPushException

from vs_user.models import LoginSession

from .constants import ChannelChoices, NotificationStatus
from .models import Notification, PushSubscription
from .notify import send_notification
from .services import push
from .tests import _NotifFixture

KEYS = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": "BPublicKeyForTests",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "privateKeyForTests",
    "WEB_PUSH_CONTACT": "mailto:dev@example.com",
}
WEBPUSH = "pywebpush.webpush"
NOTICE = {"notice_title": "Inter-house sports", "notice_excerpt": "Friday.",
          "notice_body": "Friday.", "audience_label": "JSS1 A", "tenant_name": "Alpha"}


def subscription_body(endpoint="https://push.example.com/abc", account=""):
    body = {"endpoint": endpoint, "keys": {"p256dh": "p256dh-key", "auth": "auth-key"}}
    if account:
        body["account"] = account
    return body


@override_settings(**KEYS)
class PushTests(_NotifFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = cls.plain_a
        cls.tunde.first_name = "Tunde"
        cls.tunde.save(update_fields=["first_name"])
        cls.tunde_session = LoginSession.all_objects.create(
            user=cls.tunde, tenant=cls.tunde.tenant, device_key="a" * 32, is_active=True,
        )

    def subscribe(self, user=None, **kwargs):
        return self._client(user or self.tunde).post(
            "/v1/notify/push/subscriptions/", subscription_body(**kwargs), format="json",
        )

    def notify_tunde(self):
        with mock.patch(WEBPUSH) as webpush, self.captureOnCommitCallbacks(execute=True):
            send_notification(
                event_key="notices.published", context=NOTICE,
                recipients=[self.tunde, self.admin_a], school=self.school_a,
            )
        return webpush

    # -- routes ---------------------------------------------------------------- #

    def test_the_key_route_hands_out_the_public_key(self):
        response = self._client(self.tunde).get("/v1/notify/push/key/")
        self.assertEqual(response.data["data"], {"public_key": "BPublicKeyForTests"})

    @override_settings(WEB_PUSH_VAPID_PUBLIC_KEY="", WEB_PUSH_VAPID_PRIVATE_KEY="")
    def test_without_keys_push_is_off(self):
        self.assertEqual(self._client(self.tunde).get("/v1/notify/push/key/").status_code, 503)
        self.assertEqual(self.subscribe().status_code, 503)

    def test_subscribing_twice_keeps_one_device_and_deleting_forgets_it(self):
        self.assertEqual(self.subscribe(account=self.tunde_session.device_key).status_code, 201)
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(PushSubscription.all_objects.filter(user=self.tunde).count(), 1)
        self._client(self.tunde).delete(
            "/v1/notify/push/subscriptions/",
            {"endpoint": "https://push.example.com/abc"}, format="json",
        )
        self.assertFalse(PushSubscription.all_objects.filter(user=self.tunde).exists())

    def test_an_account_that_is_not_the_callers_session_is_not_recorded(self):
        self.subscribe(user=self.admin_a, account=self.tunde_session.device_key)
        self.assertEqual(PushSubscription.all_objects.get(user=self.admin_a).device_key, "")

    def test_a_plain_http_endpoint_is_refused(self):
        self.assertEqual(self.subscribe(endpoint="http://push.example.com/abc").status_code, 400)

    # -- dispatch and delivery ------------------------------------------------- #

    def test_only_a_recipient_with_a_device_gets_a_push_and_it_names_them(self):
        self.subscribe(account=self.tunde_session.device_key)
        webpush = self.notify_tunde()

        pushes = Notification.all_objects.filter(channel=ChannelChoices.PUSH)
        self.assertEqual([n.recipient_id for n in pushes], [self.tunde.pk])
        self.assertEqual(pushes.get().status, NotificationStatus.SENT)
        payload = json.loads(webpush.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Tunde: New notice: Inter-house sports")
        self.assertEqual(payload["account"], self.tunde_session.device_key)

    def test_a_subscription_with_no_device_session_names_no_account(self):
        self.subscribe()
        webpush = self.notify_tunde()
        payload = json.loads(webpush.call_args.kwargs["data"])
        self.assertNotIn("account", payload)
        self.assertEqual(payload["title"], "New notice: Inter-house sports")

    def test_a_device_the_push_service_has_forgotten_is_deleted(self):
        self.subscribe()
        gone = WebPushException("Gone", response=mock.Mock(status_code=410))
        with mock.patch(WEBPUSH, side_effect=gone), self.captureOnCommitCallbacks(execute=True):
            send_notification(
                event_key="notices.published", context=NOTICE,
                recipients=[self.tunde], school=self.school_a,
            )
        self.assertFalse(PushSubscription.all_objects.filter(user=self.tunde).exists())
        failed = Notification.all_objects.get(channel=ChannelChoices.PUSH)
        self.assertEqual(failed.status, NotificationStatus.FAILED)

    def test_signing_the_device_out_stops_that_persons_pushes_only(self):
        from vs_user.services.device_sessions import _end

        self.subscribe(account=self.tunde_session.device_key)
        self.subscribe(user=self.admin_a, endpoint="https://push.example.com/bola")
        _end(self.tunde_session, reason="LOGOUT")
        self.assertFalse(PushSubscription.all_objects.filter(user=self.tunde).exists())
        self.assertTrue(PushSubscription.all_objects.filter(user=self.admin_a).exists())

    def test_a_payment_push_carries_no_amount(self):
        self.subscribe()
        with mock.patch(WEBPUSH) as webpush, self.captureOnCommitCallbacks(execute=True):
            send_notification(
                event_key="billing.payment_received",
                context={"amount": "₦25,000.00", "balance": "₦5,000.00",
                         "customer_name": "Tunde", "receipt_number": "RCT-0042"},
                recipients=[self.tunde], school=self.school_a,
            )
        payload = webpush.call_args.kwargs["data"]
        self.assertIn("Payment confirmed", payload)
        self.assertNotIn("25,000", payload)
        self.assertNotIn("5,000", payload)

    def test_every_push_template_carries_no_figure_placeholder(self):
        from .models import NotificationTemplate

        for template in NotificationTemplate.objects.filter(channel=ChannelChoices.PUSH):
            with self.subTest(event=template.event_type.key):
                text = template.subject + template.body
                for word in ("amount", "balance", "score", "grade", "position", "average"):
                    self.assertNotIn(word, text)

    @override_settings(WEB_PUSH_VAPID_PUBLIC_KEY="", WEB_PUSH_VAPID_PRIVATE_KEY="")
    def test_without_keys_nothing_is_pushed(self):
        PushSubscription.all_objects.create(
            tenant=self.tunde.tenant, user=self.tunde, endpoint="https://p.example.com/x",
            endpoint_hash=push.endpoint_hash("https://p.example.com/x"), p256dh="k", auth="a",
        )
        self.notify_tunde()
        self.assertFalse(Notification.all_objects.filter(channel=ChannelChoices.PUSH).exists())
