"""No message field reaches a caller who may read none, at any depth.

Every surface of ``school.messages`` is rendered on a real record as a caller
whose role has every field switched off, and the whole payload is walked. The
rows are rendered as the participant they belong to, so the data is real; only
the Field Access of the request is closed.
"""
from __future__ import annotations

import datetime as dt
import uuid

from schools.vs_messages.models import Message
from schools.vs_messages.services import config, inbox, oversight, visibility
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .base import MessagesFixture

_S = "schools.vs_messages.serializers."


class MessagesDeepPayloadTests(DeepPayloadChecks, MessagesFixture):
    """Renders every surface as a caller who may read nothing."""
    resources = frozenset({"school.messages"})
    covers = frozenset({
        _S + "ConversationRowSerializer",
        _S + "ConversationDetailSerializer",
        _S + "LastMessageSerializer",
        _S + "MessageSerializer",
        _S + "OversightRowSerializer",
        _S + "OversightDetailSerializer",
        _S + "StartConversationSerializer",
        _S + "SendMessageSerializer",
        _S + "GroupCreateSerializer",
        _S + "BroadcastCreateSerializer",
    })
    #: A branch is not a field and has no switch; its name is only named like a group's.
    allowlist = {
        (_S + "ConversationRowSerializer", "branch.name"):
            "The branch's name; a branch carries no Field Access switch.",
        (_S + "ConversationDetailSerializer", "branch.name"):
            "The branch's name; a branch carries no Field Access switch.",
        (_S + "OversightRowSerializer", "branch.name"):
            "The branch's name; a branch carries no Field Access switch.",
        (_S + "OversightDetailSerializer", "branch.name"):
            "The branch's name; a branch carries no Field Access switch.",
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.team = cls.group(cls.funke, [cls.ibrahim, cls.ngozi], name="Maths team", body="Welcome")
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Your son did well")
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim], title="Sports day")
        cls.message = Message.objects.filter(conversation=cls.team).first()

    def row(self, conversation, user):
        return inbox.prepare(
            visibility.staff_conversations(self.tenant, user).filter(pk=conversation.pk), user,
        ).get()

    def context(self, user):
        return {
            "user": user, "keys": visibility.caller_keys(self.tenant, user),
            "settings": config.load_settings(self.tenant), "multi_branch": True,
        }

    def oversight_row(self, conversation, surface):
        return oversight.listing(self.tenant, self.admin, surface).get(pk=conversation.pk)

    def samples(self):
        funke = self.context(self.funke)
        sender = self.context(self.lekki_head)
        oversight_context = {"user": self.admin, "multi_branch": True}
        return [
            Sample(_S + "ConversationRowSerializer", self.row(self.team, self.funke), context=funke),
            Sample(_S + "ConversationDetailSerializer", self.row(self.notice, self.lekki_head), context=sender),
            Sample(_S + "LastMessageSerializer", {
                "id": 1, "preview": "Welcome", "sender_name": "Funke Adeyemi",
                "sent_at": dt.datetime(2026, 10, 21, 9, 0), "mine": True,
            }),
            Sample(_S + "MessageSerializer", self.message, context={"user": self.funke}),
            Sample(_S + "OversightRowSerializer",
                   self.oversight_row(self.team, "inspect_group"), context=oversight_context),
            Sample(_S + "OversightDetailSerializer",
                   self.oversight_row(self.team, "inspect_group"), context=oversight_context),
            Sample(_S + "StartConversationSerializer", {
                "kind": "FAMILY", "recipient": self.ibrahim.pk, "student": self.tunde.pk,
                "guardian": self.mother.pk, "body": "Hello", "client_token": str(uuid.uuid4()),
            }),
            Sample(_S + "SendMessageSerializer", {"body": "Hello", "client_token": str(uuid.uuid4())}),
            Sample(_S + "GroupCreateSerializer", {
                "name": "Maths team", "branch": self.lekki.pk, "members": [self.ibrahim.pk],
                "body": "Welcome", "client_token": str(uuid.uuid4()),
            }),
            Sample(_S + "BroadcastCreateSerializer", {
                "title": "Sports day", "body": "Friday", "reply_mode": "ONE_WAY",
                "branch": self.lekki.pk, "recipients": [self.funke.pk],
                "client_token": str(uuid.uuid4()),
            }),
        ]

    def test_a_family_row_is_also_clean(self):
        """The child block and the guardian's label are the likeliest leaks; render them too."""
        from vs_rbac.tests.deep_payload import closed_request, walk, registered_names

        request, _ = closed_request(self.tenant, "school.messages")
        from schools.vs_messages.serializers import ConversationDetailSerializer

        payload = ConversationDetailSerializer(
            self.row(self.family, self.funke), context={**self.context(self.funke), "request": request},
        ).data
        names = registered_names("school.messages")
        leaks = [path for path, key, _ in walk(payload) if key in names and path != "branch.name"]
        self.assertEqual(leaks, [])
        self.assertNotIn("Tunde", str(payload))
        self.assertNotIn("Bello", str(payload))
        self.assertNotIn("Your son", str(payload))
