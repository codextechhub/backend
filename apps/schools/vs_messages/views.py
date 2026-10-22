"""The staff routes for messaging, mounted at ``/v1/messages/``.

Every route needs an RBAC key; which one is stated on each view. What a key
opens is never the whole story: a conversation is reached only through
``services.visibility.resolve``, and a conversation the caller may not reach
answers 404 with the same body as one that does not exist. Where a route is
open to two keys (a start or a send, which are ``messages.send`` for staff
conversations and ``family_messages.send`` for parent and pupil ones) the view
checks the key the particular conversation needs.

The views with a route that stores a message carry ``SendLimited``, so one
send budget per person covers all of them (see ``throttles``).

Field Access is enforced on every read and every write: serializers carry the
mixin, and the hand-built contact and count payloads go through
``can_read`` and ``FieldReadDenied``. ``pending_tenant_surface`` is left
closed: a school that has not gone live has nobody to message.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_audit.models import AuditActionType
from vs_rbac.evaluator import has_permission
from vs_rbac.field_enforcement import FieldReadDenied, assert_writable, can_read
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .constants import (
    FIELD_RESOURCE,
    MESSAGE_PAGE_DEFAULT,
    MESSAGE_PAGE_MAX,
    POLL_LIMIT,
    PERM_BROADCAST_INSPECT,
    PERM_BROADCAST_SEND,
    PERM_EXPORT,
    PERM_FAMILY_SEND,
    PERM_GROUP_CREATE,
    PERM_GROUP_INSPECT,
    PERM_SEND,
    PERM_VIEW,
    PERM_WELFARE_INSPECT,
    SURFACE_STAFF,
    ConversationKind,
    ReplyMode,
)
from .exceptions import ConversationNotFound
from .serializers import (
    BroadcastCreateSerializer,
    ConversationDetailSerializer,
    ConversationRowSerializer,
    GroupCreateSerializer,
    MessageSerializer,
    OversightDetailSerializer,
    OversightRowSerializer,
    SendMessageSerializer,
    StartConversationSerializer,
)
from .services import (
    audit,
    broadcasts,
    config,
    conversations,
    export,
    groups,
    inbox,
    messages,
    oversight,
    people,
    receipts,
    visibility,
)
from .throttles import SendLimited

_KEY = FIELD_RESOURCE + "."


def _may(request, name) -> bool:
    return can_read(request, _KEY + name)


class _MessagesView(APIView):
    """Common ground: the tenant, the render context and the standard gate."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = PERM_VIEW

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No school in context.")
        return tenant

    def context(self, **extra):
        """The context every conversation row is rendered with, built once per request."""
        if not hasattr(self, "_context"):
            from schools.vs_students.services.scoping import branch_dimension_applies

            self._context = {
                "request": self.request,
                "user": self.request.user,
                "keys": visibility.caller_keys(self.tenant, self.request.user),
                "settings": config.LazySettings(self.tenant),
                "multi_branch": branch_dimension_applies(self.tenant),
                "surface": SURFACE_STAFF,
            }
        return {**self._context, **extra}

    def has_key(self, key) -> bool:
        return has_permission(self.request.user, key, tenant=self.tenant)

    def row(self, pk, serializer=ConversationDetailSerializer):
        """Conversation *pk* as the caller sees it, rendered with *serializer*."""
        user = self.request.user
        found = inbox.prepare(
            visibility.staff_conversations(self.tenant, user).filter(pk=pk), user,
        ).first()
        if found is None:
            raise ConversationNotFound()
        return serializer(found, context=self.context()).data

    def receipts_for(self, access):
        """The receipts the caller is shown in the conversation *access* was resolved for."""
        return receipts.Receipts(
            access, self.request.user, self.context()["settings"],
            allowed=_may(self.request, "read_receipts"),
        )

    def message_data(self, message, shown=None):
        """*message* as the caller sees it, with its entry of the ``shown`` receipts if any."""
        return MessageSerializer(
            message,
            context={"request": self.request, "user": self.request.user, "receipts": shown or {}},
        ).data

    def own_message_data(self, conversation_id, message):
        """A message the caller just stored, with its receipt, or None when nothing was stored."""
        if message is None:
            return None
        access = visibility.resolve(
            self.tenant, self.request.user, conversation_id, surface=SURFACE_STAFF,
            settings=self.context()["settings"], keys=self.context()["keys"],
        )
        return self.message_data(message, self.receipts_for(access)([message]))


