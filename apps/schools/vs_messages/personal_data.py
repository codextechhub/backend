"""Message sections of a personal data copy (:mod:`core.personal_data`).

A pupil's copy holds the conversations about them; a guardian's, the
conversations with them. Each message keeps its sender's name, its words and
when it was sent.
"""
from __future__ import annotations

from core.personal_data import GUARDIAN, PUPIL, register


def _conversations(link):
    def build(person):
        from .models import Conversation, Message

        out = []
        for conversation in Conversation._base_manager.filter(**{link: person}).order_by("pk"):
            messages = Message._base_manager.filter(conversation=conversation).select_related(
                "sender").order_by("created_at", "pk")
            out.append({
                "Conversation": conversation.title or conversation.get_kind_display(),
                "Messages": [
                    {"From": str(m.sender) if m.sender_id else "", "Sent": m.created_at.isoformat(),
                     "Message": m.body}
                    for m in messages
                ],
            })
        return out

    return build


def register_sections() -> None:
    register(PUPIL, "messages", "Messages", _conversations("student"),
             covers=("vs_messages.Conversation",))
    register(GUARDIAN, "messages", "Messages", _conversations("guardian"),
             covers=("vs_messages.Conversation",))
