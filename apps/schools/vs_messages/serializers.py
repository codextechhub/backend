"""Shapes for the staff messaging screens, and the bodies they send.

Every serializer here is a surface of the ``school.messages`` Field Access
resource (see ``field_access.py``). Three things keep a hidden field out of a
payload completely:

* a block that is one field (``counterpart``, ``child``, ``participants``) is a
  key of the row, so the mixin removes it whole;
* the last message of a row is its own serializer, so its ``preview``,
  ``sender_name`` and ``sent_at`` follow their own switches;
* ``display_name`` is the group's title or a person's name depending on the
  kind, so it is decided in :meth:`ConversationRowSerializer.to_representation`
  against the switch that governs what it is showing.

Nested name blocks are plain dictionaries: their ``name`` key must not be read
as the group-title field of the same name.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin, can_read

from .constants import (
    FIELD_RESOURCE,
    PREVIEW_MAX,
    ConversationKind,
    ParticipantRole,
    ReplyMode,
)
from .models import Participant
from .services import inbox, visibility

_KEY = FIELD_RESOURCE + "."


def _may(request, name) -> bool:
    return can_read(request, _KEY + name)


def preview_of(body: str) -> str:
    """The first characters of a message, for an inbox row."""
    return body[:PREVIEW_MAX]


class LastMessageSerializer(FieldAccessMixin, serializers.Serializer):
    """The newest message a caller may read, as an inbox row shows it."""

    field_resource = FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    preview = serializers.CharField(read_only=True)
    sender_name = serializers.CharField(read_only=True)
    sent_at = serializers.DateTimeField(read_only=True)
    mine = serializers.BooleanField(read_only=True)


class ConversationRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One conversation in an inbox.

    Reads a conversation prepared by :func:`services.inbox.prepare`. The
    context carries ``request``, ``user``, ``keys`` (the caller's send keys),
    ``settings`` (the school's messaging switches), ``multi_branch`` and,
    optionally, ``surface`` and ``labels`` for a portal.
    """

    field_resource = FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    kind = serializers.CharField(read_only=True)
    kind_label = serializers.SerializerMethodField()
    title = serializers.CharField(read_only=True)
    display_name = serializers.SerializerMethodField()
    counterpart = serializers.SerializerMethodField()
    child = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    last_activity_at = serializers.DateTimeField(source="activity", read_only=True)
    unread_count = serializers.IntegerField(source="unread", read_only=True)
    reply_mode = serializers.CharField(read_only=True)
    read_only = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()
    read_only_reason = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()

    # ── context ──

    def _user(self):
        return self.context["user"]

    def _surface(self):
        return self.context.get("surface", "staff")

    def _state(self, obj):
        """``(can_send, reason)``, worked out once per conversation per render."""
        cache = self.context.setdefault("_send_state", {})
        if obj.pk not in cache:
            cache[obj.pk] = visibility.send_state(
                obj, surface=self._surface(), role=getattr(obj, "my_role", None),
                staff_ok=getattr(obj, "staff_ok", True),
                family_ok=getattr(obj, "family_ok", True),
                watching=bool(getattr(obj, "watching", False)),
                keys=self.context.get("keys"), settings=self.context.get("settings"),
                student_name=obj.student.first_name if obj.student_id else "",
            )
        return cache[obj.pk]

    # ── fields ──

    def get_kind_label(self, obj):
        return obj.get_kind_display()

    def _display_name(self, obj):
        if obj.kind in (ConversationKind.GROUP, ConversationKind.BROADCAST):
            return obj.title
        other = inbox.counterpart(
            obj, self._user(), surface=self._surface(), labels=self.context.get("labels"),
            with_child=False,
        )
        return other["name"] if other else ""

    def get_display_name(self, obj):
        return self._display_name(obj)

    def get_counterpart(self, obj):
        return inbox.counterpart(
            obj, self._user(), surface=self._surface(), labels=self.context.get("labels"),
            with_child=_may(self.context.get("request"), "child"),
        )

    def get_child(self, obj):
        if obj.kind not in (ConversationKind.FAMILY, ConversationKind.PUPIL):
            return None
        return {
            "id": obj.student_id,
            "name": obj.student.full_name,
            "class_name": getattr(obj, "child_class_name", None) or "",
        }

    def get_member_count(self, obj):
        if obj.kind == ConversationKind.BROADCAST:
            return None
        return getattr(obj, "member_total", 0)

    def get_last_message(self, obj):
        message = obj.last_message
        if message is None or message.pk <= getattr(obj, "my_after", 0):
            return None
        sender = message.sender
        return LastMessageSerializer(
            {
                "id": message.pk,
                "preview": preview_of(message.body),
                "sender_name": inbox.person_name(sender),
                "sent_at": message.created_at,
                "mine": sender is not None and sender.pk == self._user().pk,
            },
            context={"request": self.context.get("request")},
        ).data

    def get_read_only(self, obj):
        return not self._state(obj)[0]

    def get_can_send(self, obj):
        return self._state(obj)[0]

    def get_read_only_reason(self, obj):
        return self._state(obj)[1]

    def get_role(self, obj):
        return getattr(obj, "my_role", None) or None

    def get_branch(self, obj):
        if not self.context.get("multi_branch"):
            return None
        if obj.kind not in (ConversationKind.GROUP, ConversationKind.BROADCAST) or obj.branch is None:
            return None
        return {"id": obj.branch_id, "name": obj.branch.name}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if "display_name" in data:
            names_a_title = instance.kind in (ConversationKind.GROUP, ConversationKind.BROADCAST)
            if not _may(request, "title" if names_a_title else "participants"):
                data.pop("display_name")
        if not self.context.get("multi_branch") or instance.kind not in (
            ConversationKind.GROUP, ConversationKind.BROADCAST,
        ):
            data.pop("branch", None)
        return data