def _int_param(request, name, default=None):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Send a whole number."}) from None


# ── Inbox ─────────────────────────────────────────────────────────────────


class ConversationListCreateView(SendLimited, _MessagesView):
    """GET, POST /v1/messages/conversations/

    GET is the inbox: the conversations the caller takes part in, newest
    activity first, filtered by ``?kind=`` (``direct``, ``family``, ``group``,
    ``broadcast``), ``?unread=true`` and ``?search=``. POST starts a staff,
    parent or pupil conversation (or finds the one that already exists) and
    sends ``body`` when given: 201 for a new conversation, 200 for an existing
    one.

    docstring-name: Conversations
    """

    def get_permissions(self):
        if self.request.method == "GET":
            self.rbac_permission = PERM_VIEW
        else:
            self.rbac_permission = [PERM_SEND, PERM_FAMILY_SEND]
        return super().get_permissions()

    def get(self, request):
        user = request.user
        qs = visibility.staff_conversations(self.tenant, user)
        qs = inbox.listed_for(qs, user)
        qs = inbox.by_kind(qs, request.query_params.get("kind"))
        qs = inbox.search(
            qs, request.query_params.get("search"), user=user,
            titles=_may(request, "title"), people=_may(request, "participants"),
            children=_may(request, "child"), body=_may(request, "body"),
        )
        qs = inbox.prepare(qs, user)
        if (request.query_params.get("unread") or "").lower() in ("1", "true", "yes"):
            qs = qs.filter(unread__gt=0)
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = ConversationRowSerializer(page, many=True, context=self.context()).data
        return paginator.get_paginated_response(data)

    def post(self, request):
        writer = StartConversationSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        kind = data["kind"]
        needed = PERM_SEND if kind == ConversationKind.STAFF_DIRECT else PERM_FAMILY_SEND
        if not self.has_key(needed):
            raise PermissionDenied()
        body = data.get("body", "")
        token = data.get("client_token")
        if kind == ConversationKind.STAFF_DIRECT:
            conversation, message, created = conversations.start_staff_direct(
                self.tenant, request.user, recipient_id=data.get("recipient"),
                body=body, client_token=token,
            )
        else:
            conversation, message, created = conversations.start_family_from_staff(
                self.tenant, request.user, kind=kind, student_id=data.get("student"),
                guardian_id=data.get("guardian"), body=body, client_token=token,
            )
        return success_response(
            "Conversation started." if created else "Conversation found.",
            data={
                "conversation": self.row(conversation.pk),
                "message": self.own_message_data(conversation.pk, message),
            },
            status=201 if created else 200,
        )


class ConversationDetailView(_MessagesView):
    """GET /v1/messages/conversations/<id>/

    One conversation the caller takes part in, with the people in it (and, for
    a broadcast, how many it went to, and who for its sender).

    docstring-name: A conversation
    """

    def get(self, request, pk):
        return success_response(data=self.row(pk))


class MessageListCreateView(SendLimited, _MessagesView):
    """GET, POST /v1/messages/conversations/<id>/messages/

    GET pages the messages the caller may read, newest first, with a
    ``before`` cursor: ``{"results", "has_more", "next_before"}``. With
    ``?after=<message id>`` it instead polls: the newer messages oldest first
    (at most 100) and the refreshed receipts of the caller's own recent ones
    (see :func:`page_of_messages`). POST writes
    a message: 201, or 200 with the stored message when ``client_token`` was
    already used. A conversation the caller can read but not write in answers
    409 ``CONVERSATION_READ_ONLY`` with the reason.

    docstring-name: Messages
    """

    def get_permissions(self):
        if self.request.method == "GET":
            self.rbac_permission = PERM_VIEW
        else:
            self.rbac_permission = [PERM_SEND, PERM_FAMILY_SEND]
        return super().get_permissions()

    def get(self, request, pk):
        access = visibility.resolve(self.tenant, request.user, pk, surface=SURFACE_STAFF)
        return success_response(data=page_of_messages(
            request, access, self.message_data, receipts=self.receipts_for(access),
        ))

    def post(self, request, pk):
        access = visibility.resolve(
            self.tenant, request.user, pk, surface=SURFACE_STAFF,
            settings=self.context()["settings"], keys=self.context()["keys"],
        )
        writer = SendMessageSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        message, created = messages.send_message(
            access, sender=request.user, body=data.get("body", ""),
            client_token=data.get("client_token"),
        )
        return success_response(
            "Message sent.",
            data=self.message_data(message, self.receipts_for(access)([message])),
            status=201 if created else 200,
        )


