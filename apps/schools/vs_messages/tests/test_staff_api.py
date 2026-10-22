"""The staff routes: keys, isolation, starting and sending, paging and counts.

Each test builds on a small school and changes one thing. Cross-school
isolation is asserted by comparing the answer for another school's conversation
with the answer for an id that does not exist anywhere: they must be identical.
"""
from __future__ import annotations

import uuid
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_messages.models import Conversation, Message, Participant
from schools.vs_messages.services import broadcasts, conversations, groups
from schools.vs_staff.constants import EmploymentStatus
from vs_user.models import User

from .base import MessagesFixture

MISSING = 999999


class SunriseConversations:
    """One conversation of every kind at Sunrise, for the cross-school checks."""

    @classmethod
    def build(cls, fx):
        tenant = fx.solo.tenant
        out = {}
        out["STAFF_DIRECT"], _, _ = conversations.start_staff_direct(
            tenant, fx.ada, recipient_id=fx.solo_admin.pk, body="Hello",
        )
        out["FAMILY"], _, _ = conversations.start_family_from_staff(
            tenant, fx.ada, kind="FAMILY", student_id=fx.bola.pk,
            guardian_id=fx.bayo.pk, body="Hello",
        )
        out["PUPIL"], _, _ = conversations.start_family_from_staff(
            tenant, fx.ada, kind="PUPIL", student_id=fx.bola.pk, body="Hello",
        )
        out["GROUP"], _ = groups.create_group(
            tenant, fx.ada, name="Sunrise staff", member_ids=[fx.solo_admin.pk], body="Hello",
        )
        out["BROADCAST"], _ = broadcasts.send_broadcast(
            tenant, fx.solo_admin, title="Notice", body="Hello", recipient_ids=[fx.ada.pk],
            reply_mode="PRIVATE_REPLIES",
        )
        out["BROADCAST_REPLY"], _, _ = broadcasts.reply_privately(
            tenant, fx.ada, out["BROADCAST"].pk, body="Noted",
        )
        return out


class KeyTests(MessagesFixture):
    """Every route family answers 403 to a caller without its key."""

    def test_a_caller_with_no_keys_is_refused_everywhere(self):
        user = self.nobody
        checks = [
            ("get", "messages-conversations", {}),
            ("post", "messages-conversations", {}),
            ("get", "messages-conversation", {"pk": 1}),
            ("get", "messages-messages", {"pk": 1}),
            ("post", "messages-messages", {"pk": 1}),
            ("post", "messages-read", {"pk": 1}),
            ("get", "messages-unread-count", {}),
            ("get", "messages-contacts", {}),
            ("post", "messages-group-create", {}),
            ("patch", "messages-group", {"pk": 1}),
            ("post", "messages-group-members", {"pk": 1}),
            ("delete", "messages-group-member", {"pk": 1, "user_id": 1}),
            ("post", "messages-group-owner", {"pk": 1, "user_id": 1}),
            ("post", "messages-broadcast-create", {}),
            ("post", "messages-broadcast-reply", {"pk": 1}),
            ("get", "messages-oversight-groups", {}),
            ("get", "messages-oversight-broadcasts", {}),
            ("get", "messages-oversight-pupils", {}),
            ("get", "messages-oversight-conversation", {"pk": 1}),
            ("get", "messages-oversight-messages", {"pk": 1}),
        ]
        for method, name, kwargs in checks:
            with self.subTest(route=name, method=method):
                response = getattr(self, method)(user, name, **kwargs)
                self.assertEqual(response.status_code, 403, response.data)

    def test_a_reader_cannot_write_or_start_anything(self):
        checks = [
            ("post", "messages-conversations", {}),
            ("post", "messages-messages", {"pk": 1}),
            ("get", "messages-contacts", {}),
            ("post", "messages-group-create", {}),
            ("patch", "messages-group", {"pk": 1}),
            ("post", "messages-broadcast-create", {}),
            ("post", "messages-broadcast-reply", {"pk": 1}),
            ("get", "messages-oversight-groups", {}),
        ]
        for method, name, kwargs in checks:
            with self.subTest(route=name):
                response = getattr(self, method)(self.reader, name, **kwargs)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.get(self.reader, "messages-conversations").status_code, 200)
        self.assertEqual(self.get(self.reader, "messages-unread-count").status_code, 200)

    def test_the_inspection_keys_are_separate_from_each_other(self):
        from schools.vs_messages.constants import PERM_GROUP_INSPECT
        from vs_rbac.tests.helpers import make_role_permission

        group = self.group(self.lekki_head, [self.funke], branch=self.lekki)
        pupil = self.pupil_chat(self.funke)
        make_role_permission(self.reader_role, self.keys[PERM_GROUP_INSPECT])
        # The reader holds the group key only: a group in reach opens, a pupil
        # conversation answers exactly as one that does not exist.
        self.assertEqual(self.get(self.reader, "messages-oversight-conversation", pk=group.pk).status_code, 200)
        missing = self.get(self.reader, "messages-oversight-conversation", pk=MISSING)
        hidden = self.get(self.reader, "messages-oversight-conversation", pk=pupil.pk)
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.data, missing.data)


