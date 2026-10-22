"""The parent and student apps' messaging routes.

Mounted inside the two portals (``/v1/portal/parent/messages/`` and
``/v1/portal/student/messages/``) and admitted by each portal's own
``PORTAL_PERMISSIONS``: no permission key, only the link between the signed-in
account and a guardian or pupil record, re-derived on every request. Every
conversation is reached through ``services.visibility`` with the portal's
surface, so a conversation of another family, another school or one that does
not exist answers the same 404.

The two apps are served by the same behaviour with two differences. A parent
names a child (``child_of`` gives the portal's own 404 for a child that is not
theirs) and may read, read-only, the pupil conversations of children whose
primary guardian they are; a pupil has only their own conversations, and every
list carries the notice that their guardian and the school can read them.

A conversation the caller can only watch answers 409 ``CONVERSATION_READ_ONLY``
to a send, and opening it (``read/``) changes nothing anyone else sees.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from schools.vs_parent_portal import permissions as parent_portal
from schools.vs_student_portal import permissions as student_portal

from .constants import OVERSIGHT_NOTICE, SURFACE_PARENT, SURFACE_STUDENT
from .exceptions import ConversationNotFound
from .portal_serializers import (
    ParentStartSerializer,
    PortalConversationDetailSerializer,
    PortalConversationSerializer,
    PortalMessageSerializer,
    PortalSendSerializer,
    StudentStartSerializer,
)
from .services import config, conversations, inbox, messages, people, receipts, visibility
from .throttles import SendLimited
from .views import page_of_messages

_FAMILIES_OFF = "Messaging with parents is switched off at this school."
_PUPILS_OFF = "Messaging with pupils is switched off at this school."


class _Portal:
    """What the parent and student routes share: the school, the caller and the rendering."""

    surface = ""

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No school in context.")
        return tenant

    @property
    def school_settings(self):
        if not hasattr(self, "_school_settings"):
            self._school_settings = config.LazySettings(self.tenant)
        return self._school_settings

    def visible(self, **extra):
        """Every conversation the caller may open on this surface, unlisted ones included."""
        raise NotImplementedError

    def labels_for(self, rows) -> dict:
        """``{student id: {staff user id: label}}`` for the children on a page."""
        raise NotImplementedError

    def render_context(self, rows):
        return {
            "user": self.request.user, "surface": self.surface,
            "settings": self.school_settings, "labels": self.labels_for(rows),
        }

    def render_rows(self, rows, serializer=PortalConversationSerializer):
        rows = list(rows)
        return serializer(rows, many=True, context=self.render_context(rows)).data

    def row(self, pk):
        """Conversation *pk* opened: the list row plus whether the app draws read ticks."""
        found = inbox.prepare(self.visible().filter(pk=pk), self.request.user).first()
        if found is None:
            raise ConversationNotFound()
        return self.render_rows([found], PortalConversationDetailSerializer)[0]

    def receipts_for(self, access):
        """The receipts the caller is shown in the conversation *access* was resolved for."""
        return receipts.Receipts(access, self.request.user, self.school_settings)

    def message_data(self, message, shown=None):
        """*message* as the caller reads it, with its entry of the ``shown`` receipts if any."""
        return PortalMessageSerializer(
            message, context={"user": self.request.user, "receipts": shown or {}},
        ).data

    def own_message_data(self, conversation_id, message):
        """A message the caller just stored, with its receipt, or None when nothing was stored."""
        if message is None:
            return None
        access = self.access(conversation_id)
        return self.message_data(message, self.receipts_for(access)([message]))

    def access(self, pk):
        return visibility.resolve(
            self.tenant, self.request.user, pk, surface=self.surface, settings=self.school_settings,
        )

    def list_response(self, request):
        user = request.user
        qs = inbox.listed_for(self.visible(**self.list_filters(request)), user)
        qs = inbox.search(
            qs, request.query_params.get("search"), user=user, surface=self.surface,
            titles=False, people=True, children=self.surface == SURFACE_PARENT, body=True,
        )
        qs = inbox.prepare(qs, user)
        if (request.query_params.get("unread") or "").lower() in ("1", "true", "yes"):
            qs = qs.filter(unread__gt=0).exclude(watching=True) if self.surface == SURFACE_PARENT \
                else qs.filter(unread__gt=0)
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(self.render_rows(page))
        response.data["unread_count"] = visibility.unread_counts(
            self.tenant, user, surface=self.surface,
        )["total"]
        response.data.update(self.list_extras())
        return response

    def list_filters(self, request) -> dict:
        return {}

    def list_extras(self) -> dict:
        return {}

    def send_response(self, request, pk):
        access = self.access(pk)
        writer = PortalSendSerializer(data=request.data)
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

    def messages_response(self, request, pk):
        """A page of messages, or the polling answer when ``?after=`` is sent."""
        access = self.access(pk)
        return success_response(data=page_of_messages(
            request, access, self.message_data, receipts=self.receipts_for(access),
        ))

    def read_response(self, request, pk):
        access = self.access(pk)
        up_to = request.data.get("up_to") if hasattr(request.data, "get") else None
        if up_to in (None, ""):
            up_to = None
        else:
            try:
                up_to = int(up_to)
            except (TypeError, ValueError):
                raise ValidationError({"up_to": "Send a message id."}) from None
        unread = messages.mark_read(access, request.user, up_to=up_to)
        return success_response("Marked as read.", data={"conversation": pk, "unread_count": unread})

    def started(self, conversation, message, created):
        return success_response(
            "Conversation started." if created else "Conversation found.",
            data={
                "conversation": self.row(conversation.pk),
                "message": self.own_message_data(conversation.pk, message),
            },
            status=201 if created else 200,
        )

    def contacts_response(self, request, student, *, can_start: bool, reason: str):
        reachable = people.staff_reachable_for_child(self.tenant, student, self.school_settings)
        profiles = people.staff_profiles_by_user(self.tenant, reachable)
        rows = sorted(
            (
                {
                    "user_id": user_id,
                    "name": inbox.person_name(profiles[user_id].user),
                    "label": label,
                }
                for user_id, label in reachable.items() if user_id in profiles
            ),
            key=lambda row: (_rank(row["label"]), row["name"].lower()),
        )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(page)
        response.data["can_start"] = can_start
        response.data["can_start_reason"] = "" if can_start else reason
        response.data.update(self.list_extras())
        return response


def _rank(label: str) -> int:
    """Class teacher first, the school office last, subject teachers between."""
    if label.startswith("Class teacher"):
        return 0
    return 2 if label == "School office" else 1


def _child_id(raw, *, required=True):
    if raw in (None, ""):
        if required:
            raise ValidationError({"child": "Choose a child."})
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"child": "Send a child's id."}) from None


# ── Parent ────────────────────────────────────────────────────────────────


class _ParentMessages(_Portal, APIView):
    permission_classes = parent_portal.PORTAL_PERMISSIONS
    surface = SURFACE_PARENT

    def visible(self, **extra):
        return visibility.guardian_conversations(self.tenant, self.request.user, **extra)

    def list_filters(self, request):
        child = _child_id(request.query_params.get("child"), required=False)
        if child is None:
            return {}
        return {"child": parent_portal.child_of(request, child)}

    def labels_for(self, rows):
        students = {row.student_id: row.student for row in rows if row.student_id}
        return {
            student_id: people.staff_reachable_for_child(self.tenant, student, self.school_settings)
            for student_id, student in students.items()
        }


class ParentConversationsView(SendLimited, _ParentMessages):
    """GET, POST /v1/portal/parent/messages/conversations/

    GET lists the parent's conversations with staff, newest activity first,
    and, read-only, the pupil conversations of children they are primary
    guardian of (``read_only`` is true and they are never counted as unread).
    ``?child=`` narrows to one of their children (another family's child is a
    404), ``?search=`` matches the staff member's name, the child's name and the
    words said, and ``?unread=true`` keeps the ones with something new. The page
    carries the parent's total ``unread_count``. POST starts a conversation
    about one child with one member of staff they may reach (or finds the one
    that exists): ``{"child", "staff", "body", "client_token"}``, 201 new and
    200 existing, answering ``{"conversation", "message"}``. A school that has
    switched parent messaging off refuses a start with 409
    ``CONVERSATION_READ_ONLY``.

    docstring-name: My conversations as a parent
    """

    def get(self, request):
        return self.list_response(request)

    def post(self, request):
        writer = ParentStartSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        child = parent_portal.child_of(request, data["child"])
        conversation, message, created = conversations.start_from_guardian(
            self.tenant, request.user, student_id=child.pk, staff_user_id=data["staff"],
            body=data.get("body", ""), client_token=data.get("client_token"),
        )
        return self.started(conversation, message, created)


class ParentConversationView(_ParentMessages):
    """GET /v1/portal/parent/messages/conversations/<id>/

    One conversation the parent takes part in, or watches as a primary
    guardian. Another family's conversation, another school's and one that does
    not exist answer the same 404.

    docstring-name: A conversation as a parent
    """

    def get(self, request, pk):
        self.access(pk)
        return success_response(data=self.row(pk))


class ParentMessagesView(SendLimited, _ParentMessages):
    """GET, POST /v1/portal/parent/messages/conversations/<id>/messages/

    GET pages the messages, newest first, with a ``before`` cursor:
    ``{"results", "has_more", "next_before"}``; ``?after=<message id>`` polls
    instead, answering the newer messages and the refreshed receipts of the
    parent's own recent ones. POST writes a message: 201, or
    200 with the stored message when ``client_token`` was already used. A
    conversation the parent can only watch answers 409 ``CONVERSATION_READ_ONLY``.

    docstring-name: Messages as a parent
    """

    def get(self, request, pk):
        return self.messages_response(request, pk)

    def post(self, request, pk):
        return self.send_response(request, pk)


class ParentReadView(_ParentMessages):
    """POST /v1/portal/parent/messages/conversations/<id>/read/

    Marks the conversation read up to ``up_to`` (default the newest message) and
    clears its bell entries. On a conversation the parent only watches nothing
    changes for anybody.

    docstring-name: Mark a conversation read as a parent
    """

    def post(self, request, pk):
        return self.read_response(request, pk)


class ParentUnreadView(_ParentMessages):
    """GET /v1/portal/parent/messages/unread-count/

    ``{"total": n}``: unread messages in the parent's own conversations. A
    pupil conversation they only watch is never counted.

    docstring-name: Unread messages as a parent
    """

    def get(self, request):
        return success_response(data=visibility.unread_counts(
            self.tenant, request.user, surface=SURFACE_PARENT,
        ))


class ParentContactsView(_ParentMessages):
    """GET /v1/portal/parent/messages/contacts/?child=<id>

    The staff the parent may write to about that child, each with the label the
    family reads ("Class teacher, JSS1 B", the subjects taught, "School
    office"). A child that is not theirs is the portal's 404. ``can_start`` is
    false, with the reason, where the school has switched parent messaging off.

    docstring-name: Who I can message about a child
    """

    def get(self, request):
        child = parent_portal.child_of(request, _child_id(request.query_params.get("child")))
        on = self.school_settings.families_enabled
        return self.contacts_response(request, child, can_start=on, reason=_FAMILIES_OFF)


# ── Student ───────────────────────────────────────────────────────────────


class _StudentMessages(_Portal, APIView):
    permission_classes = student_portal.PORTAL_PERMISSIONS
    surface = SURFACE_STUDENT

    @property
    def student(self):
        return student_portal.linked_student(self.request)

    def visible(self, **extra):
        return visibility.pupil_conversations(self.tenant, self.request.user)

    def labels_for(self, rows):
        if not rows:
            return {}
        return {
            self.student.pk: people.staff_reachable_for_child(
                self.tenant, self.student, self.school_settings,
            ),
        }

    def list_extras(self):
        return {"oversight_notice": OVERSIGHT_NOTICE}


class StudentConversationsView(SendLimited, _StudentMessages):
    """GET, POST /v1/portal/student/messages/conversations/

    GET lists the pupil's own conversations with staff, newest activity first,
    with ``?search=`` and ``?unread=true``. The page carries ``unread_count``
    and the ``oversight_notice`` that their parent or guardian and the school
    can read their messages. POST starts a conversation with one member of staff
    who teaches them or is the school office (or finds the one that exists):
    ``{"staff", "body", "client_token"}``, 201 new and 200 existing, answering
    ``{"conversation", "message"}``. A school that has switched pupil messaging
    off refuses a start with 409 ``CONVERSATION_READ_ONLY``.

    docstring-name: My conversations as a pupil
    """

    def get(self, request):
        return self.list_response(request)

    def post(self, request):
        writer = StudentStartSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        conversation, message, created = conversations.start_from_pupil(
            self.tenant, request.user, staff_user_id=data["staff"],
            body=data.get("body", ""), client_token=data.get("client_token"),
        )
        return self.started(conversation, message, created)


class StudentConversationView(_StudentMessages):
    """GET /v1/portal/student/messages/conversations/<id>/

    One of the pupil's own conversations. Anyone else's, another school's and
    one that does not exist answer the same 404.

    docstring-name: A conversation as a pupil
    """

    def get(self, request, pk):
        self.access(pk)
        return success_response(data=self.row(pk))


class StudentMessagesView(SendLimited, _StudentMessages):
    """GET, POST /v1/portal/student/messages/conversations/<id>/messages/

    GET pages the messages, newest first, with a ``before`` cursor, or polls
    with ``?after=<message id>`` (see the parent route). POST writes
    a message: 201, or 200 with the stored message when ``client_token`` was
    already used. 409 ``CONVERSATION_READ_ONLY`` when the member of staff has
    left or the school has switched pupil messaging off.

    docstring-name: Messages as a pupil
    """

    def get(self, request, pk):
        return self.messages_response(request, pk)

    def post(self, request, pk):
        return self.send_response(request, pk)


class StudentReadView(_StudentMessages):
    """POST /v1/portal/student/messages/conversations/<id>/read/

    Marks the conversation read up to ``up_to`` (default the newest message) and
    clears its bell entries.

    docstring-name: Mark a conversation read as a pupil
    """

    def post(self, request, pk):
        return self.read_response(request, pk)


class StudentUnreadView(_StudentMessages):
    """GET /v1/portal/student/messages/unread-count/

    ``{"total": n}``: unread messages in the pupil's own conversations.

    docstring-name: Unread messages as a pupil
    """

    def get(self, request):
        return success_response(data=visibility.unread_counts(
            self.tenant, request.user, surface=SURFACE_STUDENT,
        ))


class StudentContactsView(_StudentMessages):
    """GET /v1/portal/student/messages/contacts/

    The pupil's own teachers and the school office they may write to, each with
    a label. ``can_start`` is false, with the reason, where the school has
    switched pupil messaging off. The page carries the ``oversight_notice``.

    docstring-name: Who I can message as a pupil
    """

    def get(self, request):
        on = self.school_settings.pupils_enabled
        return self.contacts_response(request, self.student, can_start=on, reason=_PUPILS_OFF)
