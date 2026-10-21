"""Downloading a conversation as a CSV file.

The administrator Mrs. Adaeze and the Lekki head are the only holders of the
export key at Brightfield; the test class also gives it to the teacher role so a
member added to a group later can download what they were able to read.
"""
from __future__ import annotations

import csv
import io

from schools.vs_messages.constants import PERM_EXPORT
from schools.vs_messages.services import groups
from vs_config.display import format_datetime
from vs_rbac.tests.helpers import install_declared_fields, make_role_permission, set_field_access

from .base import MessagesFixture, audit_events

MISSING = 999999
KEY = "school.messages."


class ExportFixture(MessagesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.messages")
        make_role_permission(cls.teacher_role, cls.keys[PERM_EXPORT])
        cls.dm = cls.direct(cls.lekki_head, cls.funke, "Hello Mrs Adeyemi")
        cls.reply = cls.say(cls.dm, cls.funke, "=HYPERLINK(\"http://evil.example\",\"click\")")
        cls.say(cls.dm, cls.lekki_head, "+1 for that, -2 for the other, @team")
        cls.say(cls.dm, cls.funke, "A plain line, with a comma")

    def export(self, user, conversation=None, pk=None):
        return self.get(user, "messages-export", pk=pk or conversation.pk)

    def rows(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, "data", ""))
        return list(csv.reader(io.StringIO(response.content.decode("utf-8"))))

    def hide(self, field, role):
        set_field_access(role, KEY + field, read=False, write=False)


class ExportTests(ExportFixture):
    def test_a_participant_downloads_the_conversation_oldest_first(self):
        response = self.export(self.lekki_head, self.dm)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"], f'attachment; filename="conversation-{self.dm.pk}.csv"',
        )
        rows = self.rows(response)
        self.assertEqual(rows[0], ["sent_at", "sender", "message"])
        self.assertEqual([r[2] for r in rows[1:]][0], "Hello Mrs Adeyemi")
        self.assertEqual(rows[-1][2], "A plain line, with a comma")
        self.assertEqual(len(rows), 5)

    def test_dates_are_written_in_the_schools_own_format(self):
        rows = self.rows(self.export(self.lekki_head, self.dm))
        first = self.dm.messages.order_by("id").first()
        self.assertEqual(rows[1][0], format_datetime(first.created_at, self.tenant))

    def test_a_cell_that_would_run_as_a_formula_is_written_as_text(self):
        rows = self.rows(self.export(self.lekki_head, self.dm))
        cells = {r[2] for r in rows[1:]}
        self.assertIn("'=HYPERLINK(\"http://evil.example\",\"click\")", cells)
        self.assertIn("'+1 for that, -2 for the other, @team", cells)
        self.assertNotIn("=HYPERLINK(\"http://evil.example\",\"click\")", cells)

    def test_a_name_that_would_run_as_a_formula_is_written_as_text(self):
        self.funke.first_name = "=cmd"
        self.funke.save(update_fields=["first_name"])
        rows = self.rows(self.export(self.lekki_head, self.dm))
        self.assertTrue(any(r[1].startswith("'=cmd") for r in rows[1:]))

    def test_the_export_is_audited_without_the_text(self):
        self.export(self.lekki_head, self.dm)
        event = audit_events("CONVERSATION_EXPORTED").get()
        self.assertEqual(event.entity_id, str(self.dm.pk))
        self.assertEqual(event.actor_user_id, self.lekki_head.pk)
        self.assertEqual(event.metadata["message_count"], 4)
        self.assertEqual(event.metadata["kind"], "STAFF_DIRECT")
        self.assertNotIn("Hello Mrs Adeyemi", event.summary + str(event.metadata))
        self.assertNotIn("HYPERLINK", event.summary + str(event.metadata))

    def test_a_refused_export_leaves_no_event(self):
        self.export(self.nobody, self.dm)
        self.export(self.admin, self.dm)
        self.assertEqual(audit_events("CONVERSATION_EXPORTED").count(), 0)