def page_of_messages(request, access, render, *, receipts=None, limit_default=MESSAGE_PAGE_DEFAULT):
    """One page of the messages *access* may read.

    ``?before=<id>`` returns messages older than that id, newest first;
    ``?limit=`` is clamped to 1 through 100. ``next_before`` is the id to ask
    for the next older page, or null on the last.

    ``?after=<id>`` is the polling form, served only where *receipts* is given:
    the messages newer than that id, oldest first, at most 100, with
    ``has_more`` when there are more. See :func:`poll_after`.

    *render* turns one message and the page's receipts into its payload.
    *receipts* is a :class:`services.receipts.Receipts`: each of the caller's
    own messages on the page is rendered with its receipt, computed once for
    the page. Without it (an inspection) no message carries one.
    """
    after = _int_param(request, "after")
    if after is not None and receipts is not None:
        return poll_after(access, render, receipts, after)
    limit = _int_param(request, "limit", limit_default)
    limit = max(1, min(limit, MESSAGE_PAGE_MAX))
    before = _int_param(request, "before")
    qs = visibility.visible_messages(access).select_related("sender").order_by("-id")
    if before is not None:
        qs = qs.filter(id__lt=before)
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    shown = receipts(rows) if receipts is not None else {}
    return {
        "results": [render(m, shown) for m in rows],
        "has_more": has_more,
        "next_before": rows[-1].pk if has_more and rows else None,
    }


def poll_after(access, render, receipts, after):
    """What changed in a conversation since message *after*: new messages and fresh receipts.

    ``results`` are the visible messages with a greater id, oldest first, at
    most 100, each own message carrying its receipt. ``receipts`` lists
    ``{"id", "state", "read_count", "recipient_count"}`` for the caller's own
    messages among the latest 100 visible ones (and among the results), so a
    tick that turned since the last poll turns on screen. The list is left out
    entirely when the caller is shown no receipts (setting off, hidden by Field
    Access, inspecting, or watching), exactly as a message then has no
    ``receipt``. The same visibility gate as a page applies, so a person who
    joined a group late never receives what came before.
    """
    visible = visibility.visible_messages(access)
    rows = list(visible.filter(id__gt=after).select_related("sender").order_by("id")[: POLL_LIMIT + 1])
    has_more = len(rows) > POLL_LIMIT
    rows = rows[:POLL_LIMIT]
    recent = (
        list(visible.order_by("-id").only("id", "sender_id")[:POLL_LIMIT])
        if receipts.enabled else []
    )
    shown = receipts([*rows, *recent])
    data = {"results": [render(m, shown) for m in rows], "has_more": has_more}
    if receipts.enabled:
        data["receipts"] = [{"id": pk, **shown[pk]} for pk in sorted(shown)]
    return data


class ConversationReadView(_MessagesView):
    """POST /v1/messages/conversations/<id>/read/

    Moves the caller's read watermark forward (never back) to ``up_to``, or to
    the newest message they may read, and returns what is still unread.

    docstring-name: Mark a conversation read
    """

    def post(self, request, pk):
        access = visibility.resolve(self.tenant, request.user, pk, surface=SURFACE_STAFF)
        up_to = request.data.get("up_to") if hasattr(request.data, "get") else None
        if up_to not in (None, ""):
            try:
                up_to = int(up_to)
            except (TypeError, ValueError):
                raise ValidationError({"up_to": "Send a message id."}) from None
        else:
            up_to = None
        unread = messages.mark_read(access, request.user, up_to=up_to)
        data = {"conversation": pk}
        if _may(request, "read_status"):
            data["unread_count"] = unread
        return success_response("Marked as read.", data=data)


