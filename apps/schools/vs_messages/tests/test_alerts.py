"""Telling people a message has reached them, and never telling them what it said.

Mrs. Adeyemi and Mr. Musa teach at Lekki; Mrs. Okoro is the Lekki office. Tunde's
mother is Mrs. Funke Bello and his father, the primary guardian, Mr. Kunle
Bello. Every send here goes through the API with the commit hooks run, as it
would in production, so the alert is the real task reading the real rows.
"""
from __future__ import annotations

import json
import uuid
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from schools.vs_messages import tasks
from schools.vs_messages.constants import (
    EVENT_NEW_BROADCAST,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_MESSAGE_PARENT,
    EVENT_NEW_MESSAGE_PUPIL,
)
from schools.vs_messages.models import Message
from schools.vs_messages.services import groups, inbox, messages
from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification, PushSubscription
from vs_notifications.services import push
from vs_notifications.services.routing import notification_action_url
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_user.models import User

from .base import MessagesFixture

SECRET = "Zebra-crossing-9141-do-not-leak"
PUSH_KEYS = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": "BPublicKeyForTests",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "privateKeyForTests",
    "WEB_PUSH_CONTACT": "mailto:dev@example.com",
}
WEBPUSH = "pywebpush.webpush"
SEND = "vs_notifications.notify.send_notification"


class AlertFixture(MessagesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)
        install_declared_fields("school.messages")
        # Started empty, so nobody has anything unread until a test says something.
        cls.dm = cls.direct(cls.funke, cls.ibrahim, body="")
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother, body="")
        cls.pupil = cls.pupil_chat(cls.funke, cls.tunde, body="")

    # ── sending as the real thing ──

    def speak(self, user, conversation, text=SECRET, *, route="messages-messages", **extra):
        """POST *text* as *user* and run what the commit queued."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(user, route, {"body": text, **extra}, pk=conversation.pk)
        self.assertIn(response.status_code, (200, 201), response.data)
        return response

    def notes(self, user, key=None, channel=None):
        rows = Notification.all_objects.filter(recipient=user, event_type__key__startswith="messages.")
        if key:
            rows = rows.filter(event_type__key=key)
        if channel:
            rows = rows.filter(channel=channel)
        return rows.order_by("created_at")

    def subjects(self, user, channel=ChannelChoices.IN_APP):
        return [n.subject for n in self.notes(user, channel=channel)]

    def clear(self):
        Notification.all_objects.all().delete()

    def hide(self, field, role=None):
        set_field_access(role or self.teacher_role, f"school.messages.{field}", read=False, write=False)


class NoBodyTests(AlertFixture):
    """No channel and no stored row ever carries the words of a message."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for user in (cls.ibrahim, cls.mother.user, cls.tunde.user):
            PushSubscription.all_objects.create(
                tenant=user.tenant, user=user, endpoint=f"https://push.example.com/{user.pk}",
                endpoint_hash=push.endpoint_hash(f"https://push.example.com/{user.pk}"),
                p256dh="p256dh-key", auth="auth-key",
            )

    def everything_said(self):
        rows = list(Notification.all_objects.filter(event_type__key__startswith="messages."))
        self.assertTrue(rows)
        return rows

    @override_settings(SCHOOL_APP_BASE_URL="https://xvs.example.com", **PUSH_KEYS)
    def test_no_stored_notification_carries_the_message(self):
        with mock.patch(WEBPUSH):
            self.speak(self.funke, self.dm)
            self.speak(self.funke, self.family)
            self.speak(self.funke, self.pupil)
        rows = self.everything_said()
        channels = {row.channel for row in rows}
        self.assertEqual(channels, {ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH})
        for row in rows:
            with self.subTest(event=row.event_type.key, channel=row.channel):
                for part in (row.subject, row.body, row.html_body, json.dumps(row.metadata)):
                    self.assertNotIn(SECRET, part)
                self.assertEqual(row.metadata, {"conversation_id": row.metadata["conversation_id"]})

    @override_settings(SCHOOL_APP_BASE_URL="https://xvs.example.com", **PUSH_KEYS)
    def test_a_push_payload_carries_no_message_either(self):
        with mock.patch(WEBPUSH) as webpush:
            self.speak(self.funke, self.dm)
        self.assertEqual(webpush.call_count, 1)
        self.assertNotIn(SECRET, webpush.call_args.kwargs["data"])

    @override_settings(SCHOOL_APP_BASE_URL="https://xvs.example.com")
    def test_the_email_names_the_school_and_links_the_conversation(self):
        self.speak(self.funke, self.dm)
        email = self.notes(self.ibrahim, channel=ChannelChoices.EMAIL).get()
        link = f"https://brightfield.xvs.example.com/messages/{self.dm.pk}"
        self.assertIn("Brightfield Schools", email.body)
        self.assertIn(link, email.body + email.html_body)
        self.assertEqual(email.subject, "New message from Funke Adeyemi")

    @override_settings(
        SCHOOL_APP_BASE_URL="https://xvs.example.com", PARENT_APP_BASE_URL="", STUDENT_APP_BASE_URL="",
    )
    def test_family_emails_link_into_their_own_apps(self):
        self.speak(self.funke, self.family)
        self.speak(self.funke, self.pupil)
        parent = self.notes(self.mother.user, channel=ChannelChoices.EMAIL).get()
        pupil = self.notes(self.tunde.user, channel=ChannelChoices.EMAIL).get()
        self.assertIn(f"https://brightfield.xvs.example.com/parent/messages/{self.family.pk}",
                      parent.body + parent.html_body)
        self.assertIn(f"https://brightfield.xvs.example.com/student/messages/{self.pupil.pk}",
                      pupil.body + pupil.html_body)


