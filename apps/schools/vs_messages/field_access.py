"""The message fields an administrator may restrict per role.

One resource, ``school.messages``. Every field is included and open by default.

* **Message** (``body``) is the text itself, wherever it travels: a message, an
  inbox preview, and the words searched.
* **Group or broadcast name** (``title``) is what a group or broadcast is
  called. It governs ``display_name`` only where that is a title; where
  ``display_name`` is a person's name it follows People in the conversation,
  so the serializers decide that one name in code rather than registering it.
* **People in the conversation** (``participants``) is every place a person is
  named: the other end of a conversation, the members of a group, the sender of
  a message, and the name matched by a search.
* **Broadcast recipients** (``recipients``) is who a broadcast went to and how
  many.
* **Pupil the conversation is about** (``child``) is the child of a family or
  pupil conversation, wherever the child is named or searched.
* **Broadcast reply setting** (``reply_mode``) is whether replies are allowed.
* **Sent times**, **read status** and **read receipts** are recorded by the
  system and read-only. Read receipts (``receipt`` on a message, ``receipts``
  on a conversation and on a polling answer) are the ticks a sender sees on
  their own messages; they are separate from read status, which counts what the
  caller has not read themselves. The counts inside a receipt travel with it,
  so no name of their own is registered: ``recipient_count`` already belongs to
  Broadcast recipients, and a polling answer, which is built by hand, leaves its
  ``receipts`` list out whole when the caller may not read them.

``title`` and ``participants`` are open on create: whoever may make a group
names it and chooses its first members, and the Write switch decides who may
rename it or change its members afterwards. ``body``, ``recipients``, ``child``
and ``reply_mode`` are not: Write off refuses sending a message, addressing a
broadcast, naming a child on a new family or pupil conversation and choosing
the reply setting.

A conversation's kind, the branch, ids, the ``mine`` and ``can_send`` flags and
the member count carry no switch: they are identifiers and counts of children,
and a school's branches are not a Field Access matter.

A parent or pupil using the portal holds no role, so these switches do not
reach them; their payloads are a fixed narrow shape.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_S = "schools.vs_messages.serializers."


def register():
    """Publish the message field declarations to the Field Access registry."""
    register_fields(
        "school",
        "messages",
        surfaces=(
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
        ),
        fields=(
            FieldSpec("body", "Message", group="Messages", scope=_TENANT,
                      sort_order=10, api_names=("body", "preview"),
                      description="The text of a message, in a conversation, "
                                  "an inbox preview and a search."),
            FieldSpec("title", "Group or broadcast name", group="Messages", scope=_TENANT,
                      sort_order=20, open_on_create=True, api_names=("title", "name"),
                      description="What a group or a broadcast is called."),
            FieldSpec("participants", "People in the conversation", group="People",
                      scope=_TENANT, sort_order=10, open_on_create=True,
                      api_names=("counterpart", "participants", "sender", "sender_name",
                                 "members", "recipient", "guardian"),
                      description="Who is in a conversation, who sent a message, "
                                  "and the names a search matches."),
            FieldSpec("recipients", "Broadcast recipients", group="People", scope=_TENANT,
                      sort_order=20, api_names=("recipients", "recipient_count"),
                      description="Who a broadcast went to, and how many."),
            FieldSpec("child", "Pupil the conversation is about", group="People",
                      scope=_TENANT, sort_order=30, api_names=("child", "student"),
                      description="The child of a parent or pupil conversation."),
            FieldSpec("reply_mode", "Broadcast reply setting", group="Messages", scope=_TENANT,
                      sort_order=30, api_names=("reply_mode",),
                      description="Whether a broadcast takes private replies."),
            FieldSpec("sent_at", "Sent times", group="Status", scope=_TENANT,
                      writable=False, sort_order=10, api_names=("sent_at", "last_activity_at"),
                      description="When messages were sent and a conversation was last active."),
            FieldSpec("read_status", "Read status and unread counts", group="Status",
                      scope=_TENANT, writable=False, sort_order=20, api_names=("unread_count",),
                      description="How many messages are unread, per conversation and in total."),
            FieldSpec("read_receipts", "Read receipts", group="Status",
                      scope=_TENANT, writable=False, sort_order=30,
                      api_names=("receipt", "receipts"),
                      description="Whether the people a message was sent to have read it, "
                                  "shown to the sender on their own messages."),
        ),
    )