class AccessTests(ExportFixture):
    def test_a_caller_without_the_key_is_refused(self):
        response = self.export(self.nobody, self.dm)
        self.assertEqual(response.status_code, 403)
        # Taking part in the conversation is not enough without the key.
        self.assertEqual(self.export(self.reader, self.dm).status_code, 403)

    def test_a_holder_of_the_key_who_is_not_in_the_conversation_gets_the_404(self):
        response = self.export(self.admin, self.dm)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, self.export(self.admin, pk=MISSING).data)

    def test_another_schools_conversation_answers_like_one_that_does_not_exist(self):
        from schools.vs_messages.services import conversations

        elsewhere, _, _ = conversations.start_staff_direct(
            self.solo.tenant, self.ada, recipient_id=self.solo_admin.pk, body="Hello",
        )
        there = self.export(self.lekki_head, pk=elsewhere.pk)
        self.assertEqual(there.status_code, 404)
        self.assertEqual(there.data, self.export(self.lekki_head, pk=MISSING).data)

    def test_a_pupil_or_a_parent_has_no_export(self):
        family = self.family_chat(self.funke, self.tunde, self.mother, "Hello")
        for user in (self.mother.user, self.tunde.user):
            with self.subTest(user=user.email):
                self.assertEqual(self.export(user, family).status_code, 403)

    def test_a_member_of_staff_in_a_family_conversation_downloads_it(self):
        family = self.family_chat(self.funke, self.tunde, self.mother, "Tunde did well")
        rows = self.rows(self.export(self.funke, family))
        self.assertEqual([r[2] for r in rows[1:]], ["Tunde did well"])
        self.assertEqual(rows[1][1], "Funke Adeyemi")

    def test_the_join_point_is_respected(self):
        team = self.group(self.lekki_head, [self.funke], name="Staff room", body="Before you joined")
        groups.add_members(
            groups.group_access(self.tenant, self.lekki_head, team.pk), self.lekki_head, [self.ibrahim.pk],
        )
        self.say(team, self.lekki_head, "After you joined")
        late = [r[2] for r in self.rows(self.export(self.ibrahim, team))[1:]]
        self.assertEqual(late, ["After you joined"])
        owner = [r[2] for r in self.rows(self.export(self.lekki_head, team))[1:]]
        self.assertEqual(owner, ["Before you joined", "After you joined"])

    def test_a_removed_member_can_no_longer_download(self):
        team = self.group(self.lekki_head, [self.funke], name="Staff room", body="Hello")
        groups.remove_member(
            groups.group_access(self.tenant, self.lekki_head, team.pk), self.lekki_head, self.funke.pk,
        )
        self.assertEqual(self.export(self.funke, team).status_code, 404)


class FieldAccessTests(ExportFixture):
    def header(self, user=None):
        return self.rows(self.export(user or self.lekki_head, self.dm))[0]

    def test_every_column_is_present_by_default(self):
        self.assertEqual(self.header(), ["sent_at", "sender", "message"])

    def test_a_column_follows_the_switch_of_what_it_carries(self):
        self.hide("participants", self.role)
        self.assertEqual(self.header(), ["sent_at", "message"])

    def test_a_hidden_message_leaves_the_text_column_out(self):
        self.hide("body", self.role)
        rows = self.rows(self.export(self.lekki_head, self.dm))
        self.assertEqual(rows[0], ["sent_at", "sender"])
        self.assertNotIn("Hello Mrs Adeyemi", self.export(self.lekki_head, self.dm).content.decode())

    def test_hidden_sent_times_leave_the_date_column_out(self):
        self.hide("sent_at", self.role)
        self.assertEqual(self.header(), ["sender", "message"])

    def test_a_caller_who_may_read_none_of_it_has_nothing_to_download(self):
        for field in ("sent_at", "participants", "body"):
            self.hide(field, self.role)
        response = self.export(self.lekki_head, self.dm)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_read_denied")
        self.assertEqual(audit_events("CONVERSATION_EXPORTED").count(), 0)
