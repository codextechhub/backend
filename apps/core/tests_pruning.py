"""The nightly pruning of personal records past their retention period.

Lagos Model College has a year of sign-ins, notifications and webhooks, old
conversations and closed tickets. Each kind is pruned on its own period from
``core.pruning.SCHEDULE``, and what is still in use stays whatever its age: an
email waiting to go, a webhook not yet booked, a ticket only resolved, and a
broadcast someone is still replying to.
"""
from __future__ import annotations

import datetime
import io
import uuid
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.celery import app as celery_app
from core import pruning
from vs_rbac.tests.helpers import make_school, make_vision_user

NOW = timezone.now()


def days_ago(days):
    return NOW - datetime.timedelta(days=days)


def backdate(row, **fields):
    type(row)._base_manager.filter(pk=row.pk).update(**fields)


class PruningFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="lagos-model", name="Lagos Model College")
        cls.tenant = cls.school.tenant
        cls.user = make_vision_user(email="prune@codex.test")

    def prune(self, key, dry_run=False):
        report = pruning.run(now=NOW, dry_run=dry_run)["periods"][key]
        self.assertNotIn("stopped", report)
        return report["rows"]


class SignInRecordTests(PruningFixture):
    def test_a_year_old_sign_in_trail_goes_and_this_years_stays(self):
        from vs_notifications.models import NativePushDevice, PushSubscription
        from vs_user.models import (
            AccountClosureLink, AuthAttempt, LoginSession, PasswordResetRequest,
            TwoStepChallenge,
        )

        def attempt(days):
            row = AuthAttempt.all_objects.create(
                email_entered="prune@codex.test", result="FAIL", ip_address="10.0.0.1",
            )
            backdate(row, created_at=days_ago(days))
            return row

        def session(days):
            return LoginSession.all_objects.create(user=self.user, last_seen_at=days_ago(days))

        def challenge(days):
            return TwoStepChallenge.objects.create(
                user=self.user, token_hash=uuid.uuid4().hex, purpose="VERIFY",
                expires_at=days_ago(days),
            )

        def reset(days):
            return PasswordResetRequest.objects.create(
                user=self.user, token_hash=uuid.uuid4().hex,
                expires_at=days_ago(days), used_at=days_ago(days),
            )

        def closure(days):
            return AccountClosureLink.objects.create(
                user=self.user, token_hash=uuid.uuid4().hex,
                expires_at=days_ago(days), used_at=days_ago(days),
            )

        def browser(days):
            return PushSubscription.all_objects.create(
                tenant=self.tenant, user=self.user, endpoint=f"https://push/{uuid.uuid4()}",
                endpoint_hash=uuid.uuid4().hex, p256dh="k", auth="a",
                last_seen_at=days_ago(days),
            )

        def phone(days):
            return NativePushDevice.all_objects.create(
                tenant=self.tenant, user=self.user, platform="ANDROID", app_id="ng.xvs",
                token=uuid.uuid4().hex, token_hash=uuid.uuid4().hex,
                last_seen_at=days_ago(days),
            )

        made = [attempt, session, challenge, reset, closure, browser, phone]
        old = [make(400) for make in made]
        recent = [make(300) for make in made]

        rows = self.prune("sign_in")

        self.assertEqual(set(rows.values()), {1})
        for row in old:
            self.assertFalse(type(row)._base_manager.filter(pk=row.pk).exists(), row)
        for row in recent:
            self.assertTrue(type(row)._base_manager.filter(pk=row.pk).exists(), row)

    def test_an_expired_refresh_token_goes_with_its_blacklist_row(self):
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )

        expired = OutstandingToken.objects.create(
            user=self.user, jti="expired", token="t1", expires_at=days_ago(1),
        )
        BlacklistedToken.objects.create(token=expired)
        live = OutstandingToken.objects.create(
            user=self.user, jti="live", token="t2", expires_at=NOW + datetime.timedelta(hours=1),
        )

        self.assertEqual(self.prune("refresh_tokens"), {"refresh_tokens": 1})
        self.assertEqual(list(OutstandingToken.objects.values_list("pk", flat=True)), [live.pk])
        self.assertFalse(BlacklistedToken.objects.exists())


