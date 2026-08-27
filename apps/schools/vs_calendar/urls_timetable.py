from django.urls import path

from .views.actions import (
    ClassTimetableClearView,
    ClassTimetableDuplicateView,
    ClassTimetablePublishView,
)
from .views.periods import PeriodDetailView, PeriodListCreateView
from .views.rooms import RoomDetailView, RoomListCreateView
from .views.teachers import TeacherListView, TeacherTimetableView
from .views.timetable import (
    ClassTimetableDetailView,
    ClassTimetableListView,
    SlotDetailView,
    SlotListCreateView,
)

urlpatterns = [
    path("rooms/", RoomListCreateView.as_view(), name="calendar-room-list"),
    path("rooms/<int:pk>/", RoomDetailView.as_view(), name="calendar-room-detail"),

    path("periods/", PeriodListCreateView.as_view(), name="calendar-period-list"),
    path("periods/<int:pk>/", PeriodDetailView.as_view(), name="calendar-period-detail"),

    path("classes/", ClassTimetableListView.as_view(), name="calendar-class-list"),
    path(
        "classes/<int:class_id>/",
        ClassTimetableDetailView.as_view(), name="calendar-class-grid",
    ),
    path(
        "classes/<int:class_id>/duplicate/",
        ClassTimetableDuplicateView.as_view(), name="calendar-class-duplicate",
    ),
    path(
        "classes/<int:class_id>/clear/",
        ClassTimetableClearView.as_view(), name="calendar-class-clear",
    ),
    path(
        "classes/<int:class_id>/publish/",
        ClassTimetablePublishView.as_view(), name="calendar-class-publish",
    ),

    path("slots/", SlotListCreateView.as_view(), name="calendar-slot-list"),
    path("slots/<int:pk>/", SlotDetailView.as_view(), name="calendar-slot-detail"),

    path("teachers/", TeacherListView.as_view(), name="calendar-teacher-list"),
    path(
        "teachers/<int:user_id>/",
        TeacherTimetableView.as_view(), name="calendar-teacher-grid",
    ),
]