class ConversationExportView(_MessagesView):
    """GET /v1/messages/conversations/<id>/export/

    Downloads the messages the caller may read in a conversation they take part
    in as ``conversation-<id>.csv``: ``sent_at``, ``sender`` and ``message``,
    oldest first, each column present only when Field Access lets the caller
    read what it carries (403 when none). Every download is audited, without
    the text. A conversation the caller does not take part in answers 404.

    docstring-name: Download a conversation
    """

    rbac_permission = PERM_EXPORT

    def get(self, request, pk):
        access = visibility.resolve(self.tenant, request.user, pk, surface=SURFACE_STAFF)
        columns = export.readable_columns(lambda field: _may(request, field))
        if not columns:
            raise FieldReadDenied(
                [field for _, field in export.COLUMNS], export.LABELS,
            )
        content, count = export.build(self.tenant, access, columns)
        conversation = access.conversation
        audit.record(
            conversation, AuditActionType.CONVERSATION_EXPORTED, request.user,
            f"{request.user.full_name} downloaded {count} messages of conversation {conversation.pk}.",
            metadata={"message_count": count, "columns": [header for header, _ in columns]},
        )
        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{export.filename_for(conversation)}"'
        return response


class UnreadCountView(_MessagesView):
    """GET /v1/messages/unread-count/

    ``{"total", "direct", "family", "group", "broadcast"}`` in one query. The
    whole response is the read-status field, so a role that may not read it is
    refused rather than shown zeros.

    docstring-name: Unread messages
    """

    def get(self, request):
        if not _may(request, "read_status"):
            raise FieldReadDenied(["unread_count"], {"unread_count": "Read status and unread counts"})
        return success_response(data=visibility.unread_counts(
            self.tenant, request.user, surface=SURFACE_STAFF,
        ))


# ── Contacts ──────────────────────────────────────────────────────────────


class ContactsView(_MessagesView):
    """GET /v1/messages/contacts/

    ``?kind=staff`` lists the staff the caller may start a conversation with,
    add to a group or address in a broadcast (``messages.send``). ``?kind=family``
    lists the pupils the caller may reach with their guardians
    (``family_messages.send``). Both are paginated and searchable.

    docstring-name: Who can be messaged
    """

    def get_permissions(self):
        self.rbac_permission = [PERM_SEND, PERM_FAMILY_SEND]
        return super().get_permissions()

    def get(self, request):
        kind = (request.query_params.get("kind") or "staff").lower()
        if kind == "family":
            return self.family(request)
        if kind != "staff":
            raise ValidationError({"kind": "Choose staff or family."})
        return self.staff(request)

    def staff(self, request):
        if not self.has_key(PERM_SEND):
            raise PermissionDenied()
        if not _may(request, "participants"):
            raise FieldReadDenied(["participants"], {"participants": "People in the conversation"})
        branch = _int_param(request, "branch")
        profiles = people.staff_contacts(
            self.tenant, request.user, search=request.query_params.get("search", ""),
            branch_id=branch,
        )
        multi = self.context()["multi_branch"]
        paginator = XVSPagination()
        page = paginator.paginate_queryset(profiles, request, view=self)
        data = []
        for profile in page:
            row = {
                "user_id": profile.user_id,
                "name": inbox.person_name(profile.user),
                "label": people.staff_label(profile),
            }
            if multi:
                row["branch"] = (
                    {"id": profile.branch_id, "name": profile.branch.name} if profile.branch_id else None
                )
            data.append(row)
        return paginator.get_paginated_response(data)

    def family(self, request):
        if not self.has_key(PERM_FAMILY_SEND):
            raise PermissionDenied()
        names_child = _may(request, "child")
        names_people = _may(request, "participants")
        if not (names_child or names_people):
            raise FieldReadDenied(
                ["child", "participants"],
                {"child": "Pupil the conversation is about", "participants": "People in the conversation"},
            )
        students = people.family_children(
            self.tenant, request.user, self.context()["settings"],
            search=request.query_params.get("search", ""), name_search=names_child,
        )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(students, request, view=self)
        data = []
        for student in page:
            row = {"pupil_has_account": student.user_id is not None and student.user.is_active}
            if names_child:
                row["student"] = {
                    "id": student.pk, "name": student.full_name,
                    "class_name": getattr(student, "child_class_name", None) or "",
                }
            else:
                row["id"] = student.pk
            if names_people:
                row["guardians"] = [
                    {
                        "id": link.guardian_id, "name": link.guardian.full_name,
                        "relationship": link.relationship_label,
                        "has_account": link.guardian.user_id is not None
                        and link.guardian.user.is_active,
                    }
                    for link in student.guardian_links.all()
                ]
            data.append(row)
        return paginator.get_paginated_response(data)