class WhatEachPersonHearsTests(AlertFixture):
    """Who is alerted, with which event and which words."""

    def test_a_colleague_hears_who_wrote_and_is_told_to_open_messages(self):
        self.speak(self.funke, self.dm)
        row = self.notes(self.ibrahim, EVENT_NEW_MESSAGE, ChannelChoices.IN_APP).get()
        self.assertEqual(row.subject, "New message from Funke Adeyemi")
        self.assertEqual(row.body, "Open Messages to read it.")
        self.assertEqual(row.metadata, {"conversation_id": self.dm.pk})
        self.assertFalse(self.notes(self.funke).exists())

    def test_a_group_is_named_by_its_title(self):
        team = self.group(self.funke, [self.ibrahim, self.ngozi], name="Maths team", body="")
        self.clear()
        self.speak(self.ibrahim, team)
        for user in (self.funke, self.ngozi):
            self.assertEqual(self.subjects(user), ["New message in Maths team"])
        self.assertFalse(self.notes(self.ibrahim).exists())

    def test_a_parent_hears_who_wrote_about_which_child(self):
        self.speak(self.funke, self.family)
        row = self.notes(self.mother.user, EVENT_NEW_MESSAGE_PARENT, ChannelChoices.IN_APP).get()
        self.assertEqual(row.subject, "New message from Funke Adeyemi about Tunde")
        self.assertEqual(row.body, "Open the parent app to read it.")
        self.assertEqual(row.metadata, {"conversation_id": self.family.pk})

    def test_a_member_of_staff_hears_a_parent_wrote(self):
        self.clear()
        self.speak(self.mother.user, self.family, route="parent-messages-messages")
        self.assertEqual(self.subjects(self.funke), ["New message from Funke Bello"])
        self.assertEqual(
            self.notes(self.funke).first().event_type.key, EVENT_NEW_MESSAGE,
        )

    def test_a_pupil_hears_who_wrote(self):
        self.speak(self.funke, self.pupil)
        row = self.notes(self.tunde.user, EVENT_NEW_MESSAGE_PUPIL, ChannelChoices.IN_APP).get()
        self.assertEqual(row.subject, "New message from Funke Adeyemi")
        self.assertEqual(row.body, "Open the app to read it.")

    def test_the_primary_guardian_who_only_watches_is_never_alerted(self):
        self.speak(self.funke, self.pupil)
        self.speak(self.tunde.user, self.pupil, route="student-messages-messages")
        self.assertFalse(self.notes(self.father.user).exists())
        self.assertFalse(self.notes(self.mother.user, EVENT_NEW_MESSAGE_PARENT).exists())

    def test_a_broadcast_is_announced_once_and_followed_up_by_name(self):
        notice = self.broadcast(self.lekki_head, [self.funke, self.ibrahim], title="Staff meeting")
        for reader in (self.funke, self.ibrahim):
            messages.mark_read(self.access(reader, notice), reader)
        self.clear()
        self.speak(self.lekki_head, notice, "A second line")
        for user in (self.funke, self.ibrahim):
            self.assertEqual(self.subjects(user), ["New message in Staff meeting"])
            self.assertEqual(self.notes(user).first().event_type.key, EVENT_NEW_MESSAGE)

    def test_a_new_broadcast_uses_its_own_event(self):
        first = self.lekki_head
        with self.captureOnCommitCallbacks(execute=True):
            notice = self.broadcast(first, [self.funke, self.ibrahim], title="Staff meeting")
        name = inbox.person_name(first)
        for user in (self.funke, self.ibrahim):
            row = self.notes(user).get(channel=ChannelChoices.IN_APP)
            self.assertEqual(row.event_type.key, EVENT_NEW_BROADCAST)
            self.assertEqual(row.subject, f"{name} sent a broadcast")
            self.assertEqual(row.metadata, {"conversation_id": notice.pk})
        self.assertFalse(self.notes(first).exists())

    def test_a_private_reply_alerts_the_sender_only(self):
        notice = self.broadcast(self.lekki_head, [self.funke, self.ibrahim], title="Staff meeting")
        self.clear()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.funke, "messages-broadcast-reply", {"body": SECRET}, pk=notice.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(self.subjects(self.lekki_head)), 1)
        self.assertFalse(self.notes(self.ibrahim).exists())