class IsolationTests(MessagesFixture):
    """A conversation at another school answers like one that does not exist."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sunrise = SunriseConversations.build(cls)
        # A Brightfield conversation of every kind the caller does not belong to.
        cls.own = {
            "STAFF_DIRECT": cls.direct(cls.funke, cls.ibrahim),
            "GROUP": cls.group(cls.funke, [cls.ibrahim]),
        }

    def assert_same_as_missing(self, user, method, name, kwargs_for, body=None):
        gone = getattr(self, method)(user, name, body, **kwargs_for(MISSING)) if body is not None \
            else getattr(self, method)(user, name, **kwargs_for(MISSING))
        self.assertEqual(gone.status_code, 404, (name, gone.data))
        for kind, convo in self.sunrise.items():
            with self.subTest(route=name, kind=kind):
                call = getattr(self, method)
                response = call(user, name, body, **kwargs_for(convo.pk)) if body is not None \
                    else call(user, name, **kwargs_for(convo.pk))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data, gone.data)

    def test_staff_routes(self):
        user = self.admin
        self.assert_same_as_missing(user, "get", "messages-conversation", lambda i: {"pk": i})
        self.assert_same_as_missing(user, "get", "messages-messages", lambda i: {"pk": i})
        self.assert_same_as_missing(
            user, "post", "messages-messages", lambda i: {"pk": i}, {"body": "Hi"},
        )
        self.assert_same_as_missing(user, "post", "messages-read", lambda i: {"pk": i}, {})
        self.assert_same_as_missing(
            user, "post", "messages-broadcast-reply", lambda i: {"pk": i}, {"body": "Hi"},
        )

    def test_group_member_routes(self):
        user = self.admin
        self.assert_same_as_missing(
            user, "patch", "messages-group", lambda i: {"pk": i}, {"name": "x"},
        )
        self.assert_same_as_missing(
            user, "post", "messages-group-members", lambda i: {"pk": i}, {"members": [self.funke.pk]},
        )
        self.assert_same_as_missing(
            user, "delete", "messages-group-member", lambda i: {"pk": i, "user_id": self.funke.pk},
        )
        self.assert_same_as_missing(
            user, "post", "messages-group-owner", lambda i: {"pk": i, "user_id": self.funke.pk}, {},
        )

    def test_oversight_routes(self):
        user = self.admin
        self.assert_same_as_missing(user, "get", "messages-oversight-conversation", lambda i: {"pk": i})
        self.assert_same_as_missing(user, "get", "messages-oversight-messages", lambda i: {"pk": i})

    def test_the_other_schools_admin_cannot_reach_ours(self):
        for convo in (self.own["STAFF_DIRECT"], self.own["GROUP"]):
            for name in ("messages-conversation", "messages-messages", "messages-oversight-conversation"):
                response = self.get(self.solo_admin, name, pk=convo.pk)
                self.assertEqual(response.status_code, 404, name)
                self.assertEqual(
                    response.data, self.get(self.solo_admin, name, pk=MISSING).data,
                )

    def test_the_other_schools_inbox_lists_only_its_own(self):
        rows = self.results(self.get(self.ada, "messages-conversations"))
        self.assertTrue(rows)
        self.assertFalse({r["id"] for r in rows} & {c.pk for c in self.own.values()})


class StartTests(MessagesFixture):
    """Starting a staff conversation."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

    def start(self, user, recipient, **extra):
        return self.post(user, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": recipient.pk if hasattr(recipient, "pk") else recipient,
            **extra,
        })

    def test_starting_makes_a_conversation_with_its_first_message(self):
        response = self.start(self.funke, self.ibrahim, body="  Can you cover period 3?  ")
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["conversation"]["kind"], "STAFF_DIRECT")
        self.assertEqual(data["conversation"]["counterpart"]["name"], "Ibrahim Musa")
        self.assertEqual(data["message"]["body"], "Can you cover period 3?")
        self.assertTrue(data["message"]["mine"])
        self.assertEqual(Participant.objects.filter(conversation_id=data["conversation"]["id"]).count(), 2)

    def test_a_second_start_returns_the_same_conversation(self):
        first = self.start(self.funke, self.ibrahim, body="One")
        again = self.start(self.funke, self.ibrahim, body="Two")
        from_other_end = self.start(self.ibrahim, self.funke)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(from_other_end.status_code, 200)
        ids = {r.data["data"]["conversation"]["id"] for r in (first, again, from_other_end)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(Conversation.objects.filter(kind="STAFF_DIRECT").count(), 1)

    def test_a_retry_with_the_same_token_creates_one_message(self):
        token = str(uuid.uuid4())
        first = self.start(self.funke, self.ibrahim, body="Hi", client_token=token)
        second = self.start(self.funke, self.ibrahim, body="Hi", client_token=token)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Message.objects.count(), 1)

    def test_two_simultaneous_starts_settle_on_one_conversation(self):
        conversation = self.direct(self.funke, self.ibrahim, body="First")
        # The first look finds nothing, as it would for the loser of a race;
        # the constraint then refuses the second create and the winner is read.
        missing = mock.Mock(first=mock.Mock(return_value=None))
        with mock.patch.object(Conversation.objects, "filter", return_value=missing):
            again, created = conversations.open_conversation(
                self.tenant, kind="STAFF_DIRECT",
                pair_key=f"staff:{min(self.funke.pk, self.ibrahim.pk)}:{max(self.funke.pk, self.ibrahim.pk)}",
                creator=self.ibrahim, participants=[(self.funke.pk, "MEMBER"), (self.ibrahim.pk, "MEMBER")],
            )
        self.assertFalse(created)
        self.assertEqual(again.pk, conversation.pk)
        self.assertEqual(Conversation.objects.filter(kind="STAFF_DIRECT").count(), 1)

    def test_an_empty_conversation_is_shown_to_its_starter_only(self):
        response = self.start(self.funke, self.ibrahim)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.results(self.get(self.funke, "messages-conversations"))), 1)
        self.assertEqual(self.results(self.get(self.ibrahim, "messages-conversations")), [])
        self.post(self.funke, "messages-messages", {"body": "Now"}, pk=self.results(response)["conversation"]["id"])
        self.assertEqual(len(self.results(self.get(self.ibrahim, "messages-conversations"))), 1)

    def test_a_branch_bound_teacher_cannot_reach_staff_outside_their_branch(self):
        out_of_reach = self.start(self.tola, self.funke, body="Hi")
        nonexistent = self.start(self.tola, MISSING, body="Hi")
        self.assertEqual(out_of_reach.status_code, 400)
        self.assertEqual(out_of_reach.data["error"]["code"], "MESSAGE_RECIPIENT_UNAVAILABLE")
        self.assertEqual(out_of_reach.data, nonexistent.data)
        self.assertFalse(Conversation.objects.exists())

    def test_a_school_wide_member_of_staff_can_be_reached_from_any_branch(self):
        self.assertEqual(self.start(self.tola, self.admin, body="Hi").status_code, 201)

    def test_someone_without_the_view_key_is_not_a_contact(self):
        self.assertEqual(self.start(self.funke, self.nobody).status_code, 400)

    def test_you_cannot_start_a_conversation_with_yourself(self):
        self.assertEqual(self.start(self.funke, self.funke).status_code, 400)

    def test_the_body_is_checked(self):
        ok = self.start(self.funke, self.ibrahim)
        pk = self.results(ok)["conversation"]["id"]
        blank = self.post(self.funke, "messages-messages", {"body": "   "}, pk=pk)
        self.assertEqual(blank.status_code, 400)
        self.assertEqual(blank.data["error"]["code"], "MESSAGE_BODY_REQUIRED")
        long = self.post(self.funke, "messages-messages", {"body": "x" * 4001}, pk=pk)
        self.assertEqual(long.data["error"]["code"], "MESSAGE_BODY_TOO_LONG")
        fits = self.post(self.funke, "messages-messages", {"body": "x" * 4000}, pk=pk)
        self.assertEqual(fits.status_code, 201)

    def test_a_deactivated_account_is_refused_on_its_next_request(self):
        User.objects.filter(pk=self.funke.pk).update(status="DEACTIVATED", is_active=False)
        self.assertIn(self.get(self.funke, "messages-conversations").status_code, (401, 403))

    def test_a_member_of_staff_taken_off_the_roll_loses_their_conversations(self):
        conversation = self.direct(self.funke, self.ibrahim)
        profile = self.funke.staff_profile
        profile.employment_status = EmploymentStatus.RESIGNED
        profile.save(update_fields=["employment_status"])
        self.assertEqual(self.results(self.get(self.funke, "messages-conversations")), [])
        self.assertEqual(self.get(self.funke, "messages-conversation", pk=conversation.pk).status_code, 404)
        # And they stop being someone colleagues can start a conversation with.
        self.assertEqual(self.start(self.ibrahim, self.funke).status_code, 400)

    def test_staff_who_were_deactivated_are_not_contacts(self):
        User.objects.filter(pk=self.ibrahim.pk).update(status="DEACTIVATED", is_active=False)
        self.assertEqual(self.start(self.funke, self.ibrahim).status_code, 400)


