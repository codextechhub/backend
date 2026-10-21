"""Staff groups: making one, what a late joiner may read, and who may change it.

The history rule is exact: a member added to a group reads messages with an id
greater than the group's newest at the moment they joined, in the message list,
the preview, search and the unread count alike.
"""
from __future__ import annotations

import uuid

from schools.vs_messages.models import Message, Participant
from schools.vs_messages.services import groups, messages, visibility

from .base import MessagesFixture, audit_events

MISSING = 999999


class CreateTests(MessagesFixture):
    """Making a group."""
    def make(self, user, **body):
        body.setdefault("name", "Maths team")
        body.setdefault("branch", self.lekki.pk)
        return self.post(user, "messages-group-create", body)

    def test_a_group_is_made_with_its_creator_as_owner(self):
        response = self.make(self.lekki_head, members=[self.ibrahim.pk, self.ngozi.pk], body="Welcome")
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["kind"], "GROUP")
        self.assertEqual(data["title"], "Maths team")
        self.assertEqual(data["display_name"], "Maths team")
        self.assertEqual(data["member_count"], 3)
        self.assertEqual(data["branch"], {"id": self.lekki.pk, "name": "Lekki"})
        self.assertEqual(data["role"], "OWNER")
        roles = {p["name"]: p["role"] for p in data["participants"]}
        self.assertEqual(roles, {
            "School Admin": "OWNER", "Ibrahim Musa": "MEMBER", "Ngozi Okoro": "MEMBER",
        })
        self.assertEqual(data["last_message"]["preview"], "Welcome")

    def test_only_staff_can_be_members(self):
        for user in (self.mother.user, self.tunde.user):
            response = self.make(self.lekki_head, members=[self.ibrahim.pk, user.pk])
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["error"]["code"], "MESSAGE_RECIPIENT_UNAVAILABLE")

    def test_an_out_of_reach_member_is_refused_like_one_who_does_not_exist(self):
        out = self.make(self.lekki_head, members=[self.tola.pk])
        gone = self.make(self.lekki_head, members=[MISSING])
        self.assertEqual(out.status_code, 400)
        self.assertEqual(out.data, gone.data)

    def test_a_branch_group_takes_staff_of_that_branch_or_school_wide_only(self):
        response = self.make(self.admin, members=[self.tola.pk], branch=self.lekki.pk)
        self.assertEqual(response.status_code, 400)
        ok = self.make(self.admin, members=[self.funke.pk, self.lekki_head.pk], branch=self.lekki.pk)
        self.assertEqual(ok.status_code, 201)
        school_wide = self.make(self.admin, members=[self.funke.pk, self.tola.pk], branch=None)
        self.assertEqual(school_wide.status_code, 201)
        self.assertIsNone(self.results(school_wide)["branch"])

    def test_a_branch_bound_creator_cannot_make_a_school_wide_group_or_another_branchs(self):
        wide = self.make(self.lekki_head, members=[self.funke.pk], branch=None)
        self.assertEqual(wide.status_code, 403)
        self.assertEqual(wide.data["error"]["code"], "MESSAGE_BRANCH_OUT_OF_REACH")
        other = self.make(self.lekki_head, members=[], branch=self.ikeja.pk)
        self.assertEqual(other.status_code, 403)
        nonexistent = self.make(self.lekki_head, members=[], branch=MISSING)
        self.assertEqual(nonexistent.data, other.data)

    def test_a_group_needs_a_name_within_the_limit(self):
        blank = self.make(self.lekki_head, name="  ", members=[])
        self.assertEqual(blank.data["error"]["code"], "MESSAGE_TITLE_INVALID")
        self.assertEqual(self.make(self.lekki_head, name="x" * 151, members=[]).status_code, 400)
        self.assertEqual(self.make(self.lekki_head, name="x" * 150, members=[]).status_code, 201)

    def test_the_creating_key_is_required(self):
        self.assertEqual(self.make(self.ibrahim, members=[]).status_code, 403)

    def test_a_retried_create_with_the_same_token_returns_the_first_group(self):
        token = str(uuid.uuid4())
        first = self.make(self.lekki_head, members=[self.ibrahim.pk], body="Hi", client_token=token)
        again = self.make(self.lekki_head, members=[self.ibrahim.pk], body="Hi", client_token=token)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(self.results(first)["id"], self.results(again)["id"])

    def test_creating_is_audited_without_the_text(self):
        self.make(self.lekki_head, members=[self.ibrahim.pk], body="a secret plan")
        event = audit_events("MESSAGE_GROUP_CREATED").get()
        self.assertEqual(event.entity_type, "conversation")
        self.assertNotIn("secret", event.summary)
        self.assertNotIn("secret", str(event.metadata))