# ── Groups ────────────────────────────────────────────────────────────────


class GroupCreateView(SendLimited, _MessagesView):
    """POST /v1/messages/groups/

    Makes a staff group with the caller as its owner. The name and the first
    members are open to whoever may make a group; ``body`` sends a first
    message.

    docstring-name: Make a group
    """

    rbac_permission = PERM_GROUP_CREATE

    def post(self, request):
        writer = GroupCreateSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        group, created = groups.create_group(
            self.tenant, request.user, name=data.get("name"), branch_id=data.get("branch"),
            member_ids=data.get("members", []), body=data.get("body", ""),
            client_token=data.get("client_token"),
        )
        return success_response(
            "Group created." if created else "Group found.",
            data=self.row(group.pk), status=201 if created else 200,
        )


class GroupDetailView(_MessagesView):
    """PATCH /v1/messages/groups/<id>/

    An owner renames the group.

    docstring-name: Rename a group
    """

    rbac_permission = PERM_SEND

    def patch(self, request, pk):
        access = groups.group_access(self.tenant, request.user, pk)
        name = request.data.get("name") if hasattr(request.data, "get") else None
        assert_writable(
            request, FIELD_RESOURCE, {"name": name}, current={"name": access.conversation.title},
        )
        groups.rename_group(access, request.user, name)
        return success_response("Group renamed.", data=self.row(pk))


class GroupMembersView(_MessagesView):
    """POST /v1/messages/groups/<id>/members/

    An owner adds staff. Each new member reads only what is said after they
    are added.

    docstring-name: Add people to a group
    """

    rbac_permission = PERM_SEND

    def post(self, request, pk):
        access = groups.group_access(self.tenant, request.user, pk)
        ids = request.data.get("members") if hasattr(request.data, "get") else None
        assert_writable(request, FIELD_RESOURCE, {"members": ids})
        groups.add_members(access, request.user, ids)
        return success_response("People added.", data=self.row(pk))


class GroupMemberView(_MessagesView):
    """DELETE /v1/messages/groups/<id>/members/<user_id>/

    An owner removes a member, or a member leaves. A removed or departed
    member loses all access to the group.

    docstring-name: Remove someone from a group
    """

    def delete(self, request, pk, user_id):
        access = groups.group_access(self.tenant, request.user, pk)
        if user_id != request.user.pk:
            assert_writable(request, FIELD_RESOURCE, {"members": [user_id]})
        groups.remove_member(access, request.user, user_id)
        return success_response("Done.", data={"group": pk, "user_id": user_id})


class GroupOwnerView(_MessagesView):
    """POST /v1/messages/groups/<id>/members/<user_id>/owner/

    An owner makes a current member an owner too.

    docstring-name: Make a group owner
    """

    rbac_permission = PERM_SEND

    def post(self, request, pk, user_id):
        access = groups.group_access(self.tenant, request.user, pk)
        assert_writable(request, FIELD_RESOURCE, {"members": [user_id]})
        groups.make_owner(access, request.user, user_id)
        return success_response("Owner added.", data=self.row(pk))


# ── Broadcasts ────────────────────────────────────────────────────────────