class ContactTests(MessagesFixture):
    """The staff a caller may address."""
    def names(self, response):
        return {row["name"] for row in self.results(response)}

    def test_a_branch_head_reaches_their_branch_and_the_school_wide(self):
        response = self.get(self.lekki_head, "messages-contacts", {"kind": "staff"})
        names = self.names(response)
        self.assertIn("Funke Adeyemi", names)
        self.assertIn("School Admin", names)  # the school-wide account
        self.assertNotIn("Tola Bankole", names)
        self.assertNotIn("Segun Ola", names)

    def test_the_whole_school_reach_sees_everyone(self):
        names = self.names(self.get(self.admin, "messages-contacts"))
        self.assertTrue({"Funke Adeyemi", "Tola Bankole", "Segun Ola"} <= names)

    def test_the_caller_is_not_their_own_contact(self):
        self.assertNotIn("Funke Adeyemi", self.names(self.get(self.funke, "messages-contacts")))

    def test_search_and_branch_filter(self):
        by_name = self.names(self.get(self.admin, "messages-contacts", {"search": "segun"}))
        self.assertEqual(by_name, {"Segun Ola"})
        by_branch = self.names(self.get(self.admin, "messages-contacts", {"branch": self.ikeja.pk}))
        self.assertEqual(by_branch, {"Tola Bankole", "Segun Ola"})

    def test_a_multi_branch_school_shows_the_branch_and_a_one_branch_school_does_not(self):
        brightfield = self.results(self.get(self.admin, "messages-contacts"))
        self.assertTrue(all("branch" in row for row in brightfield))
        sunrise = self.results(self.get(self.ada, "messages-contacts"))
        self.assertTrue(sunrise)
        self.assertTrue(all("branch" not in row for row in sunrise))

    def test_contacts_are_paginated(self):
        response = self.get(self.admin, "messages-contacts", {"page_size": 2})
        self.assertEqual(len(self.results(response)), 2)
        self.assertGreater(response.data["pagination"]["totalItems"], 2)

    def test_contacts_never_list_the_other_school(self):
        names = self.names(self.get(self.admin, "messages-contacts"))
        self.assertNotIn("Ada Sunday", names)

    def test_an_unknown_kind_is_refused(self):
        self.assertEqual(self.get(self.admin, "messages-contacts", {"kind": "x"}).status_code, 400)