def _people(conversation, *, roles=None):
    """Current participants of *conversation* as the people blocks a screen lists."""
    rows = (
        Participant.objects.filter(conversation=conversation, left_at__isnull=True)
        .select_related("user", "user__staff_profile")
        .order_by("joined_at", "pk")
    )
    if roles is not None:
        rows = rows.filter(role__in=roles)
    out = []
    for row in rows:
        profile = getattr(row.user, "staff_profile", None)
        if row.role == ParticipantRole.GUARDIAN:
            label = "Parent"
        elif row.role == ParticipantRole.PUPIL:
            label = "Pupil"
        else:
            label = ((profile.job_title or "").strip() if profile else "") or "Staff"
        out.append({
            "user_id": row.user_id, "name": inbox.person_name(row.user),
            "label": label, "role": row.role,
        })
    return out


class ConversationDetailSerializer(ConversationRowSerializer):
    """One conversation opened: the row, who is in it, and who a broadcast went to.

    Everyone sees ``recipient_count`` of a broadcast; only its sender sees
    ``recipients``.
    """

    field_access_detail = True

    participants = serializers.SerializerMethodField()
    recipients = serializers.SerializerMethodField()
    recipient_count = serializers.SerializerMethodField()

    def get_participants(self, obj):
        if obj.kind == ConversationKind.BROADCAST:
            return None
        return _people(obj)

    def get_recipients(self, obj):
        if obj.kind != ConversationKind.BROADCAST or getattr(obj, "my_role", None) != ParticipantRole.SENDER:
            return None
        return _people(obj, roles=[ParticipantRole.RECIPIENT])

    def get_recipient_count(self, obj):
        if obj.kind != ConversationKind.BROADCAST:
            return None
        return max(getattr(obj, "member_total", 1) - 1, 0)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for name in ("participants", "recipients", "recipient_count"):
            if name in data and data[name] is None:
                data.pop(name)
        return data


class MessageSerializer(FieldAccessMixin, serializers.Serializer):
    """One message in a conversation."""

    field_resource = FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    body = serializers.CharField(read_only=True)
    sender = serializers.SerializerMethodField()
    sent_at = serializers.DateTimeField(source="created_at", read_only=True)
    mine = serializers.SerializerMethodField()

    def get_sender(self, obj):
        if obj.sender is None:
            return None
        return {"user_id": obj.sender_id, "name": inbox.person_name(obj.sender)}

    def get_mine(self, obj):
        user = self.context.get("user")
        return user is not None and obj.sender_id == user.pk