class NotificationTests(PruningFixture):
    def test_a_year_old_notification_goes_unless_its_email_is_still_waiting(self):
        from vs_notifications.constants import ChannelChoices, NotificationStatus
        from vs_notifications.models import Notification, NotificationEventType

        event = NotificationEventType.objects.create(
            key="prune.test", label="Prune test", source_module="vs_notifications",
            supported_channels=[ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        )

        def notification(days, channel=ChannelChoices.IN_APP, status=NotificationStatus.SENT):
            row = Notification.all_objects.create(
                tenant=self.tenant, recipient=self.user, event_type=event, body="Fees due",
                channel=channel, status=status,
            )
            backdate(row, created_at=days_ago(days))
            return row

        read = notification(400)
        failed = notification(400, ChannelChoices.EMAIL, NotificationStatus.FAILED)
        waiting = notification(400, ChannelChoices.EMAIL, NotificationStatus.PENDING)
        recent = notification(30)

        self.assertEqual(self.prune("notifications"), {"notifications": 2})
        kept = set(Notification.all_objects.values_list("pk", flat=True))
        self.assertEqual(kept, {waiting.pk, recent.pk})
        self.assertNotIn(read.pk, kept)
        self.assertNotIn(failed.pk, kept)


class WebhookTests(PruningFixture):
    def test_a_handled_webhook_goes_after_a_year_and_an_unhandled_one_stays(self):
        from vs_payments.models import WebhookEvent

        def webhook(days, status):
            row = WebhookEvent.objects.create(
                provider="PAYSTACK", status=status, dedupe_key=uuid.uuid4().hex,
                payload={"customer": {"email": "parent@example.com"}},
            )
            backdate(row, created_at=days_ago(days))
            return row

        webhook(400, "PROCESSED")
        webhook(400, "IGNORED")
        received = webhook(400, "RECEIVED")
        recent = webhook(30, "PROCESSED")

        self.assertEqual(self.prune("payment_webhooks"), {"webhooks": 2})
        self.assertEqual(
            set(WebhookEvent.objects.values_list("pk", flat=True)), {received.pk, recent.pk},
        )


class ConversationTests(PruningFixture):
    def conversation(self, days, **fields):
        from schools.vs_messages.models import Conversation

        fields.setdefault("kind", "STAFF_DIRECT")
        if fields["kind"] in ("STAFF_DIRECT", "BROADCAST_REPLY"):
            fields.setdefault("pair_key", uuid.uuid4().hex)
        row = Conversation.all_objects.create(tenant=self.tenant, **fields)
        backdate(row, created_at=days_ago(days), last_message_at=days_ago(days))
        return row

    def test_a_conversation_quiet_for_three_years_goes_with_its_messages(self):
        from schools.vs_messages.models import Conversation, Message

        quiet = self.conversation(3 * 365 + 10)
        Message.all_objects.create(tenant=self.tenant, conversation=quiet, sender=self.user, body="Hello")
        lively = self.conversation(365)

        self.assertEqual(self.prune("messages"), {"conversations": 1})
        self.assertEqual(list(Conversation.all_objects.values_list("pk", flat=True)), [lively.pk])
        self.assertFalse(Message.all_objects.exists())

    def test_a_quiet_broadcast_stays_while_a_reply_to_it_is_in_use(self):
        from schools.vs_messages.models import Conversation

        old = 3 * 365 + 10
        answered = self.conversation(old, kind="BROADCAST", title="Sports day", reply_mode="ONE_WAY")
        self.conversation(30, kind="BROADCAST_REPLY", broadcast=answered)
        finished = self.conversation(old, kind="BROADCAST", title="Old notice", reply_mode="ONE_WAY")
        self.conversation(old, kind="BROADCAST_REPLY", broadcast=finished)

        self.assertEqual(self.prune("messages", dry_run=True), {"conversations": 2})
        self.assertEqual(self.prune("messages"), {"conversations": 2})
        self.assertTrue(Conversation.all_objects.filter(pk=answered.pk).exists())
        self.assertFalse(Conversation.all_objects.filter(pk=finished.pk).exists())


class TicketTests(PruningFixture):
    def test_a_ticket_closed_two_years_ago_goes_and_a_resolved_one_stays(self):
        from vs_tickets.models import Ticket

        def ticket(status, closed_days=None):
            row = Ticket.all_objects.create(
                tenant=self.tenant, requester=self.user, title=status, description="x",
                status=status,
            )
            backdate(row, closed_at=days_ago(closed_days) if closed_days else None,
                     created_at=days_ago(800))
            return row

        ticket("CLOSED", 2 * 365 + 10)
        recent = ticket("CLOSED", 365)
        resolved = ticket("RESOLVED")

        self.assertEqual(self.prune("support_tickets"), {"tickets": 1})
        self.assertEqual(
            set(Ticket.all_objects.values_list("pk", flat=True)), {recent.pk, resolved.pk},
        )


class RunTests(PruningFixture):
    def make_old_attempt(self):
        from vs_user.models import AuthAttempt

        row = AuthAttempt.all_objects.create(email_entered="old@codex.test", result="FAIL")
        backdate(row, created_at=days_ago(400))
        return row

    def test_a_dry_run_counts_and_deletes_nothing(self):
        from vs_user.models import AuthAttempt

        row = self.make_old_attempt()
        self.assertEqual(self.prune("sign_in", dry_run=True)["sign_in_attempts"], 1)
        self.assertTrue(AuthAttempt.all_objects.filter(pk=row.pk).exists())

    def test_a_second_run_finds_nothing(self):
        self.make_old_attempt()
        self.prune("sign_in")
        self.assertEqual(self.prune("sign_in")["sign_in_attempts"], 0)

    def test_one_period_failing_does_not_stop_the_others(self):
        from vs_user.models import AuthAttempt

        self.make_old_attempt()
        with mock.patch(
            "vs_notifications.pruning.prune_notifications", side_effect=RuntimeError("boom"),
        ):
            report = pruning.run(now=NOW)["periods"]
        self.assertEqual(report["notifications"]["stopped"], "boom")
        self.assertEqual(report["sign_in"]["rows"]["sign_in_attempts"], 1)
        self.assertFalse(AuthAttempt.all_objects.exists())

    def test_batches_take_every_row(self):
        from vs_user.models import AuthAttempt

        for _ in range(5):
            self.make_old_attempt()
        with mock.patch.object(pruning, "BATCH", 2):
            self.assertEqual(self.prune("sign_in")["sign_in_attempts"], 5)
        self.assertFalse(AuthAttempt.all_objects.exists())

    def test_every_period_names_a_function_that_exists(self):
        from django.utils.module_loading import import_string

        for period in pruning.SCHEDULE:
            self.assertTrue(callable(import_string(period.prune)), period.key)

    def test_the_job_runs_every_night(self):
        entry = celery_app.conf.beat_schedule["core-prune-past-retention"]
        self.assertEqual(entry["task"], "core.prune_past_retention")
        from core.tasks import prune_past_retention_task

        self.assertEqual(prune_past_retention_task.name, "core.prune_past_retention")

    def test_the_command_reports_a_dry_run(self):
        from vs_user.models import AuthAttempt

        self.make_old_attempt()
        out = io.StringIO()
        call_command("prune_past_retention", "--dry-run", stdout=out)
        self.assertIn("would delete 1 sign in attempts", out.getvalue())
        self.assertTrue(AuthAttempt.all_objects.exists())