class LateJoinerTests(MessagesFixture):
    """What a member added later may read, and what leaving ends."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.team = cls.group(cls.funke, [cls.ibrahim], name="Team", body="before one")
        cls.say(cls.team, cls.ibrahim, "before two")

    def join(self, *users):
        groups.add_members(self.access(self.funke, self.team), self.funke, [u.pk for u in users])

    def page(self, user):
        return self.results(self.get(user, "messages-messages", pk=self.team.pk))["results"]

    def test_a_new_member_reads_nothing_said_before_they_joined(self):
        self.join(self.ngozi)
        self.assertEqual(self.page(self.ngozi), [])
        mine = Participant.objects.get(conversation=self.team, user=self.ngozi)
        newest = Message.objects.filter(conversation=self.team).order_by("-id").first()
        self.assertEqual(mine.after_message_id, newest.pk)
        self.say(self.team, self.funke, "after")
        self.assertEqual([m["body"] for m in self.page(self.ngozi)], ["after"])

    def test_the_boundary_is_exact(self):
        self.join(self.ngozi)
        boundary = Participant.objects.get(conversation=self.team, user=self.ngozi).after_message_id
        # The message at the boundary is hidden, the next one is not.
        self.assertTrue(Message.objects.filter(pk=boundary).exists())
        after = self.say(self.team, self.funke, "after")
        self.assertEqual(after.pk > boundary, True)
        ids = [m["id"] for m in self.page(self.ngozi)]
        self.assertEqual(ids, [after.pk])
        self.assertNotIn(boundary, ids)

    def test_a_new_member_has_no_preview_unread_count_or_search_hit_for_the_past(self):
        self.join(self.ngozi)
        row = [r for r in self.results(self.get(self.ngozi, "messages-conversations"))
               if r["id"] == self.team.pk][0]
        self.assertIsNone(row["last_message"])
        self.assertEqual(row["unread_count"], 0)
        self.assertEqual(visibility.unread_counts(self.tenant, self.ngozi)["group"], 0)
        found = self.results(self.get(self.ngozi, "messages-conversations", {"search": "before"}))
        self.assertEqual(found, [])
        self.say(self.team, self.funke, "after")
        row = [r for r in self.results(self.get(self.ngozi, "messages-conversations"))
               if r["id"] == self.team.pk][0]
        self.assertEqual(row["last_message"]["preview"], "after")
        self.assertEqual(row["unread_count"], 1)
        self.assertEqual(
            len(self.results(self.get(self.ngozi, "messages-conversations", {"search": "after"}))), 1,
        )

    def test_a_removed_member_loses_all_access(self):
        self.join(self.ngozi)
        self.say(self.team, self.funke, "while in")
        response = self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.ngozi.pk)
        self.assertEqual(response.status_code, 200, response.data)
        for name in ("messages-conversation", "messages-messages"):
            self.assertEqual(self.get(self.ngozi, name, pk=self.team.pk).status_code, 404)
        self.assertEqual(self.results(self.get(self.ngozi, "messages-conversations")), [])
        self.assertEqual(
            self.post(self.ngozi, "messages-messages", {"body": "x"}, pk=self.team.pk).status_code, 404,
        )

    def test_a_member_added_again_sees_only_what_is_said_after_returning(self):
        self.join(self.ngozi)
        self.say(self.team, self.funke, "while in")
        self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.ngozi.pk)
        self.say(self.team, self.funke, "while out")
        self.join(self.ngozi)
        self.assertEqual(self.page(self.ngozi), [])
        self.say(self.team, self.funke, "back")
        self.assertEqual([m["body"] for m in self.page(self.ngozi)], ["back"])
        periods = Participant.objects.filter(conversation=self.team, user=self.ngozi)
        self.assertEqual(periods.count(), 2)
        self.assertEqual(periods.filter(left_at__isnull=True).count(), 1)

    def test_an_inspector_reads_all_of_it_without_joining(self):
        self.join(self.ngozi)
        before = Participant.objects.filter(conversation=self.team).count()
        response = self.get(self.admin, "messages-oversight-messages", pk=self.team.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(m["body"] for m in self.results(response)["results"]),
            ["before one", "before two"],
        )
        self.assertTrue(self.results(response)["read_only"])
        self.assertEqual(Participant.objects.filter(conversation=self.team).count(), before)
        self.assertFalse(Participant.objects.filter(conversation=self.team, user=self.admin).exists())


class OwnerRuleTests(MessagesFixture):
    """Only an owner changes a group; the last owner stays."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.team = cls.group(cls.funke, [cls.ibrahim, cls.ngozi], name="Team", body="hello")

    def test_a_member_cannot_rename_add_remove_or_promote(self):
        pk = self.team.pk
        checks = [
            self.patch(self.ibrahim, "messages-group", {"name": "Mine"}, pk=pk),
            self.post(self.ibrahim, "messages-group-members", {"members": [self.reader.pk]}, pk=pk),
            self.delete(self.ibrahim, "messages-group-member", pk=pk, user_id=self.ngozi.pk),
            self.post(self.ibrahim, "messages-group-owner", {}, pk=pk, user_id=self.ngozi.pk),
        ]
        for response in checks:
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(response.data["error"]["code"], "GROUP_OWNER_ONLY")

    def test_an_owner_renames_and_it_is_audited(self):
        response = self.patch(self.funke, "messages-group", {"name": "Maths department"}, pk=self.team.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.results(response)["title"], "Maths department")
        event = audit_events("MESSAGE_GROUP_RENAMED").get()
        self.assertIn("Maths department", event.summary)

    def test_an_owner_adds_staff_but_not_people_out_of_reach(self):
        ok = self.post(self.funke, "messages-group-members", {"members": [self.reader.pk]}, pk=self.team.pk)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.results(ok)["member_count"], 4)
        out = self.post(self.funke, "messages-group-members", {"members": [self.tola.pk]}, pk=self.team.pk)
        self.assertEqual(out.status_code, 400)
        guardian = self.post(self.funke, "messages-group-members", {"members": [self.mother.user.pk]}, pk=self.team.pk)
        self.assertEqual(guardian.status_code, 400)

    def test_adding_someone_already_in_does_nothing(self):
        self.post(self.funke, "messages-group-members", {"members": [self.ibrahim.pk]}, pk=self.team.pk)
        self.assertEqual(
            Participant.objects.filter(conversation=self.team, user=self.ibrahim, left_at__isnull=True).count(), 1,
        )

    def test_the_last_owner_cannot_leave_while_others_remain(self):
        response = self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.funke.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "GROUP_NEEDS_OWNER")
        self.post(self.funke, "messages-group-owner", {}, pk=self.team.pk, user_id=self.ibrahim.pk)
        left = self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.funke.pk)
        self.assertEqual(left.status_code, 200)
        self.assertEqual(self.get(self.funke, "messages-conversation", pk=self.team.pk).status_code, 404)

    def test_a_member_can_leave_and_the_last_person_can_go(self):
        self.assertEqual(
            self.delete(self.ngozi, "messages-group-member", pk=self.team.pk, user_id=self.ngozi.pk).status_code, 200,
        )
        self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.ibrahim.pk)
        self.assertEqual(
            self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.funke.pk).status_code, 200,
        )

    def test_removing_someone_who_is_not_in_the_group(self):
        response = self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.tola.pk)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "GROUP_MEMBER_NOT_FOUND")

    def test_membership_changes_are_audited(self):
        self.post(self.funke, "messages-group-members", {"members": [self.reader.pk]}, pk=self.team.pk)
        self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.reader.pk)
        self.post(self.funke, "messages-group-owner", {}, pk=self.team.pk, user_id=self.ibrahim.pk)
        self.assertEqual(audit_events("MESSAGE_GROUP_MEMBERS_CHANGED").count(), 3)

    def test_the_group_routes_refuse_other_kinds_of_conversation(self):
        dm = self.direct(self.funke, self.ibrahim)
        self.assertEqual(self.patch(self.funke, "messages-group", {"name": "x"}, pk=dm.pk).status_code, 404)
        self.assertEqual(
            self.delete(self.funke, "messages-group-member", pk=dm.pk, user_id=self.ibrahim.pk).status_code, 404,
        )

    def test_a_member_can_write_and_everyone_reads_the_new_message(self):
        self.assertEqual(
            self.post(self.ibrahim, "messages-messages", {"body": "Hi all"}, pk=self.team.pk).status_code, 201,
        )
        self.assertEqual(
            len(self.results(self.get(self.ngozi, "messages-messages", pk=self.team.pk))["results"]), 2,
        )

    def test_unread_for_a_group_after_someone_speaks(self):
        self.say(self.team, self.ibrahim, "Hi all")
        messages.mark_read(self.access(self.ngozi, self.team), self.ngozi)
        self.say(self.team, self.ibrahim, "One more")
        self.assertEqual(visibility.unread_counts(self.tenant, self.ngozi)["group"], 1)
