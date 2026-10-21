"""Leaders reading groups, broadcasts and pupil conversations, read-only and on the record.

Brightfield's admin reaches the whole school; the Lekki head reaches Lekki. Tunde
is at Lekki and Kemi at Ikeja, so the head can inspect Tunde's conversation and
not Kemi's.
"""
from __future__ import annotations

from schools.vs_messages.models import Participant
from schools.vs_messages.services import messages, visibility

from .base import MessagesFixture, audit_events

MISSING = 999999


class WelfareTests(MessagesFixture):
    """Inspecting pupil conversations."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde_chat = cls.pupil_chat(cls.funke, cls.tunde, "Please see me after class")
        cls.say(cls.tunde_chat, cls.tunde.user, "Yes miss")
        cls.kemi_chat = cls.pupil_chat(cls.tola, cls.kemi, "Hello Kemi")
        cls.family = cls.family_chat(cls.funke)
        cls.dm = cls.direct(cls.funke, cls.ibrahim)

    def test_a_holder_in_reach_reads_it_read_only(self):
        detail = self.get(self.lekki_head, "messages-oversight-conversation", pk=self.tunde_chat.pk)
        self.assertEqual(detail.status_code, 200, detail.data)
        data = self.results(detail)
        self.assertTrue(data["read_only"])
        self.assertTrue(data["inspection"])
        self.assertEqual(data["child"]["name"], "Tunde Bello")
        self.assertEqual(data["display_name"], "Tunde Bello")
        page = self.results(self.get(self.lekki_head, "messages-oversight-messages", pk=self.tunde_chat.pk))
        self.assertEqual(
            [m["body"] for m in page["results"]], ["Yes miss", "Please see me after class"],
        )
        self.assertTrue(page["read_only"] and page["inspection"])

    def test_every_opening_is_one_audit_event_and_never_carries_the_text(self):
        self.get(self.lekki_head, "messages-oversight-conversation", pk=self.tunde_chat.pk)
        self.get(self.lekki_head, "messages-oversight-messages", pk=self.tunde_chat.pk)
        self.get(self.lekki_head, "messages-oversight-messages", pk=self.tunde_chat.pk)
        events = audit_events("CONVERSATION_INSPECTED")
        self.assertEqual(events.count(), 3)
        event = events.order_by("event_at").first()
        self.assertEqual(event.actor_user_id, self.lekki_head.pk)
        self.assertEqual(event.entity_id, str(self.tunde_chat.pk))
        self.assertEqual(event.metadata["surface"], "inspect_welfare")
        self.assertEqual(event.metadata["kind"], "PUPIL")
        for e in events:
            self.assertNotIn("Yes miss", e.summary + str(e.metadata))

    def test_a_refused_inspection_is_not_audited(self):
        self.get(self.lekki_head, "messages-oversight-conversation", pk=self.kemi_chat.pk)
        self.assertFalse(audit_events("CONVERSATION_INSPECTED").exists())

    def test_the_watermarks_do_not_move(self):
        before = list(Participant.objects.order_by("pk").values_list("pk", "last_read_message_id", "last_read_at"))
        count = Participant.objects.count()
        self.get(self.admin, "messages-oversight-messages", pk=self.tunde_chat.pk)
        self.get(self.admin, "messages-oversight-conversation", pk=self.tunde_chat.pk)
        after = list(Participant.objects.order_by("pk").values_list("pk", "last_read_message_id", "last_read_at"))
        self.assertEqual(before, after)
        self.assertEqual(Participant.objects.count(), count)

    def test_a_holder_whose_reach_excludes_the_childs_branch_gets_not_found(self):
        missing = self.get(self.lekki_head, "messages-oversight-conversation", pk=MISSING)
        hidden = self.get(self.lekki_head, "messages-oversight-conversation", pk=self.kemi_chat.pk)
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.data, missing.data)
        self.assertEqual(
            self.get(self.lekki_head, "messages-oversight-messages", pk=self.kemi_chat.pk).status_code, 404,
        )
        self.assertEqual(self.get(self.admin, "messages-oversight-conversation", pk=self.kemi_chat.pk).status_code, 200)

    def test_family_and_staff_conversations_never_resolve_here(self):
        missing = self.get(self.admin, "messages-oversight-conversation", pk=MISSING)
        for convo in (self.family, self.dm):
            for name in ("messages-oversight-conversation", "messages-oversight-messages"):
                response = self.get(self.admin, name, pk=convo.pk)
                self.assertEqual(response.status_code, 404, (convo.kind, name))
                self.assertEqual(response.data, missing.data)

    def test_the_list_holds_only_pupil_conversations_in_reach_with_no_text(self):
        rows = self.results(self.get(self.lekki_head, "messages-oversight-pupils"))
        self.assertEqual([r["id"] for r in rows], [self.tunde_chat.pk])
        row = rows[0]
        self.assertNotIn("last_message", row)
        self.assertNotIn("preview", row)
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["child"]["class_name"], "JSS1 B")
        self.assertEqual({p["name"] for p in row["participants"]}, {"Tunde Bello", "Funke Adeyemi"})
        self.assertEqual(row["branch"]["name"], "Lekki")
        everyone = self.results(self.get(self.admin, "messages-oversight-pupils"))
        self.assertEqual({r["id"] for r in everyone}, {self.tunde_chat.pk, self.kemi_chat.pk})

    def test_the_list_searches_by_the_childs_name(self):
        rows = self.results(self.get(self.admin, "messages-oversight-pupils", {"search": "kemi"}))
        self.assertEqual([r["id"] for r in rows], [self.kemi_chat.pk])

    def test_an_inspector_who_also_takes_part_is_audited_all_the_same(self):
        own = self.group(self.lekki_head, [self.funke], name="Heads and leads")
        self.get(self.lekki_head, "messages-oversight-conversation", pk=own.pk)
        self.assertEqual(audit_events("CONVERSATION_INSPECTED").count(), 1)

    def test_someone_without_the_welfare_key_cannot_inspect_a_pupil_conversation_they_take_part_in(self):
        chat = self.pupil_chat(self.ngozi, self.chiamaka, "Hello")
        self.assertEqual(self.get(self.ngozi, "messages-oversight-conversation", pk=chat.pk).status_code, 403)
        self.assertFalse(audit_events("CONVERSATION_INSPECTED").exists())

    def test_the_key_for_the_kind_is_the_key_that_opens_it(self):
        group = self.group(self.funke, [self.ibrahim])
        for name in ("messages-oversight-conversation", "messages-oversight-messages"):
            self.assertEqual(self.get(self.admin, name, pk=group.pk).status_code, 200)

    def test_the_pupil_still_cannot_be_written_to_through_inspection(self):
        access = visibility.resolve(self.tenant, self.admin, self.tunde_chat.pk, surface="inspect_welfare")
        self.assertFalse(access.can_send)
        with self.assertRaises(Exception) as caught:
            messages.send_message(access, sender=self.admin, body="x")
        self.assertEqual(caught.exception.error_code, "CONVERSATION_READ_ONLY")


class GroupAndBroadcastListTests(MessagesFixture):
    """Inspecting groups and broadcasts, and their lists."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_group = cls.group(cls.funke, [cls.ibrahim, cls.ngozi], name="Lekki staff")
        cls.wide_group = cls.group(cls.admin, [cls.funke, cls.tola], name="All staff", branch=None)
        cls.ikeja_group = cls.group(cls.tola, [cls.segun], name="Ikeja staff")
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim], title="Lekki notice")
        cls.wide_notice = cls.broadcast(cls.admin, [cls.funke, cls.tola], title="Everyone", branch=None)

    def ids(self, user, name, **params):
        return {r["id"] for r in self.results(self.get(user, name, params))}

    def test_a_head_sees_the_groups_of_their_branch_only(self):
        self.assertEqual(self.ids(self.lekki_head, "messages-oversight-groups"), {self.lekki_group.pk})

    def test_a_school_wide_group_needs_a_school_wide_reach(self):
        self.assertIn(self.wide_group.pk, self.ids(self.admin, "messages-oversight-groups"))
        self.assertNotIn(self.wide_group.pk, self.ids(self.lekki_head, "messages-oversight-groups"))
        self.assertEqual(
            self.get(self.lekki_head, "messages-oversight-conversation", pk=self.wide_group.pk).status_code, 404,
        )

    def test_the_whole_school_reach_sees_every_group(self):
        self.assertEqual(
            self.ids(self.admin, "messages-oversight-groups"),
            {self.lekki_group.pk, self.wide_group.pk, self.ikeja_group.pk},
        )

    def test_broadcasts_by_reach(self):
        self.assertEqual(self.ids(self.lekki_head, "messages-oversight-broadcasts"), {self.notice.pk})
        self.assertEqual(
            self.ids(self.admin, "messages-oversight-broadcasts"), {self.notice.pk, self.wide_notice.pk},
        )

    def test_a_group_row_names_its_owner_and_counts_without_listing_members(self):
        row = [r for r in self.results(self.get(self.admin, "messages-oversight-groups"))
               if r["id"] == self.lekki_group.pk][0]
        self.assertEqual(row["display_name"], "Lekki staff")
        self.assertEqual([p["name"] for p in row["participants"]], ["Funke Adeyemi"])
        self.assertEqual(row["member_count"], 3)
        self.assertEqual(row["message_count"], 1)
        self.assertEqual(row["branch"]["name"], "Lekki")
        self.assertNotIn("recipient_count", row)

    def test_a_broadcast_row_has_a_recipient_count(self):
        row = self.results(self.get(self.lekki_head, "messages-oversight-broadcasts"))[0]
        self.assertEqual(row["recipient_count"], 2)
        self.assertNotIn("member_count", row)

    def test_search_by_name_and_by_member(self):
        self.assertEqual(
            self.ids(self.admin, "messages-oversight-groups", search="Ikeja"), {self.ikeja_group.pk},
        )
        self.assertEqual(
            self.ids(self.admin, "messages-oversight-groups", search="Segun"), {self.ikeja_group.pk},
        )

    def test_detail_of_a_group_lists_every_member(self):
        data = self.results(self.get(self.admin, "messages-oversight-conversation", pk=self.lekki_group.pk))
        self.assertEqual(len(data["members"]), 3)
        self.assertTrue(data["read_only"])

    def test_a_group_inspection_is_audited_every_time(self):
        self.get(self.admin, "messages-oversight-conversation", pk=self.lekki_group.pk)
        self.get(self.admin, "messages-oversight-messages", pk=self.lekki_group.pk)
        events = audit_events("CONVERSATION_INSPECTED")
        self.assertEqual(events.count(), 2)
        self.assertEqual({e.metadata["surface"] for e in events}, {"inspect_group"})

    def test_oversight_lists_are_paginated_and_empty_is_a_list(self):
        response = self.get(self.admin, "messages-oversight-groups", {"page_size": 2})
        self.assertEqual(len(self.results(response)), 2)
        self.assertEqual(response.data["pagination"]["totalItems"], 3)
        self.assertEqual(self.results(self.get(self.admin, "messages-oversight-pupils")), [])

    def test_a_one_branch_school_shows_no_branch_on_an_oversight_row(self):
        from schools.vs_messages.services import groups

        groups.create_group(
            self.solo.tenant, self.ada, name="Sunrise staff", member_ids=[self.solo_admin.pk],
        )
        rows = self.results(self.get(self.solo_admin, "messages-oversight-groups"))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("branch", rows[0])