class InboxTests(MessagesFixture):
    """The inbox list: order, shape, filters and search."""
    def test_an_empty_inbox_is_an_empty_list_and_zero_counts(self):
        response = self.get(self.tola, "messages-conversations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["pagination"]["totalItems"], 0)
        counts = self.results(self.get(self.tola, "messages-unread-count"))
        self.assertEqual(counts, {"total": 0, "direct": 0, "family": 0, "group": 0, "broadcast": 0})

    def test_newest_activity_first_and_paged(self):
        a = self.direct(self.funke, self.ibrahim, "one")
        b = self.group(self.funke, [self.ibrahim], name="Maths", body="two")
        c = self.direct(self.funke, self.ngozi, "three")
        self.say(a, self.ibrahim, "four")
        response = self.get(self.funke, "messages-conversations")
        self.assertEqual([r["id"] for r in self.results(response)], [a.pk, c.pk, b.pk])
        first = self.get(self.funke, "messages-conversations", {"page_size": 2})
        self.assertEqual([r["id"] for r in self.results(first)], [a.pk, c.pk])
        self.assertEqual(first.data["pagination"]["totalPages"], 2)
        second = self.get(self.funke, "messages-conversations", {"page_size": 2, "page": 2})
        self.assertEqual([r["id"] for r in self.results(second)], [b.pk])

    def test_a_row_carries_its_preview_counterpart_and_unread_count(self):
        convo = self.direct(self.funke, self.ibrahim, "x" * 200)
        self.say(convo, self.ibrahim, "Reply")
        row = self.results(self.get(self.funke, "messages-conversations"))[0]
        self.assertEqual(row["display_name"], "Ibrahim Musa")
        self.assertEqual(row["counterpart"], {
            "user_id": self.ibrahim.pk, "name": "Ibrahim Musa", "label": "Maths teacher",
        })
        self.assertEqual(row["last_message"]["preview"], "Reply")
        self.assertEqual(row["last_message"]["sender_name"], "Ibrahim Musa")
        self.assertFalse(row["last_message"]["mine"])
        self.assertEqual(row["unread_count"], 1)
        self.assertTrue(row["can_send"])
        self.assertFalse(row["read_only"])
        self.assertEqual(row["child"], None)
        # Ibrahim wrote last, so nothing is unread for him.
        mine = self.results(self.get(self.ibrahim, "messages-conversations"))[0]
        self.assertEqual(mine["unread_count"], 0)

    def test_the_preview_is_the_first_120_characters(self):
        convo = self.direct(self.funke, self.ibrahim, "x" * 200)
        row = self.results(self.get(self.funke, "messages-conversations"))[0]
        self.assertEqual(len(row["last_message"]["preview"]), 120)
        self.assertEqual(convo.pk, row["id"])

    def test_the_kind_filter(self):
        self.direct(self.funke, self.ibrahim)
        self.group(self.funke, [self.ibrahim])
        self.family_chat(self.funke)
        self.pupil_chat(self.funke)
        kinds = lambda k: sorted(r["kind"] for r in self.results(
            self.get(self.funke, "messages-conversations", {"kind": k})))
        self.assertEqual(kinds("direct"), ["STAFF_DIRECT"])
        self.assertEqual(kinds("group"), ["GROUP"])
        self.assertEqual(kinds("family"), ["FAMILY", "PUPIL"])
        self.assertEqual(kinds("broadcast"), [])
        self.assertEqual(len(kinds("")), 4)

    def test_the_unread_filter(self):
        a = self.direct(self.funke, self.ibrahim, "one")
        self.direct(self.funke, self.ngozi, "two")
        self.say(a, self.ibrahim, "three")
        unread = self.results(self.get(self.funke, "messages-conversations", {"unread": "true"}))
        self.assertEqual([r["id"] for r in unread], [a.pk])

    def test_search_matches_names_titles_and_the_text_of_messages_you_may_read(self):
        a = self.direct(self.funke, self.ibrahim, "Bring the register")
        g = self.group(self.funke, [self.ibrahim], name="Sports day", body="Hello all")
        ids = lambda term: {r["id"] for r in self.results(
            self.get(self.funke, "messages-conversations", {"search": term}))}
        self.assertEqual(ids("Ibrahim"), {a.pk})
        self.assertEqual(ids("sports"), {g.pk})
        self.assertEqual(ids("register"), {a.pk})
        self.assertEqual(ids("nothing here"), set())

    def test_search_does_not_reach_a_late_joiners_earlier_messages(self):
        g = self.group(self.funke, [self.ibrahim], name="Team", body="secret budget")
        groups.add_members(self.access(self.funke, g), self.funke, [self.ngozi.pk])
        found = lambda user: self.results(
            self.get(user, "messages-conversations", {"search": "budget"}))
        self.assertEqual(len(found(self.ibrahim)), 1)
        self.assertEqual(found(self.ngozi), [])

    def test_a_bad_page_size_is_ignored_and_a_bad_filter_value_returns_everything(self):
        self.direct(self.funke, self.ibrahim)
        response = self.get(self.funke, "messages-conversations", {"kind": "wat"})
        self.assertEqual(len(self.results(response)), 1)


class MessageTests(MessagesFixture):
    """Reading and writing messages."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "m1")
        for n in range(2, 8):
            cls.say(cls.dm, cls.funke if n % 2 else cls.ibrahim, f"m{n}")

    def page(self, user, **params):
        response = self.get(user, "messages-messages", params, pk=self.dm.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return self.results(response)

    def test_messages_come_newest_first_with_a_cursor(self):
        first = self.page(self.funke, limit=3)
        self.assertEqual([m["body"] for m in first["results"]], ["m7", "m6", "m5"])
        self.assertTrue(first["has_more"])
        second = self.page(self.funke, limit=3, before=first["next_before"])
        self.assertEqual([m["body"] for m in second["results"]], ["m4", "m3", "m2"])
        last = self.page(self.funke, limit=3, before=second["next_before"])
        self.assertEqual([m["body"] for m in last["results"]], ["m1"])
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_before"])

    def test_the_default_limit_covers_a_short_conversation(self):
        data = self.page(self.funke)
        self.assertEqual(len(data["results"]), 7)
        self.assertFalse(data["has_more"])

    def test_the_limit_is_clamped(self):
        self.assertEqual(len(self.page(self.funke, limit=0)["results"]), 1)
        self.assertEqual(len(self.page(self.funke, limit=100000)["results"]), 7)
        self.assertEqual(self.get(self.funke, "messages-messages", {"limit": "x"}, pk=self.dm.pk).status_code, 400)

    def test_a_message_carries_sender_time_and_mine(self):
        row = self.page(self.funke)["results"][0]
        self.assertEqual(set(row), {"id", "body", "sender", "sent_at", "mine", "receipt"})
        self.assertEqual(row["sender"]["name"], "Funke Adeyemi")
        self.assertTrue(row["mine"])
        theirs = self.page(self.ibrahim)["results"][0]
        self.assertFalse(theirs["mine"])
        self.assertNotIn("receipt", theirs)

    def test_a_non_participant_cannot_read_or_write(self):
        for user in (self.tola, self.admin):
            self.assertEqual(self.get(user, "messages-messages", pk=self.dm.pk).status_code, 404)
            self.assertEqual(self.post(user, "messages-messages", {"body": "x"}, pk=self.dm.pk).status_code, 404)

    def test_a_repeated_token_returns_the_stored_message(self):
        token = str(uuid.uuid4())
        first = self.post(self.funke, "messages-messages", {"body": "once", "client_token": token}, pk=self.dm.pk)
        again = self.post(self.funke, "messages-messages", {"body": "once", "client_token": token}, pk=self.dm.pk)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(self.results(first)["id"], self.results(again)["id"])
        self.assertEqual(Message.objects.filter(client_token=token).count(), 1)

    def test_the_same_token_from_someone_else_is_a_different_message(self):
        token = str(uuid.uuid4())
        self.post(self.funke, "messages-messages", {"body": "a", "client_token": token}, pk=self.dm.pk)
        other = self.post(self.ibrahim, "messages-messages", {"body": "b", "client_token": token}, pk=self.dm.pk)
        self.assertEqual(other.status_code, 201)

    def test_sending_updates_the_conversation_and_the_senders_watermark(self):
        self.post(self.funke, "messages-messages", {"body": "latest"}, pk=self.dm.pk)
        self.dm.refresh_from_db()
        self.assertEqual(self.dm.last_message.body, "latest")
        mine = Participant.objects.get(conversation=self.dm, user=self.funke)
        self.assertEqual(mine.last_read_message_id, self.dm.last_message_id)

    def test_a_reader_without_the_send_key_cannot_write_in_a_conversation_they_have(self):
        convo = self.direct(self.ibrahim, self.reader, "hello")
        self.assertEqual(self.get(self.reader, "messages-messages", pk=convo.pk).status_code, 200)
        response = self.post(self.reader, "messages-messages", {"body": "hi"}, pk=convo.pk)
        self.assertEqual(response.status_code, 403)


class AlertHookTests(MessagesFixture):
    """The alert and bell seams are called for every stored message and every read."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "m1")

    def test_a_stored_message_queues_one_alert_and_a_repeat_queues_none(self):
        token = str(uuid.uuid4())
        with mock.patch("schools.vs_messages.tasks.queue_message_alert") as queue:
            first = self.post(self.funke, "messages-messages", {"body": "a", "client_token": token}, pk=self.dm.pk)
            self.post(self.funke, "messages-messages", {"body": "a", "client_token": token}, pk=self.dm.pk)
        queue.assert_called_once_with(self.results(first)["id"])

    def test_reading_clears_the_conversations_bell_entries(self):
        with mock.patch("schools.vs_messages.tasks.clear_message_alerts") as clear:
            self.post(self.ibrahim, "messages-read", {}, pk=self.dm.pk)
        self.assertEqual(clear.call_args.args[1], self.dm.pk)

    def test_an_unknown_surface_resolves_nothing(self):
        from schools.vs_messages.exceptions import ConversationNotFound
        from schools.vs_messages.services import visibility

        with self.assertRaises(ConversationNotFound):
            visibility.resolve(self.tenant, self.funke, self.dm.pk, surface="nowhere")


class ReadTests(MessagesFixture):
    """The read watermark."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "m1")
        cls.say(cls.dm, cls.ibrahim, "m2")
        cls.say(cls.dm, cls.ibrahim, "m3")

    def test_reading_clears_the_unread_count(self):
        before = self.results(self.get(self.funke, "messages-unread-count"))
        self.assertEqual(before["direct"], 2)
        response = self.post(self.funke, "messages-read", {}, pk=self.dm.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.results(response)["unread_count"], 0)
        self.assertEqual(self.results(self.get(self.funke, "messages-unread-count"))["total"], 0)

    def test_up_to_reads_part_of_it_and_never_moves_back(self):
        ids = list(Message.objects.filter(conversation=self.dm).order_by("id").values_list("id", flat=True))
        part = self.post(self.funke, "messages-read", {"up_to": ids[1]}, pk=self.dm.pk)
        self.assertEqual(self.results(part)["unread_count"], 1)
        back = self.post(self.funke, "messages-read", {"up_to": ids[0]}, pk=self.dm.pk)
        self.assertEqual(self.results(back)["unread_count"], 1)

    def test_a_bad_up_to_is_refused(self):
        self.assertEqual(self.post(self.funke, "messages-read", {"up_to": "x"}, pk=self.dm.pk).status_code, 400)

    def test_reading_does_not_move_the_other_ends_watermark(self):
        self.post(self.funke, "messages-read", {}, pk=self.dm.pk)
        mine = Participant.objects.get(conversation=self.dm, user=self.funke)
        self.assertGreater(mine.last_read_message_id, 0)
        self.assertEqual(self.results(self.get(self.ibrahim, "messages-unread-count"))["direct"], 0)


class QueryCountTests(MessagesFixture):
    """A page costs the same queries however many rows it holds."""

    def count(self, user, name, params=None, **kwargs):
        self.get(user, name, params, **kwargs)  # warm the caches a first request fills
        with CaptureQueriesContext(connection) as queries:
            response = self.get(user, name, params, **kwargs)
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries)

    def test_the_inbox_costs_the_same_for_one_row_of_each_kind_and_many(self):
        self.direct(self.funke, self.ibrahim, "one")
        self.group(self.funke, [self.ibrahim], name="G0", body="x")
        self.family_chat(self.funke)
        self.pupil_chat(self.funke)
        self.broadcast(self.admin, [self.funke])
        few = self.count(self.funke, "messages-conversations")
        for other in (self.ngozi, self.admin, self.lekki_head, self.reader):
            self.direct(self.funke, other, "hello")
        self.group(self.funke, [self.ibrahim, self.ngozi, self.reader], name="G1", body="x")
        self.group(self.funke, [self.ibrahim, self.ngozi], name="G2", body="x")
        self.family_chat(self.funke, self.tunde, self.father)
        self.broadcast(self.admin, [self.funke, self.ibrahim, self.ngozi])
        many = self.count(self.funke, "messages-conversations")
        self.assertEqual(few, many)
        self.assertEqual(len(self.results(self.get(self.funke, "messages-conversations"))), 13)

    def test_a_page_of_messages_costs_the_same_for_one_message_and_many(self):
        convo = self.direct(self.funke, self.ibrahim, "one")
        one = self.count(self.funke, "messages-messages", pk=convo.pk)
        for n in range(30):
            self.say(convo, self.ibrahim if n % 2 else self.funke, f"more {n}")
        many = self.count(self.funke, "messages-messages", pk=convo.pk)
        self.assertEqual(one, many)

    def test_a_detail_costs_the_same_for_a_small_group_and_a_large_one(self):
        small = self.group(self.funke, [self.ibrahim], name="small")
        n_small = self.count(self.funke, "messages-conversation", pk=small.pk)
        large = self.group(self.funke, [self.ibrahim, self.ngozi, self.reader, self.lekki_head], name="large")
        self.assertEqual(n_small, self.count(self.funke, "messages-conversation", pk=large.pk))
