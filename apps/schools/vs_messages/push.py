"""Where a tap on a message push opens.

Registered with the push layer
(:func:`vs_notifications.services.push.register_destination`) when the app
loads. It recognises the two pushes a family can receive, and passes on
everything else:

- a message to a parent opens that conversation in the parent app
  (``/parent/messages/<id>``);
- a message to a pupil opens it in the student app (``/student/messages/<id>``).

A staff push keeps the push layer's default, the recipient's own app. A push
that names no conversation opens the app's Messages screen.
"""
from __future__ import annotations

from vs_notifications.services.push import PushDestination

from .constants import EVENT_NEW_MESSAGE_PARENT, EVENT_NEW_MESSAGE_PUPIL


def destination(notification):
    """A :class:`PushDestination` for a family push about a conversation, or None to pass."""
    from schools.vs_notices.constants import PARENT_APP_PATH
    from schools.vs_students.constants import STUDENT_APP_PATH

    key = notification.event_type.key
    if key == EVENT_NEW_MESSAGE_PARENT:
        root = PARENT_APP_PATH
    elif key == EVENT_NEW_MESSAGE_PUPIL:
        root = STUDENT_APP_PATH
    else:
        return None
    conversation_id = (notification.metadata or {}).get("conversation_id")
    page = f"messages/{conversation_id}" if conversation_id else "messages"
    return PushDestination(url=f"{root}{page}")
