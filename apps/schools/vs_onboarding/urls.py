"""Routes for /v1/onboarding/.

This file is also the onboarding surface: every path here is reachable by a
school that has not gone live, and adding one is a decision to open it to
schools that are still being set up. Take it deliberately.
"""
from django.urls import path

from .views import (
    GoLiveApproveView,
    GoLiveRejectView,
    GoLiveRequestCreateView,
    GoLiveRequestListView,
    OnboardingProvisionView,
    OnboardingReinstateView,
    OnboardingRevalidateView,
    OnboardingStateView,
    OnboardingTaskDetailView,
    OnboardingTaskListView,
)

urlpatterns = [
    path("state/", OnboardingStateView.as_view(), name="onboarding-state"),
    path("provision/", OnboardingProvisionView.as_view(), name="onboarding-provision"),
    path("revalidate/", OnboardingRevalidateView.as_view(), name="onboarding-revalidate"),
    path("tasks/", OnboardingTaskListView.as_view(), name="onboarding-task-list"),
    path("tasks/<str:key>/", OnboardingTaskDetailView.as_view(), name="onboarding-task-detail"),
    # Before the ``<int:pk>`` routes below it in intent, though not in fact:
    # "request" is not an integer, so the two cannot collide.
    path("go-live/request/", GoLiveRequestCreateView.as_view(), name="onboarding-go-live-request"),
    path("go-live/", GoLiveRequestListView.as_view(), name="onboarding-go-live-list"),
    path("go-live/<int:pk>/approve/", GoLiveApproveView.as_view(), name="onboarding-go-live-approve"),
    path("go-live/<int:pk>/reject/", GoLiveRejectView.as_view(), name="onboarding-go-live-reject"),
    # Platform-only, despite living on the onboarding surface: the school it
    # names is suspended, and a suspended tenant cannot authenticate at all, so
    # the target is a path segment rather than the asserted ?tenant=.
    path("reinstate/<slug:slug>/", OnboardingReinstateView.as_view(), name="onboarding-reinstate"),
]
