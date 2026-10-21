"""Field Access on messaging: hidden, read-only and writable, for every field.

The caller under test is Mrs. Adeyemi (the teacher role) unless a test needs the
sender of a broadcast or a group's creator, who hold the administrator role. A
role with no switch row reads and writes every field, which is the default the
first test pins down.
"""
from __future__ import annotations

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .base import MessagesFixture

KEY = "school.messages."


class FieldAccessFixture(MessagesFixture):
    """A school with one conversation of every kind and helpers to switch fields."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.messages")
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "Please send the scheme of work")
        cls.say(cls.dm, cls.ibrahim, "Will do tonight")
        cls.team = cls.group(cls.funke, [cls.ibrahim, cls.ngozi], name="Maths team", body="Welcome to the team")
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Your son did well today")
        cls.pupil = cls.pupil_chat(cls.funke, cls.tunde, "Please see me about the quiz")
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim], title="Sports day", body="Friday at 9")

    def hide(self, field, *, role=None, read=False, write=False):
        set_field_access(role or self.teacher_role, KEY + field, read=read, write=write)

    def rows(self, user=None, **params):
        return {r["id"]: r for r in self.results(self.get(user or self.funke, "messages-conversations", params))}

    def detail(self, pk, user=None):
        return self.results(self.get(user or self.funke, "messages-conversation", pk=pk))

    def page(self, pk, user=None):
        return self.results(self.get(user or self.funke, "messages-messages", pk=pk))["results"]


class DefaultTests(FieldAccessFixture):
    """With no switch set, every field is open."""
    def test_every_field_is_open_by_default(self):
        rows = self.rows()
        dm, team = rows[self.dm.pk], rows[self.team.pk]
        for name in ("counterpart", "last_message", "last_activity_at", "unread_count", "display_name"):
            self.assertIn(name, dm)
        self.assertEqual(team["title"], "Maths team")
        self.assertEqual(rows[self.family.pk]["child"]["name"], "Tunde Bello")
        self.assertEqual(rows[self.notice.pk]["reply_mode"], "PRIVATE_REPLIES")
        self.assertEqual(set(dm["last_message"]), {"id", "preview", "sender_name", "sent_at", "mine"})
        message = self.page(self.dm.pk)[0]
        self.assertEqual(set(message), {"id", "body", "sender", "sent_at", "mine"})

    def test_a_detail_names_what_cannot_be_changed(self):
        self.hide("title", read=True, write=False)
        read_only = self.detail(self.team.pk)["_read_only_fields"]
        self.assertIn("title", read_only)
        self.assertIn("unread_count", read_only)
        self.assertIn("last_activity_at", read_only)
        self.assertNotIn("counterpart", read_only)


class BodyTests(FieldAccessFixture):
    """The Message field: payloads, previews, search and sending."""
    def test_a_hidden_message_leaves_messages_previews_and_search(self):
        self.hide("body")
        message = self.page(self.dm.pk)[0]
        self.assertNotIn("body", message)
        self.assertIn("sent_at", message)
        row = self.rows()[self.dm.pk]
        self.assertNotIn("preview", row["last_message"])
        self.assertIn("sent_at", row["last_message"])
        self.assertEqual(self.results(self.get(self.funke, "messages-conversations", {"search": "scheme"})), [])
        # Names still search.
        self.assertEqual(len(self.results(self.get(self.funke, "messages-conversations", {"search": "Ibrahim"}))), 1)

    def test_a_message_that_may_not_be_written_cannot_be_sent(self):
        self.hide("body", read=True, write=False)
        self.assertIn("body", self.page(self.dm.pk)[0])
        for response in (
            self.post(self.funke, "messages-messages", {"body": "Hi"}, pk=self.dm.pk),
            self.post(self.funke, "messages-conversations", {
                "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk, "body": "Hi"}),
            self.post(self.funke, "messages-broadcast-reply", {"body": "Ok"}, pk=self.notice.pk),
        ):
            self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(len(self.page(self.dm.pk)), 2)

    def test_starting_a_conversation_without_a_message_is_still_allowed(self):
        self.hide("body", read=True, write=False)
        response = self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk, "body": ""})
        self.assertEqual(response.status_code, 201)

    def test_a_group_or_broadcast_cannot_carry_text_the_role_may_not_write(self):
        self.hide("body", role=self.role, read=True, write=False)
        group = self.post(self.lekki_head, "messages-group-create", {
            "name": "Staff", "branch": self.lekki.pk, "members": [self.funke.pk], "body": "Hello"})
        notice = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "T", "body": "Hello", "branch": self.lekki.pk, "recipients": [self.funke.pk]})
        self.assertEqual(group.status_code, 403)
        self.assertEqual(notice.status_code, 403)


class ParticipantTests(FieldAccessFixture):
    """The People field: names in every payload, search, contacts and group changes."""
    def test_hidden_people_leave_every_name_in_every_payload(self):
        self.hide("participants")
        rows = self.rows()
        self.assertNotIn("counterpart", rows[self.dm.pk])
        self.assertNotIn("display_name", rows[self.dm.pk])
        self.assertNotIn("sender_name", rows[self.dm.pk]["last_message"])
        self.assertNotIn("counterpart", rows[self.family.pk])
        self.assertNotIn("display_name", rows[self.family.pk])
        self.assertNotIn("display_name", rows[self.pupil.pk])
        # A group is called by its title, which is a different switch.
        self.assertEqual(rows[self.team.pk]["display_name"], "Maths team")
        self.assertNotIn("sender", self.page(self.dm.pk)[0])
        self.assertNotIn("participants", self.detail(self.team.pk))
        self.assertNotIn("participants", self.detail(self.dm.pk))

    def test_a_search_does_not_match_names_the_caller_may_not_read(self):
        found = lambda term: self.results(self.get(self.funke, "messages-conversations", {"search": term}))
        self.assertEqual({r["id"] for r in found("Funke")}, {self.family.pk})
        self.assertEqual({r["id"] for r in found("Ibrahim")}, {self.dm.pk})
        self.hide("participants")
        self.assertEqual(found("Funke"), [])
        self.assertEqual(found("Ibrahim"), [])
        # Titles and message text still search.
        self.assertEqual(len(found("Maths")), 1)
        self.assertEqual(len(found("scheme")), 1)

    def test_the_staff_contact_list_is_closed(self):
        self.hide("participants")
        self.assertEqual(self.get(self.funke, "messages-contacts").status_code, 403)

    def test_the_family_contact_list_loses_the_guardians_but_keeps_the_pupils(self):
        self.hide("participants")
        row = self.results(self.get(self.funke, "messages-contacts", {"kind": "family"}))[0]
        self.assertNotIn("guardians", row)
        self.assertEqual(row["student"]["name"], "Tunde Bello")

    def test_people_are_open_on_create_even_when_they_may_not_be_written(self):
        self.hide("participants", read=True, write=False)
        direct = self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk, "body": "Hi"})
        self.assertEqual(direct.status_code, 201, direct.data)
        self.hide("participants", role=self.role, read=True, write=False)
        group = self.post(self.lekki_head, "messages-group-create", {
            "name": "Staff", "branch": self.lekki.pk, "members": [self.funke.pk]})
        self.assertEqual(group.status_code, 201, group.data)

    def test_changing_a_groups_people_later_needs_the_write_switch(self):
        self.hide("participants", read=True, write=False)
        add = self.post(self.funke, "messages-group-members", {"members": [self.reader.pk]}, pk=self.team.pk)
        remove = self.delete(self.funke, "messages-group-member", pk=self.team.pk, user_id=self.ngozi.pk)
        promote = self.post(self.funke, "messages-group-owner", {}, pk=self.team.pk, user_id=self.ngozi.pk)
        for response in (add, remove, promote):
            self.assertEqual(response.status_code, 403, response.data)
        # Leaving is not refused.
        leave = self.delete(self.ibrahim, "messages-group-member", pk=self.team.pk, user_id=self.ibrahim.pk)
        self.assertEqual(leave.status_code, 200)

    def test_a_label_does_not_name_the_child_when_the_child_is_hidden(self):
        self.hide("child")
        self.assertEqual(self.rows()[self.family.pk]["counterpart"]["label"], "Mother")


class TitleTests(FieldAccessFixture):
    """The Group or broadcast name field."""
    def test_a_hidden_title_leaves_every_name_of_a_group_or_broadcast(self):
        self.hide("title")
        rows = self.rows()
        for pk in (self.team.pk, self.notice.pk):
            self.assertNotIn("title", rows[pk])
            self.assertNotIn("display_name", rows[pk])
        # A person's name is a different switch.
        self.assertEqual(rows[self.dm.pk]["display_name"], "Ibrahim Musa")
        self.assertEqual(self.results(self.get(self.funke, "messages-conversations", {"search": "Maths"})), [])

    def test_renaming_needs_the_write_switch_and_creating_does_not(self):
        self.hide("title", role=self.role, read=True, write=False)
        self.hide("title", read=True, write=False)
        rename = self.patch(self.funke, "messages-group", {"name": "Renamed"}, pk=self.team.pk)
        self.assertEqual(rename.status_code, 403)
        again = self.patch(self.funke, "messages-group", {"name": "Maths team"}, pk=self.team.pk)
        self.assertEqual(again.status_code, 200)  # unchanged value is not a change
        created = self.post(self.lekki_head, "messages-group-create", {
            "name": "Brand new", "branch": self.lekki.pk, "members": []})
        self.assertEqual(created.status_code, 201)
        broadcast = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "Brand new", "body": "Hello", "branch": self.lekki.pk, "recipients": [self.funke.pk]})
        self.assertEqual(broadcast.status_code, 201)


class RecipientTests(FieldAccessFixture):
    """The Broadcast recipients field."""
    def test_hidden_recipients_leave_the_list_and_the_count(self):
        self.hide("recipients", role=self.role)
        sender = self.detail(self.notice.pk, self.lekki_head)
        self.assertNotIn("recipients", sender)
        self.assertNotIn("recipient_count", sender)
        row = self.results(self.get(self.admin, "messages-oversight-broadcasts"))[0]
        self.assertNotIn("recipient_count", row)
        self.assertIn("recipient_count", self.detail(self.notice.pk))  # the teacher role still reads it
        self.hide("recipients")
        self.assertNotIn("recipient_count", self.detail(self.notice.pk))

    def test_addressing_a_broadcast_needs_the_write_switch(self):
        self.hide("recipients", role=self.role, read=True, write=False)
        response = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "T", "body": "Hello", "branch": self.lekki.pk, "recipients": [self.funke.pk]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")


class ChildTests(FieldAccessFixture):
    """The Pupil field: the block, the label, search and starting."""
    def test_a_hidden_child_leaves_the_block_and_the_search(self):
        self.hide("child")
        rows = self.rows()
        self.assertNotIn("child", rows[self.family.pk])
        self.assertNotIn("child", rows[self.pupil.pk])
        # The child's name only matched through the child; the guardian's name is a person.
        found = self.results(self.get(self.funke, "messages-conversations", {"search": "Tunde"}))
        self.assertEqual({r["id"] for r in found}, {self.pupil.pk})  # the pupil is also a person here
        self.assertEqual(self.results(self.get(self.funke, "messages-conversations", {"search": "Funke Bello"}))[0]["id"], self.family.pk)
        self.hide("participants")
        self.assertEqual(self.results(self.get(self.funke, "messages-conversations", {"search": "Tunde"})), [])

    def test_naming_a_child_on_a_new_conversation_needs_the_write_switch(self):
        self.hide("child", read=True, write=False)
        response = self.post(self.funke, "messages-conversations", {
            "kind": "FAMILY", "student": self.tunde.pk, "guardian": self.father.pk, "body": "Hi"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_the_family_contact_list_loses_the_pupil_block_when_the_child_is_hidden(self):
        self.hide("child")
        row = self.results(self.get(self.funke, "messages-contacts", {"kind": "family"}))[0]
        self.assertNotIn("student", row)
        self.assertEqual(len(row["guardians"]), 2)

    def test_with_nothing_readable_the_family_contacts_are_closed(self):
        self.hide("child")
        self.hide("participants")
        self.assertEqual(self.get(self.funke, "messages-contacts", {"kind": "family"}).status_code, 403)

    def test_oversight_follows_the_same_switch(self):
        self.hide("child", role=self.role)
        row = self.results(self.get(self.admin, "messages-oversight-pupils"))[0]
        self.assertNotIn("child", row)
        self.assertEqual(
            self.results(self.get(self.admin, "messages-oversight-pupils", {"search": "Tunde"}))[0]["id"],
            self.pupil.pk,
        )  # still a person in the conversation


class ReplyModeTests(FieldAccessFixture):
    """The Broadcast reply setting field."""
    def test_a_hidden_reply_setting_leaves_the_row(self):
        self.hide("reply_mode")
        self.assertNotIn("reply_mode", self.rows()[self.notice.pk])

    def test_choosing_it_needs_the_write_switch_and_omitting_it_gives_one_way(self):
        self.hide("reply_mode", role=self.role, read=True, write=False)
        chosen = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "T", "body": "Hi", "branch": self.lekki.pk, "recipients": [self.funke.pk],
            "reply_mode": "PRIVATE_REPLIES"})
        self.assertEqual(chosen.status_code, 403)
        omitted = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "T", "body": "Hi", "branch": self.lekki.pk, "recipients": [self.funke.pk]})
        self.assertEqual(omitted.status_code, 201)
        self.assertEqual(self.results(omitted)["reply_mode"], "ONE_WAY")


class StatusTests(FieldAccessFixture):
    """The read-only Sent times and Read status fields."""
    def test_hidden_sent_times_leave_every_timestamp(self):
        self.hide("sent_at")
        row = self.rows()[self.dm.pk]
        self.assertNotIn("last_activity_at", row)
        self.assertNotIn("sent_at", row["last_message"])
        self.assertNotIn("sent_at", self.page(self.dm.pk)[0])
        oversight = self.results(self.get(self.admin, "messages-oversight-groups"))[0]
        self.assertIn("last_activity_at", oversight)

    def test_hidden_read_status_leaves_counts_and_closes_the_total(self):
        self.hide("read_status")
        self.assertNotIn("unread_count", self.rows()[self.dm.pk])
        response = self.get(self.funke, "messages-unread-count")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_read_denied")
        read = self.post(self.funke, "messages-read", {}, pk=self.dm.pk)
        self.assertNotIn("unread_count", self.results(read))

    def test_these_two_cannot_be_written_at_all(self):
        from vs_rbac.models import FieldDefinition

        for name in ("sent_at", "read_status"):
            self.assertFalse(FieldDefinition.objects.get(key=KEY + name).writable)
        for name in ("body", "title", "participants", "recipients", "child", "reply_mode"):
            self.assertTrue(FieldDefinition.objects.get(key=KEY + name).writable)

    def test_only_the_title_and_the_people_are_open_on_create(self):
        from vs_rbac.models import FieldDefinition

        opened = set(
            FieldDefinition.objects.filter(
                resource__module_id="school", resource__name="messages", open_on_create=True,
            ).values_list("name", flat=True)
        )
        self.assertEqual(opened, {"title", "participants"})


class OversightSwitchTests(FieldAccessFixture):
    """The switches apply to a leader reading a conversation too."""
    def test_a_hidden_message_leaves_an_inspected_conversation_and_hidden_people_leave_it_too(self):
        self.hide("body", role=self.role)
        self.hide("participants", role=self.role)
        page = self.results(self.get(self.admin, "messages-oversight-messages", pk=self.team.pk))["results"]
        self.assertTrue(page)
        self.assertNotIn("body", page[0])
        self.assertNotIn("sender", page[0])
        detail = self.results(self.get(self.admin, "messages-oversight-conversation", pk=self.team.pk))
        self.assertNotIn("members", detail)
        self.assertNotIn("participants", detail)
        self.assertEqual(detail["display_name"], "Maths team")
