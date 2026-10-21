"""Routes for the student app's messages, mounted at ``/v1/portal/student/messages/``."""
from django.urls import path

from .portal_views import (
    StudentContactsView,
    StudentConversationsView,
    StudentConversationView,
    StudentMessagesView,
    StudentReadView,
    StudentUnreadView,
)

urlpatterns = [
    path("conversations/", StudentConversationsView.as_view(), name="student-messages-conversations"),
    path("conversations/<int:pk>/", StudentConversationView.as_view(), name="student-messages-conversation"),
    path(
        "conversations/<int:pk>/messages/", StudentMessagesView.as_view(),
        name="student-messages-messages",
    ),
    path("conversations/<int:pk>/read/", StudentReadView.as_view(), name="student-messages-read"),
    path("unread-count/", StudentUnreadView.as_view(), name="student-messages-unread-count"),
    path("contacts/", StudentContactsView.as_view(), name="student-messages-contacts"),
]
