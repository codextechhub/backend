"""Refusals the messaging module raises.

Each carries a code the client branches on, a sentence written for the person
who pressed the button, and the HTTP status, and the shared exception handler
renders them as they are.

Two refusals say nothing about what exists. :class:`ConversationNotFound`
answers a conversation at another school, one that does not exist and one the
caller may not reach with the same status, code and sentence, and
:class:`RecipientUnavailable` answers a person who is out of reach and a
person who is not there at all in the same words.
"""


class MessagesError(Exception):
    """Base of every refusal this module raises."""
    error_code = "MESSAGES_ERROR"
    default_message = "That could not be done to this conversation."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class ConversationNotFound(MessagesError):
    """The one answer for a conversation that is missing, at another school or out of reach."""
    error_code = "CONVERSATION_NOT_FOUND"
    default_message = "There is no such conversation."
    http_status = 404

    def __init__(self):
        super().__init__(self.default_message)


class ConversationReadOnly(MessagesError):
    """The caller may read the conversation but not write in it."""
    error_code = "CONVERSATION_READ_ONLY"
    default_message = "You can read this conversation but not write in it."
    http_status = 409


class BroadcastOneWay(MessagesError):
    """A private reply was sent to a broadcast that takes none."""
    error_code = "BROADCAST_ONE_WAY"
    default_message = "This broadcast does not take replies."
    http_status = 409


class GroupNeedsOwner(MessagesError):
    """The last owner tried to leave a group that still has members."""
    error_code = "GROUP_NEEDS_OWNER"
    default_message = (
        "A group needs an owner while it has other members. Make someone else "
        "an owner first."
    )
    http_status = 409


class NotGroupOwner(MessagesError):
    """A group action reserved to its owners was attempted by a member."""
    error_code = "GROUP_OWNER_ONLY"
    default_message = "Only an owner of this group can do that."
    http_status = 403


class RecipientUnavailable(MessagesError):
    """The person cannot be messaged: out of reach, not there, or without an account."""
    error_code = "MESSAGE_RECIPIENT_UNAVAILABLE"
    default_message = "That person cannot be messaged from here."
    http_status = 400


class BodyRequired(MessagesError):
    """A message with no text."""
    error_code = "MESSAGE_BODY_REQUIRED"
    default_message = "Write a message before sending it."


class BodyTooLong(MessagesError):
    """A message longer than the limit."""
    error_code = "MESSAGE_BODY_TOO_LONG"
    default_message = "That message is too long."


class TitleInvalid(MessagesError):
    """A group name or broadcast title that is empty or too long."""
    error_code = "MESSAGE_TITLE_INVALID"
    default_message = "Give it a name."


class RecipientsInvalid(MessagesError):
    """A list of people that is empty, malformed or over the limit."""
    error_code = "MESSAGE_RECIPIENTS_INVALID"
    default_message = "Choose who it is for."


class BranchOutOfReach(MessagesError):
    """A group or broadcast aimed at a branch the caller does not reach."""
    error_code = "MESSAGE_BRANCH_OUT_OF_REACH"
    default_message = "You cannot use that branch."
    http_status = 403


class NotAMember(MessagesError):
    """The person named is not a current member of the group."""
    error_code = "GROUP_MEMBER_NOT_FOUND"
    default_message = "That person is not in this group."
    http_status = 404


class MessageRateLimited(MessagesError):
    """The caller has used the minute's send budget; see ``throttles``."""
    error_code = "MESSAGE_RATE_LIMITED"
    default_message = "You are sending messages too quickly. Wait a moment and try again."
    http_status = 429
