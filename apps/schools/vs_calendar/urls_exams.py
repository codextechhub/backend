from django.urls import path

from .views.exams import (
    ExamDetailView,
    ExamListCreateView,
    ExamPublishView,
    ExamSlotDetailView,
    ExamSlotListCreateView,
    ExamSlotPreviewView,
)

urlpatterns = [
    path("", ExamListCreateView.as_view(), name="calendar-exam-list"),
    path("<int:pk>/", ExamDetailView.as_view(), name="calendar-exam-detail"),
    path(
        "<int:exam_id>/slots/",
        ExamSlotListCreateView.as_view(), name="calendar-exam-slot-list",
    ),
    path(
        "<int:exam_id>/slots/preview/",
        ExamSlotPreviewView.as_view(), name="calendar-exam-slot-preview",
    ),
    path(
        "<int:exam_id>/slots/<int:pk>/",
        ExamSlotDetailView.as_view(), name="calendar-exam-slot-detail",
    ),
    path(
        "<int:exam_id>/publish/",
        ExamPublishView.as_view(), name="calendar-exam-publish",
    ),
]