class FieldAccessInAlertsTests(AlertFixture):
    """A recipient who may not read names is not told them."""

    def test_hidden_names_become_a_colleague(self):
        self.hide("participants")
        self.speak(self.funke, self.dm)
        self.assertEqual(self.subjects(self.ibrahim), ["New message from a colleague"])
        email = self.notes(self.ibrahim, channel=ChannelChoices.EMAIL).get()
        self.assertEqual(email.subject, "New message from a colleague")
        self.assertNotIn("Funke", email.subject + email.body)

    def test_hidden_names_become_a_parent_when_a_parent_wrote(self):
        self.hide("participants")
        self.clear()
        self.speak(self.mother.user, self.family, route="parent-messages-messages")
        self.assertEqual(self.subjects(self.funke), ["New message from a parent"])

    def test_hidden_group_names_become_a_staff_group(self):
        team = self.group(self.lekki_head, [self.funke, self.ngozi], name="Maths team", body="")
        self.hide("title")
        self.clear()
        self.speak(self.lekki_head, team)
        self.assertEqual(self.subjects(self.funke), ["New message in a staff group"])
        # A role with no switch set still reads the name.
        self.assertEqual(self.subjects(self.ngozi), ["New message in Maths team"])

    def test_a_hidden_broadcast_sender_is_capitalised(self):
        self.hide("participants")
        with self.captureOnCommitCallbacks(execute=True):
            self.broadcast(self.lekki_head, [self.funke, self.ngozi], title="Staff meeting")
        self.assertEqual(self.subjects(self.funke), ["A colleague sent a broadcast"])
        self.assertEqual(
            self.subjects(self.ngozi), [f"{inbox.person_name(self.lekki_head)} sent a broadcast"],
        )

    def test_staff_with_the_same_rights_share_one_send(self):
        team = self.group(self.lekki_head, [self.funke, self.ibrahim, self.ngozi], name="Staff room", body="")
        self.clear()
        with mock.patch(SEND) as send, self.captureOnCommitCallbacks(execute=True):
            self.post(self.lekki_head, "messages-messages", {"body": "Hello"}, pk=team.pk)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            {u.pk for u in send.call_args.kwargs["recipients"]},
            {self.funke.pk, self.ibrahim.pk, self.ngozi.pk},
        )


