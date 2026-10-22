"""The fixed, narrow shapes the parent and student apps are given.

A parent or a pupil holds no role, so Field Access switches do not reach them,
and these serializers deliberately do not carry the Field Access mixin: a
teacher who is also a parent signs in to the parent app with the same account,
and a switch her staff role has closed must not blank her own child's
conversation there. What the portals may see is decided by what these shapes
contain, and they contain only what a conversation needs to be read and
answered:

* the other end as a name and a label (a family is shown "Class teacher,
  JSS1 B", never a staff member's contact details or job record);
* the child, for a parent;
* the last message, the unread count, and whether the family may write.

They never carry a group, a branch, a member count, a role, a reply setting or a
guardian's contact details.
"""
from __future__ import annotations

from rest_framework import serializers

from .constants import PREVIEW_MAX, SURFACE_PARENT, SURFACE_STUDENT, ParticipantRole
from .services import inbox, visibility


class PortalConversationSerializer(serializers.Serializer):
    """One conversation in a parent's or a pupil's list, prepared by :func:`services.inbox.prepare`.

    The context carries ``user``, ``surface``, ``settings`` and ``labels``, a
    dictionary of ``{student id: {staff user id: label}}`` read once for the
    page. A conversation the primary guardian is only watching has
    ``read_only`` set, no unread count and the reason in words.
    """

    id = serializers.IntegerField(read_only=True)
    kind = serializers.CharField(read_only=True)
    kind_label = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    counterpart = serializers.SerializerMethodField()
    child = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    last_activity_at = serializers.DateTimeField(source="activity", read_only=True)
    unread_count = serializers.SerializerMethodField()
    read_only = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()
    read_only_reason = serializers.SerializerMethodField()

    def _surface(self):
        return self.context["surface"]

    def _state(self, obj):
        cache = self.context.setdefault("_send_state", {})
        if obj.pk not in cache:
            watching = bool(getattr(obj, "watching", False))
            role = (
                ParticipantRole.GUARDIAN if self._surface() == SURFACE_PARENT
                else ParticipantRole.PUPIL
            )
            cache[obj.pk] = visibility.send_state(
                obj, surface=self._surface(), role=role,
                staff_ok=getattr(obj, "staff_ok", True), watching=watching,
                settings=self.context.get("settings"),
                student_name=obj.student.first_name if obj.student_id else "",
            )
        return cache[obj.pk]

    def _counterpart(self, obj):
        labels = (self.context.get("labels") or {}).get(obj.student_id)
        return inbox.counterpart(
            obj, self.context["user"], surface=self._surface(), labels=labels, with_child=False,
        )

    def get_kind_label(self, obj):
        return obj.get_kind_display()

    def get_display_name(self, obj):
        other = self._counterpart(obj)
        return other["name"] if other else ""

    def get_counterpart(self, obj):
        return self._counterpart(obj)

    def get_child(self, obj):
        if self._surface() != SURFACE_PARENT:
            return None
        return {
            "id": obj.student_id,
            "name": obj.student.full_name,
            "class_name": getattr(obj, "child_class_name", None) or "",
        }

    def get_last_message(self, obj):
        message = obj.last_message
        if message is None or message.pk <= getattr(obj, "my_after", 0):
            return None
        sender = message.sender
        return {
            "id": message.pk,
            "preview": message.body[:PREVIEW_MAX],
            "sender_name": inbox.person_name(sender),
            "sent_at": serializers.DateTimeField().to_representation(message.created_at),
            "mine": sender is not None and sender.pk == self.context["user"].pk,
        }

    def get_unread_count(self, obj):
        return 0 if getattr(obj, "watching", False) else obj.unread

    def get_read_only(self, obj):
        return not self._state(obj)[0]

    def get_can_send(self, obj):
        return self._state(obj)[0]

    def get_read_only_reason(self, obj):
        return self._state(obj)[1]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self._surface() == SURFACE_STUDENT:
            data.pop("child", None)
        return data


class PortalConversationDetailSerializer(PortalConversationSerializer):
    """One conversation opened: the row, and whether the app draws read ticks.

    ``receipts`` is true while the school shows receipts and the caller takes
    part in the conversation; a parent only watching a pupil conversation is
    shown none.
    """

    receipts = serializers.SerializerMethodField()

    def get_receipts(self, obj):
        if getattr(obj, "watching", False):
            return False
        return bool(getattr(self.context.get("settings"), "read_receipts", True))


class PortalMessageSerializer(serializers.Serializer):
    """One message, as a parent or a pupil reads it.

    The context may carry ``receipts``, a dictionary of ``{message id: receipt}``
    computed once for the page; a message with no entry has no ``receipt`` key.
    """

    id = serializers.IntegerField(read_only=True)
    body = serializers.CharField(read_only=True)
    sender = serializers.SerializerMethodField()
    sent_at = serializers.DateTimeField(source="created_at", read_only=True)
    mine = serializers.SerializerMethodField()
    receipt = serializers.SerializerMethodField()

    def get_sender(self, obj):
        if obj.sender is None:
            return None
        return {"user_id": obj.sender_id, "name": inbox.person_name(obj.sender)}

    def get_mine(self, obj):
        return obj.sender_id == self.context["user"].pk

    def get_receipt(self, obj):
        return (self.context.get("receipts") or {}).get(obj.pk)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("receipt") is None:
            data.pop("receipt", None)
        return data


class PortalSendSerializer(serializers.Serializer):
    """The text of a message, with the token that makes a retry safe."""

    body = serializers.CharField(allow_blank=True, trim_whitespace=False)
    client_token = serializers.UUIDField(required=False, allow_null=True)


class ParentStartSerializer(PortalSendSerializer):
    """A parent starts a conversation: which child it is about and which member of staff."""

    child = serializers.IntegerField()
    staff = serializers.IntegerField()
    body = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)


class StudentStartSerializer(PortalSendSerializer):
    """A pupil starts a conversation with one member of staff."""

    staff = serializers.IntegerField()
    body = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

