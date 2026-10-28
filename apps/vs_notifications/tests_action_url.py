"""Where a bell entry opens: the same answer the push gives, else the module's own page.

A module that serves another app registers where its pushes open
(:func:`vs_notifications.services.push.register_destination`). The bell entry
for the same notification follows that answer, so a tap on the push and a tap on
the entry never disagree. A record page the notification table names, and the
module index it falls back to, apply only when no registered destination claims
the notification.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from vs_notifications.services import push
from vs_notifications.services.routing import notification_action_url


def notification(key, **metadata):
    return SimpleNamespace(event_type=SimpleNamespace(key=key), metadata=metadata, pk="n-1")


class ActionUrlFollowsPushDestinationTests(SimpleTestCase):
    def test_a_registered_destination_decides_the_link(self):
        claimed = push.PushDestination(url="/parent/fees/receipts/9")
        with mock.patch.object(push, "_DESTINATIONS", [lambda _n: claimed]):
            self.assertEqual(
                notification_action_url(notification("billing.payment_received", payment_id=9)),
                "/parent/fees/receipts/9",
            )

    def test_a_notification_no_destination_claims_keeps_its_module_index(self):
        with mock.patch.object(push, "_DESTINATIONS", [lambda _n: None]):
            self.assertEqual(
                notification_action_url(notification("billing.payment_received", payment_id=9)),
                "/finance",
            )

    def test_a_record_page_in_the_routing_table_outranks_a_destination(self):
        claimed = push.PushDestination(url="/elsewhere")
        with mock.patch.object(push, "_DESTINATIONS", [lambda _n: claimed]):
            self.assertEqual(
                notification_action_url(notification("ticket.created", ticket_id=4)),
                "/support/tickets/4",
            )

    def test_a_destination_that_fails_never_breaks_the_link(self):
        def broken(_n):
            raise RuntimeError("boom")

        with mock.patch.object(push, "_DESTINATIONS", [broken]), self.assertLogs("vs_notifications.push", "ERROR"):
            self.assertEqual(notification_action_url(notification("billing.payment_received")), "/finance")