class BroadcastCreateView(SendLimited, _MessagesView):
    """POST /v1/messages/broadcasts/

    Sends a broadcast to staff. ``reply_mode`` is fixed when it is sent, and
    is ``ONE_WAY`` when the caller may not choose it.

    docstring-name: Send a broadcast
    """

    rbac_permission = PERM_BROADCAST_SEND

    def post(self, request):
        writer = BroadcastCreateSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        broadcast, created = broadcasts.send_broadcast(
            self.tenant, request.user, title=data.get("title"), body=data.get("body", ""),
            recipient_ids=data.get("recipients", []), branch_id=data.get("branch"),
            reply_mode=data.get("reply_mode", ReplyMode.ONE_WAY),
            client_token=data.get("client_token"),
        )
        return success_response(
            "Broadcast sent." if created else "Broadcast found.",
            data=self.row(broadcast.pk), status=201 if created else 200,
        )


class BroadcastReplyView(SendLimited, _MessagesView):
    """POST /v1/messages/broadcasts/<id>/reply/

    A recipient's private reply: finds or creates their reply conversation with
    the sender and writes in it. 409 ``BROADCAST_ONE_WAY`` when the broadcast
    takes no replies.

    docstring-name: Reply to a broadcast
    """

    rbac_permission = PERM_SEND

    def post(self, request, pk):
        writer = SendMessageSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        thread, message, created = broadcasts.reply_privately(
            self.tenant, request.user, pk, body=data.get("body", ""),
            client_token=data.get("client_token"),
        )
        return success_response(
            "Reply sent.",
            data={
                "conversation": self.row(thread.pk),
                "message": self.own_message_data(thread.pk, message),
            },
            status=201 if created else 200,
        )


# ── Oversight ─────────────────────────────────────────────────────────────


class _OversightView(_MessagesView):
    """Shared by the leaders' read-only routes."""

    def list_context(self):
        return {"request": self.request, "user": self.request.user,
                "multi_branch": self.context()["multi_branch"]}


class OversightListView(_OversightView):
    """GET /v1/messages/oversight/groups/, .../broadcasts/, .../pupil-conversations/

    The conversations of one kind inside the caller's branch reach, newest
    first, with no message text. The key is the one for that kind.

    docstring-name: Conversations open to inspection
    """

    surface = None
    key = None

    def get_permissions(self):
        self.rbac_permission = self.key
        return super().get_permissions()

    def get(self, request):
        qs = oversight.listing(self.tenant, request.user, self.surface)
        qs = oversight.search(
            qs, request.query_params.get("search"),
            titles=_may(request, "title"), people=_may(request, "participants"),
            children=_may(request, "child"),
        )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = OversightRowSerializer(page, many=True, context=self.list_context()).data
        return paginator.get_paginated_response(data)


class OversightDetailView(_OversightView):
    """GET /v1/messages/oversight/conversations/<id>/

    Opens a group, a broadcast or a pupil conversation read-only. Every call is
    audited. A conversation of any other kind, or of a kind the caller holds no
    inspection key for, answers 404 like one that does not exist.

    docstring-name: Inspect a conversation
    """

    rbac_permission = [PERM_GROUP_INSPECT, PERM_BROADCAST_INSPECT, PERM_WELFARE_INSPECT]

    def get(self, request, pk):
        access = oversight.resolve_any(self.tenant, request.user, pk)
        oversight.record_opening(access, request.user, what="detail")
        row = oversight.listing(self.tenant, request.user, access.surface).get(pk=access.conversation.pk)
        return success_response(
            data=OversightDetailSerializer(row, context=self.list_context()).data,
        )


class OversightMessagesView(_OversightView):
    """GET /v1/messages/oversight/conversations/<id>/messages/

    The messages of an inspected conversation, newest first, with the same
    cursor as the staff route. Every call is audited and no read watermark
    moves.

    docstring-name: Read an inspected conversation
    """

    rbac_permission = OversightDetailView.rbac_permission

    def get(self, request, pk):
        access = oversight.resolve_any(self.tenant, request.user, pk)
        oversight.record_opening(access, request.user, what="messages")
        data = page_of_messages(request, access, self.message_data)
        data["read_only"] = True
        data["inspection"] = True
        return success_response(data=data)
