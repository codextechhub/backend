"""Routes for the parent app's messages, mounted at ``/v1/portal/parent/messages/``."""
from django.urls import path

from .portal_views import (
    ParentContactsView,
    ParentConversationsView,
    ParentConversationView,
    ParentMessagesView,
    ParentReadView,
    ParentUnreadView,
)

urlpatterns = [
    path("conversations/", ParentConversationsView.as_view(), name="parent-messages-conversations"),
    path("conversations/<int:pk>/", ParentConversationView.as_view(), name="parent-messages-conversation"),
    path(
        "conversations/<int:pk>/messages/", ParentMessagesView.as_view(),
        name="parent-messages-messages",
    ),
    path("conversations/<int:pk>/read/", ParentReadView.as_view(), name="parent-messages-read"),
    path("unread-count/", ParentUnreadView.as_view(), name="parent-messages-unread-count"),
    path("contacts/", ParentContactsView.as_view(), name="parent-messages-contacts"),
]
