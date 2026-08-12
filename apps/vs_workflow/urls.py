"""URL routing for vs_workflow. Mount at: path("api/v1/workflow/", include("vs_workflow.urls"))"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from vs_workflow.views import (
    ApprovalDelegationViewSet, MySubmissionsView, PendingApprovalsView,
    ReverseActionView, TeamLoadView, WorkflowApproverGroupViewSet,
    WorkflowInstanceViewSet, WorkflowStageApproverOverrideViewSet,
    WorkflowTemplateViewSet,
)
router = DefaultRouter()
router.register(r"templates", WorkflowTemplateViewSet, basename="workflow-template")
router.register(r"approver-groups", WorkflowApproverGroupViewSet, basename="workflow-approver-group")
router.register(r"stage-approvers", WorkflowStageApproverOverrideViewSet, basename="workflow-stage-approver")
router.register(r"instances", WorkflowInstanceViewSet, basename="workflow-instance")
router.register(r"delegations", ApprovalDelegationViewSet, basename="workflow-delegation")
urlpatterns = [
    path("", include(router.urls)),
    path("actions/<str:action_id>/reverse/", ReverseActionView.as_view(), name="workflow-action-reverse"),
    path("dashboard/pending/",   PendingApprovalsView.as_view(), name="workflow-dashboard-pending"),
    path("dashboard/submitted/", MySubmissionsView.as_view(),    name="workflow-dashboard-submitted"),
    path("dashboard/team-load/", TeamLoadView.as_view(),         name="workflow-dashboard-team-load"),
]
