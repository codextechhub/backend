"""Routes for messaging, mounted at ``/v1/messages/``.

Literal segments (``unread-count/``, ``contacts/``, ``oversight/``) never clash
with the integer conversation ids, which are matched with ``<int:pk>``.
"""
from django.urls import path

from .constants import (
    PERM_BROADCAST_INSPECT,
    PERM_GROUP_INSPECT,
    PERM_WELFARE_INSPECT,
    SURFACE_INSPECT_BROADCAST,
    SURFACE_INSPECT_GROUP,
    SURFACE_INSPECT_WELFARE,
)
from .views import (
    BroadcastCreateView,
    BroadcastReplyView,
    ContactsView,
    ConversationDetailView,
    ConversationExportView,
    ConversationListCreateView,
    ConversationReadView,
    GroupCreateView,
    GroupDetailView,
    GroupMembersView,
    GroupMemberView,
    GroupOwnerView,
    MessageListCreateView,
    OversightDetailView,
    OversightListView,
    OversightMessagesView,
    UnreadCountView,
)

urlpatterns = [
    path("conversations/", ConversationListCreateView.as_view(), name="messages-conversations"),
    path("conversations/<int:pk>/", ConversationDetailView.as_view(), name="messages-conversation"),
    path("conversations/<int:pk>/messages/", MessageListCreateView.as_view(), name="messages-messages"),
    path("conversations/<int:pk>/read/", ConversationReadView.as_view(), name="messages-read"),
    path(
        "conversations/<int:pk>/export/", ConversationExportView.as_view(),
        name="messages-export",
    ),
    path("unread-count/", UnreadCountView.as_view(), name="messages-unread-count"),
    path("contacts/", ContactsView.as_view(), name="messages-contacts"),
    path("groups/", GroupCreateView.as_view(), name="messages-group-create"),
    path("groups/<int:pk>/", GroupDetailView.as_view(), name="messages-group"),
    path("groups/<int:pk>/members/", GroupMembersView.as_view(), name="messages-group-members"),
    path(
        "groups/<int:pk>/members/<int:user_id>/",
        GroupMemberView.as_view(), name="messages-group-member",
    ),
    path(
        "groups/<int:pk>/members/<int:user_id>/owner/",
        GroupOwnerView.as_view(), name="messages-group-owner",
    ),
    path("broadcasts/", BroadcastCreateView.as_view(), name="messages-broadcast-create"),
    path("broadcasts/<int:pk>/reply/", BroadcastReplyView.as_view(), name="messages-broadcast-reply"),
    path(
        "oversight/groups/",
        OversightListView.as_view(surface=SURFACE_INSPECT_GROUP, key=PERM_GROUP_INSPECT),
        name="messages-oversight-groups",
    ),
    path(
        "oversight/broadcasts/",
        OversightListView.as_view(surface=SURFACE_INSPECT_BROADCAST, key=PERM_BROADCAST_INSPECT),
        name="messages-oversight-broadcasts",
    ),
    path(
        "oversight/pupil-conversations/",
        OversightListView.as_view(surface=SURFACE_INSPECT_WELFARE, key=PERM_WELFARE_INSPECT),
        name="messages-oversight-pupils",
    ),
    path(
        "oversight/conversations/<int:pk>/",
        OversightDetailView.as_view(), name="messages-oversight-conversation",
    ),
    path(
        "oversight/conversations/<int:pk>/messages/",
        OversightMessagesView.as_view(), name="messages-oversight-messages",
    ),
]