class WhoIsAlertedTests(AlertFixture):
    """Coalescing, joining, leaving and deactivation."""

    def test_the_second_message_before_reading_sends_no_second_alert(self):
        self.speak(self.funke, self.dm, "First")
        self.speak(self.funke, self.dm, "Second")
        self.assertEqual(len(self.subjects(self.ibrahim)), 1)

    def test_after_reading_the_next_message_alerts_again(self):
        self.speak(self.funke, self.dm, "First")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.ibrahim, "messages-read", pk=self.dm.pk)
        self.assertEqual(response.status_code, 200)
        self.speak(self.funke, self.dm, "Third")
        self.assertEqual(len(self.subjects(self.ibrahim)), 2)

    def test_reading_the_conversation_clears_its_bell_entries(self):
        self.speak(self.funke, self.dm)
        before = self.notes(self.ibrahim, channel=ChannelChoices.IN_APP).get()
        self.assertFalse(before.is_read)
        self.post(self.ibrahim, "messages-read", pk=self.dm.pk)
        before.refresh_from_db()
        self.assertTrue(before.is_read)

    def test_reading_one_conversation_leaves_another_alert_unread(self):
        team = self.group(self.funke, [self.ibrahim], name="Maths team", body="")
        self.clear()
        self.speak(self.funke, self.dm)
        self.speak(self.funke, team)
        self.post(self.ibrahim, "messages-read", pk=self.dm.pk)
        self.assertEqual(
            [n.is_read for n in self.notes(self.ibrahim, channel=ChannelChoices.IN_APP)],
            [True, False],
        )

    def test_a_parent_and_a_pupil_clear_their_own_bell_entries(self):
        self.speak(self.funke, self.family)
        self.speak(self.funke, self.pupil)
        self.post(self.mother.user, "parent-messages-read", pk=self.family.pk)
        self.post(self.tunde.user, "student-messages-read", pk=self.pupil.pk)
        for user in (self.mother.user, self.tunde.user):
            self.assertTrue(all(n.is_read for n in self.notes(user, channel=ChannelChoices.IN_APP)))

    def test_a_member_added_later_is_alerted_only_to_what_comes_after_joining(self):
        team = self.group(self.funke, [self.ibrahim], name="Maths team", body="Before you")
        older = Message.objects.filter(conversation=team).latest("id")
        groups.add_members(groups.group_access(self.tenant, self.funke, team.pk), self.funke, [self.ngozi.pk])
        self.clear()
        # A worker that reaches the older message late does not tell the newcomer about it.
        tasks.dispatch_message_alert(older.pk)
        self.assertFalse(self.notes(self.ngozi).exists())
        self.speak(self.funke, team, "After you")
        self.assertEqual(self.subjects(self.ngozi), ["New message in Maths team"])

    def test_a_removed_member_and_a_deactivated_account_are_not_alerted(self):
        team = self.group(self.funke, [self.ibrahim, self.ngozi, self.reader], name="Staff room", body="")
        groups.remove_member(groups.group_access(self.tenant, self.funke, team.pk), self.funke, self.ngozi.pk)
        User.objects.filter(pk=self.ibrahim.pk).update(is_active=False)
        self.clear()
        self.speak(self.funke, team)
        self.assertFalse(self.notes(self.ngozi).exists())
        self.assertFalse(self.notes(self.ibrahim).exists())
        self.assertEqual(len(self.subjects(self.reader)), 1)

    def test_a_late_worker_does_not_alert_someone_who_has_already_read(self):
        message = self.say(self.dm, self.funke, "Hello again")
        messages.mark_read(self.access(self.ibrahim, self.dm), self.ibrahim)
        self.clear()
        tasks.dispatch_message_alert(message.pk)
        self.assertFalse(Notification.all_objects.filter(event_type__key__startswith="messages.").exists())

    def test_a_staff_member_who_left_the_roll_is_not_alerted(self):
        from schools.vs_staff.models import StaffProfile

        StaffProfile.objects.filter(user=self.ibrahim).update(employment_status="RESIGNED")
        self.clear()
        self.speak(self.funke, self.dm)
        self.assertFalse(self.notes(self.ibrahim).exists())

    def test_a_family_whose_link_was_removed_is_not_alerted(self):
        from schools.vs_students.models import StudentGuardian

        message = self.say(self.family, self.funke, "Hello")
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).delete()
        tasks.dispatch_message_alert(message.pk)
        self.assertFalse(self.notes(self.mother.user).exists())

    def test_a_large_group_is_told_in_batches(self):
        team = self.group(self.lekki_head, [self.funke, self.ibrahim, self.ngozi], name="Staff room", body="")
        self.clear()
        with mock.patch.object(tasks, "BATCH", 2), mock.patch(SEND) as send, \
                self.captureOnCommitCallbacks(execute=True):
            self.post(self.lekki_head, "messages-messages", {"body": "Hello"}, pk=team.pk)
        self.assertEqual([len(c.kwargs["recipients"]) for c in send.call_args_list], [2, 1])


