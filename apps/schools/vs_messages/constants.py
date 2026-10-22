"""Words, keys and limits the messaging module shares."""
from django.db import models

#: The staff inbox: conversations the holder takes part in.
PERM_VIEW = "school.messages.view"
#: Start a staff direct conversation, write in conversations the holder takes
#: part in, and reply privately to a broadcast.
PERM_SEND = "school.messages.send"
#: Download a conversation the holder takes part in.
PERM_EXPORT = "school.messages.export"
#: The staff end of family and pupil conversations.
PERM_FAMILY_SEND = "school.family_messages.send"
#: Be the "school office" contact for families at the branches in reach.
PERM_FAMILY_OFFICE = "school.family_messages.office"
#: Create a staff group; the creator becomes its owner.
PERM_GROUP_CREATE = "school.message_groups.create"
#: Send a broadcast to staff.
PERM_BROADCAST_SEND = "school.message_broadcasts.send"
#: Read any group in reach, read-only, with every opening recorded.
PERM_GROUP_INSPECT = "school.message_groups.inspect"
#: Read any broadcast in reach, read-only, never its private replies.
PERM_BROADCAST_INSPECT = "school.message_broadcasts.inspect"
#: Read any pupil conversation in reach, read-only, with every opening recorded.
PERM_WELFARE_INSPECT = "school.message_welfare.inspect"

#: School settings (whole tenant, BOOLEAN).
CFG_FAMILIES_ENABLED = "messages.families_enabled"
CFG_PUPILS_ENABLED = "messages.pupils_enabled"
CFG_SUBJECT_TEACHERS = "messages.family_reach_subject_teachers"
CFG_READ_RECEIPTS = "messages.read_receipts"

#: The Field Access resource every payload of this module answers to.
FIELD_RESOURCE = "school.messages"

#: Notification events. None of them ever carries the text of a message: each
#: says only that something was said, by whom and where.
EVENT_NEW_MESSAGE = "messages.new_message"
EVENT_NEW_BROADCAST = "messages.new_broadcast"
EVENT_NEW_MESSAGE_PARENT = "messages.new_message_parent"
EVENT_NEW_MESSAGE_PUPIL = "messages.new_message_pupil"

#: What a staff alert calls a sender or a conversation the recipient may not
#: be told the name of.
UNNAMED_COLLEAGUE = "a colleague"
UNNAMED_GROUP = "a staff group"
UNNAMED_BROADCAST = "a broadcast"
UNNAMED_PARENT = "a parent"
UNNAMED_PUPIL = "a pupil"

#: Said to a pupil beside their conversations: who else can read them.
OVERSIGHT_NOTICE = "Your parent or guardian and the school can read your messages."

BODY_MAX = 4000
TITLE_MAX = 150
PREVIEW_MAX = 120
#: People one broadcast or one new group may address.
RECIPIENTS_MAX = 1000
MESSAGE_PAGE_DEFAULT = 50
MESSAGE_PAGE_MAX = 100
#: Messages one polling call returns, and the latest messages whose receipts it refreshes.
POLL_LIMIT = 100


class ConversationKind(models.TextChoices):
    """The six shapes a conversation takes; each has a fixed set of columns."""
    STAFF_DIRECT = "STAFF_DIRECT", "Staff"
    FAMILY = "FAMILY", "Parent"
    PUPIL = "PUPIL", "Pupil"
    GROUP = "GROUP", "Group"
    BROADCAST = "BROADCAST", "Broadcast"
    BROADCAST_REPLY = "BROADCAST_REPLY", "Broadcast reply"


class ParticipantRole(models.TextChoices):
    """How a person stands in a conversation, for the staff and the portals alike."""
    MEMBER = "MEMBER", "Member"
    OWNER = "OWNER", "Owner"
    SENDER = "SENDER", "Sender"
    RECIPIENT = "RECIPIENT", "Recipient"
    STAFF = "STAFF", "Staff"
    GUARDIAN = "GUARDIAN", "Guardian"
    PUPIL = "PUPIL", "Pupil"


class ReplyMode(models.TextChoices):
    """Whether a broadcast takes private replies, fixed when it is sent."""
    ONE_WAY = "ONE_WAY", "One way"
    PRIVATE_REPLIES = "PRIVATE_REPLIES", "Replies allowed"


#: Roles that belong to the staff surface. A guardian or pupil participation
#: is read through the portal surfaces only.
STAFF_ROLES = frozenset({
    ParticipantRole.MEMBER, ParticipantRole.OWNER, ParticipantRole.SENDER,
    ParticipantRole.RECIPIENT, ParticipantRole.STAFF,
})

#: Kinds whose two ends are fixed when the first message is sent. A listing
#: shows one to its other end only once something has been said in it.
PAIR_KINDS = frozenset({
    ConversationKind.STAFF_DIRECT, ConversationKind.FAMILY,
    ConversationKind.PUPIL, ConversationKind.BROADCAST_REPLY,
})

#: The ``?kind=`` filter of the staff inbox.
KIND_FILTERS = {
    "direct": (ConversationKind.STAFF_DIRECT,),
    "family": (ConversationKind.FAMILY, ConversationKind.PUPIL),
    "group": (ConversationKind.GROUP,),
    "broadcast": (ConversationKind.BROADCAST, ConversationKind.BROADCAST_REPLY),
}

# The surfaces a conversation is resolved on.
SURFACE_STAFF = "staff"
SURFACE_PARENT = "parent"
SURFACE_STUDENT = "student"
SURFACE_INSPECT_GROUP = "inspect_group"
SURFACE_INSPECT_BROADCAST = "inspect_broadcast"
SURFACE_INSPECT_WELFARE = "inspect_welfare"

INSPECT_SURFACES = {
    SURFACE_INSPECT_GROUP: ConversationKind.GROUP,
    SURFACE_INSPECT_BROADCAST: ConversationKind.BROADCAST,
    SURFACE_INSPECT_WELFARE: ConversationKind.PUPIL,
}
