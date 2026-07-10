"""REST views for vs_workflow. See urls.py for the full routing table."""

from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from vs_rbac.permissions import user_has_rbac_permission

from vs_workflow.constants import (
    PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW,
    PERM_INSTANCE_SUBMIT, PERM_INSTANCE_VIEW, PERM_INSTANCE_CANCEL,
    PERM_ACTION_REVERSE,
    ApproverSource, OrganogramTarget,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowInstance, WorkflowStage, WorkflowStageAction,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.serializers import (
    ApprovalDelegationSerializer, ApproverPreviewRequestSerializer,
    CancelInstanceSerializer, ReverseActionSerializer,
    StageActionWriteSerializer, SubmitForApprovalSerializer,
    WorkflowInstanceDetailSerializer, WorkflowInstanceListSerializer,
    WorkflowTemplatePublishSerializer, WorkflowTemplateReadSerializer,
)
from vs_workflow.services import actions as actions_svc
from vs_workflow.services import submission as submission_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import resolve_approvers


# ── Helpers ───────────────────────────────────────────────────────────────────

# Apply school scope only when the request has one.
def _filter_by_school(qs, school):
    if school is not None:
        return qs.filter(school=school)
    return qs


# Apply branch scope only when the user is branch-scoped.
def _filter_by_branch(qs, branch):
    if branch is not None:
        return qs.filter(branch=branch)
    return qs


# Resolve school/branch context once for all workflow views.
class SchoolScopedMixin:
    def get_school(self):
        return getattr(self.request, "_cached_school", None)

    def get_branch(self):
        return getattr(self.request.user, "branch", None)


# ── Templates ────────────────────────────────────────────────────────────────

class WorkflowTemplateViewSet(
    SchoolScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet,
):
    """docstring-name: Workflow templates"""
    serializer_class = WorkflowTemplateReadSerializer

    def get_permissions(self):
        # Publishing templates requires manage rights; read endpoints use view rights.
        self.rbac_permission = PERM_TEMPLATE_MANAGE if self.action == "publish" else PERM_TEMPLATE_VIEW
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        # Templates are explicitly scoped; global manager context is not trusted here.
        qs = _filter_by_school(WorkflowTemplate.objects.all(), self.get_school())
        qs = _filter_by_branch(qs, self.get_branch())
        return qs.prefetch_related("stages", "routes")

    @action(detail=False, methods=["post"], url_path="preview-approvers")
    def preview_approvers(self, request):
        """Resolve the eligible approvers for an ad-hoc stage config + sample
        requester WITHOUT persisting anything. Powers the template builder's
        live "who would approve?" preview. Honours both approver sources and
        active delegations, exactly like activation-time resolution."""
        s = ApproverPreviewRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        requester = UserModel.objects.filter(pk=d["requester"]).first()
        if requester is None:
            return Response({"detail": "Requester not found."}, status=status.HTTP_404_NOT_FOUND)

        # Build a transient (unsaved) stage from the posted config.
        stage = WorkflowStage(
            approver_source=d["approver_source"],
            organogram_target=d.get("organogram_target", "") or "",
            organogram_levels=d.get("organogram_levels", 1) or 1,
            approver_permission_key=d.get("approver_permission_key", "") or "",
            approver_scope=d.get("approver_scope"),
        )
        if d["approver_source"] == ApproverSource.ORGANOGRAM and \
                d.get("organogram_target") == OrganogramTarget.SPECIFIC_POSITION:
            try:
                from vs_user.models import Position
                stage.organogram_position = Position.objects.filter(code=d["organogram_position_code"]).first()
            except ImportError:
                stage.organogram_position = None

        # Build a transient instance carrying just the context the resolver reads.
        instance = WorkflowInstance(
            requested_by=requester,
            school=getattr(requester, "school", None),
            branch=getattr(requester, "branch", None),
            document_type=d.get("document_type", "") or "",
        )

        eligible = resolve_approvers(stage, instance)

        def _u(user):
            if user is None:
                return None
            return {
                "id": str(user.pk),
                "full_name": getattr(user, "full_name", "") or user.get_username(),
                "email": getattr(user, "email", ""),
            }

        approvers = [{"user": _u(e.user), "on_behalf_of": _u(e.on_behalf_of)} for e in eligible]
        return Response({
            "approver_source": d["approver_source"],
            "organogram_target": d.get("organogram_target") or None,
            "count": len(approvers),
            "approvers": approvers,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="publish")
    def publish(self, request):
        p = WorkflowTemplatePublishSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        d = p.validated_data
        # Template publishing replaces stage/route configuration through the service layer.
        t = templates_svc.publish_template(
            school=self.get_school(),
            branch=self.get_branch(),
            document_type=d["document_type"], code=d["code"], name=d["name"],
            description=d.get("description", ""),
            notification_events=d.get("notification_events", {}),
            created_by=request.user,
            stages_payload=d["stages"], routes_payload=d.get("routes", []),
        )
        return Response(WorkflowTemplateReadSerializer(t).data, status=status.HTTP_201_CREATED)


# ── Instances ────────────────────────────────────────────────────────────────

class WorkflowInstanceViewSet(
    SchoolScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet,
):
    """docstring-name: Workflow instances"""
    def get_permissions(self):
        if self.action == "create":
            self.rbac_permission = PERM_INSTANCE_SUBMIT
        elif self.action == "cancel":
            self.rbac_permission = PERM_INSTANCE_CANCEL
        elif self.action in ("list", "retrieve"):
            self.rbac_permission = PERM_INSTANCE_VIEW
        else:
            # Actor-level actions are guarded by ownership/eligibility in the service layer.
            return [IsAuthenticatedAndActive()]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_class(self):
        return WorkflowInstanceDetailSerializer if self.action == "retrieve" else WorkflowInstanceListSerializer

    def get_queryset(self):
        # Instance lists are tenant-scoped before any user-supplied filters apply.
        qs = (_filter_by_school(WorkflowInstance.objects.all(), self.get_school())
              .select_related("template", "current_stage")
              .prefetch_related("stage_instances__stage", "stage_instances__actions",
                                "stage_instances__eligible_approvers", "audit_logs")
              .order_by("-updated_at", "-created_at"))
        p = self.request.query_params
        if p.get("document_type"): qs = qs.filter(document_type=p["document_type"])
        if p.get("status"):        qs = qs.filter(status=p["status"])
        if p.get("requested_by"):  qs = qs.filter(requested_by_id=p["requested_by"])
        if p.get("template_code"): qs = qs.filter(template__code=p["template_code"])
        return qs

    def create(self, request):
        p = SubmitForApprovalSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        d = p.validated_data
        try:
            ct = ContentType.objects.get(pk=d["content_type_id"])
            document = ct.model_class().objects.get(pk=d["object_id"])
        except Exception:
            return Response({
                "success": False,
                "message": "The referenced document was not found.",
                "error": {"code": "DOCUMENT_NOT_FOUND", "detail": {}},
            }, status=status.HTTP_404_NOT_FOUND)
        # Submission service validates the document handler, template, and initial routing.
        instance = submission_svc.submit_for_approval(
            document=document, requested_by=request.user,
            template_code=d.get("template_code") or None,
        )
        return Response(WorkflowInstanceDetailSerializer(instance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        instance = actions_svc.withdraw(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = actions_svc.resubmit(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        p = CancelInstanceSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.cancel(
            self.get_object().id, request.user, p.validated_data["reason"])
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"], url_path="actions")
    def record_action(self, request, pk=None):
        p = StageActionWriteSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.record_action(
            self.get_object().id, request.user,
            action=p.validated_data["action"],
            comment=p.validated_data.get("comment", ""),
        )
        return Response(WorkflowInstanceDetailSerializer(instance).data)


class ReverseActionView(SchoolScopedMixin, APIView):
    """docstring-name: Reverse an approval action"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = PERM_ACTION_REVERSE

    def post(self, request, action_id):
        p = ReverseActionSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        try:
            row = WorkflowStageAction.objects.select_related(
                "stage_instance__instance").get(pk=action_id)
        except WorkflowStageAction.DoesNotExist:
            raise NotFound("Action not found.")
        school = self.get_school()
        if school is not None and row.stage_instance.instance.school_id != school.pk:
            # Hide cross-school action existence behind the same 404.
            raise NotFound("Action not found.")
        reversal = actions_svc.reverse_action(action_id, request.user, p.validated_data["reason"])
        return Response({"reversal_action_id": str(reversal.id)})


# ── Dashboards ────────────────────────────────────────────────────────────────

class PendingApprovalsView(SchoolScopedMixin, APIView):
    """GET /workflow/dashboard/pending/ — instances where the user is eligible to act.

    docstring-name: My pending approvals
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        school = self.get_school()
        user = request.user
        # Start from approver snapshots so delegated approvals are included.
        snaps_qs = WorkflowStageApprover.objects.filter(
            user=user,
            stage_instance__status="ACTIVE",
            stage_instance__instance__status="IN_PROGRESS",
        )
        if school is not None:
            snaps_qs = snaps_qs.filter(stage_instance__instance__school=school)
        snaps = snaps_qs.select_related(
            "stage_instance__instance__template", "stage_instance__stage",
        ).order_by("-stage_instance__activated_at")
        already_acted = set(
            WorkflowStageAction.objects.filter(
                actor=user, reversed_at__isnull=True, is_reversal_of__isnull=True,
            ).values_list("stage_instance_id", "attempt"))
        results = []
        for snap in snaps:
            # Hide stages where the actor already voted in the current attempt.
            if (snap.stage_instance_id, snap.stage_instance.attempt) in already_acted:
                continue
            # Ignore stale approver snapshots from previous attempts.
            if snap.attempt != snap.stage_instance.attempt:
                continue
            inst = snap.stage_instance.instance
            results.append(WorkflowInstanceListSerializer(inst).data | {
                "awaiting_on_stage": snap.stage_instance.stage.label,
                "awaiting_since": snap.stage_instance.activated_at,
                "on_behalf_of": str(snap.on_behalf_of_id) if snap.on_behalf_of_id else None,
            })
        return Response({"results": results, "count": len(results)})


class MySubmissionsView(SchoolScopedMixin, APIView):
    """GET /workflow/dashboard/submitted/ — instances the user has submitted.

    docstring-name: My submissions
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        # Submitter dashboard is restricted to the caller's own submitted instances.
        qs = (_filter_by_school(WorkflowInstance.objects.all(), self.get_school())
              .filter(requested_by=request.user)
              .select_related("template", "current_stage")
              .order_by("-updated_at", "-created_at"))
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response(WorkflowInstanceListSerializer(qs, many=True).data)


class TeamLoadView(SchoolScopedMixin, APIView):
    """GET /workflow/dashboard/team-load/ — active instance counts by stage.

    docstring-name: Team approval load
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = PERM_INSTANCE_VIEW

    def get(self, request):
        school = self.get_school()
        # Count active stage instances by document type/stage for operational load.
        base = WorkflowStageInstance.objects.filter(status="ACTIVE")
        if school is not None:
            base = base.filter(instance__school=school)
        qs = (base
              .values("instance__document_type", "stage__code", "stage__label")
              .order_by("instance__document_type", "stage__code"))
        buckets = defaultdict(lambda: {"count": 0, "stage_label": None})
        for row in qs:
            key = (row["instance__document_type"], row["stage__code"])
            buckets[key]["count"] += 1
            buckets[key]["stage_label"] = row["stage__label"]
        return Response([
            {"document_type": dt, "stage_code": code,
             "stage_label": info["stage_label"], "active_count": info["count"]}
            for (dt, code), info in sorted(buckets.items())
        ])


# ── Delegations ───────────────────────────────────────────────────────────────

class ApprovalDelegationViewSet(SchoolScopedMixin, ModelViewSet):
    """docstring-name: Approval delegations"""
    serializer_class = ApprovalDelegationSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_queryset(self):
        user = self.request.user
        school = self.get_school()
        qs = _filter_by_school(ApprovalDelegation.objects.all(), school)
        if not user_has_rbac_permission(user, PERM_TEMPLATE_MANAGE, school=school):
            # Non-admin users can only see delegations they created or receive.
            qs = qs.filter(Q(delegator=user) | Q(delegate=user))
        return qs.order_by("-starts_at")

    def perform_create(self, serializer):
        # Delegations are always created by the current user within the active school scope.
        serializer.save(school=self.get_school(), delegator=self.request.user)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        delegation = self.get_object()
        school = self.get_school()
        if (delegation.delegator_id != request.user.pk and
                not user_has_rbac_permission(request.user, PERM_TEMPLATE_MANAGE, school=school)):
            return Response({
                "success": False,
                "message": "You do not have permission to revoke this delegation.",
                "error": {"code": "PERMISSION_DENIED", "detail": {}},
            }, status=status.HTTP_403_FORBIDDEN)
        # Revocation is timestamped instead of deleting the delegation record.
        delegation.revoked_at = timezone.now()
        delegation.save(update_fields=["revoked_at"])
        return Response(ApprovalDelegationSerializer(delegation).data)