class OversightRowSerializer(FieldAccessMixin, serializers.Serializer):
    """A conversation in a leader's oversight list: no message text.

    ``participants`` names the people who run the conversation (a group's
    owners, a broadcast's sender, a pupil conversation's pupil and member of
    staff), which keeps a list of large groups cheap.
    """

    field_resource = FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    kind = serializers.CharField(read_only=True)
    kind_label = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    child = serializers.SerializerMethodField()
    participants = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    recipient_count = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()
    last_activity_at = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()

    def get_kind_label(self, obj):
        return obj.get_kind_display()

    def get_display_name(self, obj):
        if obj.kind == ConversationKind.PUPIL:
            return obj.student.full_name
        return obj.title

    def get_child(self, obj):
        if obj.kind != ConversationKind.PUPIL:
            return None
        return {
            "id": obj.student_id, "name": obj.student.full_name,
            "class_name": getattr(obj, "child_class_name", None) or "",
        }

    def get_participants(self, obj):
        leaders = {
            ConversationKind.GROUP: [ParticipantRole.OWNER],
            ConversationKind.BROADCAST: [ParticipantRole.SENDER],
        }.get(obj.kind, [ParticipantRole.PUPIL, ParticipantRole.STAFF])
        rows = getattr(obj, "leaders", None)
        if rows is None:
            rows = _leaders(obj, leaders)
        return [
            {"user_id": p.user_id, "name": inbox.person_name(p.user), "role": p.role}
            for p in rows
        ]

    def get_member_count(self, obj):
        return None if obj.kind == ConversationKind.BROADCAST else obj.member_total

    def get_recipient_count(self, obj):
        return max(obj.member_total - 1, 0) if obj.kind == ConversationKind.BROADCAST else None

    def get_branch(self, obj):
        if obj.kind == ConversationKind.PUPIL:
            return {"id": obj.student.branch_id, "name": obj.student.branch.name}
        return {"id": obj.branch_id, "name": obj.branch.name} if obj.branch_id else None

    def get_last_activity_at(self, obj):
        value = obj.last_message_at or obj.created_at
        return serializers.DateTimeField().to_representation(value)

    def get_message_count(self, obj):
        return obj.message_total

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "display_name" in data:
            names_a_title = instance.kind != ConversationKind.PUPIL
            if not _may(self.context.get("request"), "title" if names_a_title else "participants"):
                data.pop("display_name")
        for name in ("child", "recipient_count", "member_count"):
            if name in data and data[name] is None:
                data.pop(name)
        if not self.context.get("multi_branch"):
            data.pop("branch", None)
        return data


def _leaders(conversation, roles):
    return list(
        Participant.objects.filter(
            conversation=conversation, left_at__isnull=True, role__in=roles,
        ).select_related("user")
    )


class OversightDetailSerializer(OversightRowSerializer):
    """An inspected conversation: the row, its full member list where it has one, and the read-only flags."""

    field_access_detail = True

    members = serializers.SerializerMethodField()
    read_only = serializers.SerializerMethodField()
    inspection = serializers.SerializerMethodField()

    def get_members(self, obj):
        return None if obj.kind == ConversationKind.BROADCAST else _people(obj)

    def get_read_only(self, obj):
        return True

    def get_inspection(self, obj):
        return True

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("members") is None:
            data.pop("members", None)
        return data


# ── What a caller sends ───────────────────────────────────────────────────


class SendMessageSerializer(FieldAccessMixin, serializers.Serializer):
    """The text of a message, with the token that makes a retry safe."""

    field_resource = FIELD_RESOURCE

    body = serializers.CharField(allow_blank=True, trim_whitespace=False)
    client_token = serializers.UUIDField(required=False, allow_null=True)


class StartConversationSerializer(FieldAccessMixin, serializers.Serializer):
    """Start a staff, parent or pupil conversation, with an optional first message."""

    field_resource = FIELD_RESOURCE

    kind = serializers.ChoiceField(choices=[
        ConversationKind.STAFF_DIRECT, ConversationKind.FAMILY, ConversationKind.PUPIL,
    ])
    recipient = serializers.IntegerField(required=False)
    student = serializers.IntegerField(required=False)
    guardian = serializers.IntegerField(required=False)
    body = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    client_token = serializers.UUIDField(required=False, allow_null=True)


class GroupCreateSerializer(FieldAccessMixin, serializers.Serializer):
    """Make a staff group."""

    field_resource = FIELD_RESOURCE

    name = serializers.CharField(allow_blank=True)
    branch = serializers.IntegerField(required=False, allow_null=True)
    members = serializers.ListField(child=serializers.IntegerField(), required=False)
    body = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    client_token = serializers.UUIDField(required=False, allow_null=True)


class BroadcastCreateSerializer(FieldAccessMixin, serializers.Serializer):
    """Send a broadcast to staff."""

    field_resource = FIELD_RESOURCE

    title = serializers.CharField(allow_blank=True)
    body = serializers.CharField(allow_blank=True, trim_whitespace=False)
    reply_mode = serializers.ChoiceField(choices=ReplyMode.choices, required=False)
    branch = serializers.IntegerField(required=False, allow_null=True)
    recipients = serializers.ListField(child=serializers.IntegerField(), required=False)
    client_token = serializers.UUIDField(required=False, allow_null=True)