class FailureTests(AlertFixture):
    """A failure to alert never loses or fails a message."""

    def test_a_failing_notification_leaves_the_message_saved_and_the_send_201(self):
        with mock.patch(SEND, side_effect=RuntimeError("mail is down")), \
                self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.funke, "messages-messages", {"body": SECRET}, pk=self.dm.pk)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Message.objects.filter(conversation=self.dm, body=SECRET).exists())

    def test_one_failing_group_does_not_stop_the_others(self):
        self.hide("participants")
        team = self.group(self.lekki_head, [self.funke, self.ngozi], name="Staff room", body="")
        self.clear()
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs["context"]["sender_label"])
            if len(calls) == 1:
                raise RuntimeError("first group fails")

        with mock.patch(SEND, side_effect=flaky), self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.lekki_head, "messages-messages", {"body": "Hello"}, pk=team.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(calls), 2)

    def test_a_broker_that_is_down_loses_the_alert_and_nothing_else(self):
        with mock.patch.object(tasks, "dispatch_message_alert") as task:
            task.delay.side_effect = OSError("no broker")
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post(self.funke, "messages-messages", {"body": "Hello"}, pk=self.dm.pk)
        self.assertEqual(response.status_code, 201)

    def test_a_message_that_is_gone_tells_nobody(self):
        self.assertEqual(tasks.dispatch_message_alert(999999), 0)

    def test_suppressed_alerts_queue_nothing(self):
        with tasks.alerts_suppressed(), self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.say(self.dm, self.funke, "Quietly")
        self.assertEqual(callbacks, [])
        self.assertFalse(Notification.all_objects.filter(event_type__key__startswith="messages.").exists())


class DestinationTests(AlertFixture):
    """Where each alert opens: in the bell and as a push."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for user in (cls.mother.user, cls.tunde.user):
            PushSubscription.all_objects.create(
                tenant=user.tenant, user=user, endpoint=f"https://push.example.com/{user.pk}",
                endpoint_hash=push.endpoint_hash(f"https://push.example.com/{user.pk}"),
                p256dh="p256dh-key", auth="auth-key",
            )

    def test_every_alert_opens_its_conversation(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.broadcast(self.lekki_head, [self.funke, self.ibrahim], title="Staff meeting")
        self.speak(self.funke, self.dm)
        self.speak(self.funke, self.family)
        self.speak(self.funke, self.pupil)
        seen = set()
        for row in Notification.all_objects.filter(event_type__key__startswith="messages."):
            seen.add(row.event_type.key)
            self.assertEqual(notification_action_url(row), f"/messages/{row.metadata['conversation_id']}")
        self.assertEqual(
            seen,
            {EVENT_NEW_BROADCAST, EVENT_NEW_MESSAGE, EVENT_NEW_MESSAGE_PARENT, EVENT_NEW_MESSAGE_PUPIL},
        )

    def test_a_new_broadcast_opens_its_conversation(self):
        with self.captureOnCommitCallbacks(execute=True):
            notice = self.broadcast(self.lekki_head, [self.funke], title="Staff meeting")
        row = self.notes(self.funke, EVENT_NEW_BROADCAST, ChannelChoices.IN_APP).get()
        self.assertEqual(notification_action_url(row), f"/messages/{notice.pk}")

    @override_settings(**PUSH_KEYS)
    def test_a_parents_push_opens_the_parent_app_and_a_pupils_the_student_app(self):
        with mock.patch(WEBPUSH) as webpush:
            self.speak(self.funke, self.family)
            self.speak(self.funke, self.pupil)
        urls = {}
        for call in webpush.call_args_list:
            payload = json.loads(call.kwargs["data"])
            urls[payload["tag"]] = payload["url"]
        self.assertEqual(urls[EVENT_NEW_MESSAGE_PARENT], f"/parent/messages/{self.family.pk}")
        self.assertEqual(urls[EVENT_NEW_MESSAGE_PUPIL], f"/student/messages/{self.pupil.pk}")

    def test_the_push_layer_knows_the_destinations_by_event(self):
        from schools.vs_messages.push import destination

        def note(key, **metadata):
            return mock.Mock(event_type=mock.Mock(key=key), metadata=metadata)

        self.assertEqual(destination(note(EVENT_NEW_MESSAGE_PARENT, conversation_id=7)).url, "/parent/messages/7")
        self.assertEqual(destination(note(EVENT_NEW_MESSAGE_PUPIL, conversation_id=7)).url, "/student/messages/7")
        self.assertEqual(destination(note(EVENT_NEW_MESSAGE_PARENT)).url, "/parent/messages")
        self.assertIsNone(destination(note(EVENT_NEW_MESSAGE, conversation_id=7)))
        self.assertIsNone(destination(note("notices.published")))

    def test_the_destination_is_registered_with_the_push_layer(self):
        from schools.vs_messages.push import destination

        self.assertIn(destination, push._DESTINATIONS)


class OneTokenOnlyTests(AlertFixture):
    """A retried send is one message and so one alert."""

    def test_a_retried_send_alerts_once(self):
        token = str(uuid.uuid4())
        self.speak(self.funke, self.dm, "Hello", client_token=token)
        self.post(self.funke, "messages-messages", {"body": "Hello", "client_token": token}, pk=self.dm.pk)
        self.assertEqual(len(self.subjects(self.ibrahim)), 1)

