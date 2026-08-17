"""The nine onboarding endpoints.

Three things are true of every view here and none of them is optional.

**It declares ``pending_tenant_surface``.** This module is the only surface a
school that has not gone live can reach, and membership is an explicit
attribute that defaults to closed (FR-012). A view here that forgets it locks
the school out of the one screen it needs.

**It scopes its queryset on ``request.tenant``.** Holding a permission key and
being scoped to a row are separate questions, and the second is answered here.
Another tenant's row answers 404, never 403, so tenant identifiers cannot be
enumerated.

**It computes nothing.** Readiness, counts and blockers come from the services;
a view that worked them out for itself would eventually disagree with the gate.
"""
from __future__ import annotations

from rest_framework import generics
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .constants import (
    ONBOARDING_EXPIRY_DAYS,
    PERM_GO_LIVE_APPROVE,
    PERM_GO_LIVE_REJECT,
    PERM_GO_LIVE_SUBMIT,
    PERM_GO_LIVE_VIEW,
    PERM_PROGRESS_CREATE,
    PERM_PROGRESS_REACTIVATE,
    PERM_PROGRESS_VIEW,
    PERM_TASK_UPDATE,
    TaskStatus,
)
from .exceptions import OnboardingNotProvisioned
from .models import GoLiveRequest, OnboardingProgress, OnboardingTask
from .serializers import (
    GoLiveRejectSerializer,
    GoLiveRequestSerializer,
    GoLiveSubmitSerializer,
    OnboardingTaskSerializer,
    OnboardingTaskTransitionSerializer,
)
from .services import go_live as go_live_service
from .services.lifecycle import expiry_outlook, reinstate_school
from .services.provisioning import provision_onboarding
from .services.readiness import ReadinessService
from .services.scoping import rows_for
from .services.tasks import transition_task


class OnboardingViewMixin:
    """Shared wiring for every view in this module."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    # FR-012. Without this the school that most needs these endpoints, the one
    # that is still PENDING, is refused all nine of them.
    pending_tenant_surface = True

    @property
    def tenant(self):
        return getattr(self.request, "tenant", None)


def _progress_or_404(tenant) -> OnboardingProgress:
    """The tenant's progress row, or the refusal that says it was never set up.

    404 with a code of its own rather than an empty success payload: a client
    has to be able to tell "this school has done nothing yet" from "this school
    was never provisioned", because only one of them is somebody's bug.
    """
    progress = rows_for(OnboardingProgress, tenant).first()
    if progress is None:
        raise OnboardingNotProvisioned()
    return progress


def _state_payload(tenant, progress, tasks) -> dict:
    """Everything the control room renders, from rows already in memory.

    Counts and ``go_live_blocked`` are computed here and stored nowhere: a
    stored count is a cache with no invalidation, and this one would be wrong
    the first time a task moved.

    ``expiry`` is the one block this view does *not* work out for itself. It
    comes from :func:`~schools.vs_onboarding.services.lifecycle.expiry_outlook`,
    the same function the sweep warns and expires from, so a countdown on the
    school's screen and the job that acts on it cannot disagree. It costs no
    query: every column it reads is on the tenant the request already carries.
    """
    done = sum(1 for task in tasks if task.status == TaskStatus.DONE)
    skipped = sum(1 for task in tasks if task.status == TaskStatus.SKIPPED)
    blocking = [
        task.key for task in tasks
        if task.is_required and task.status != TaskStatus.DONE
    ]
    return {
        "readiness_state": progress.readiness_state,
        "go_live_at": progress.go_live_at,
        "last_validation_at": progress.last_validation_at,
        "tasks": OnboardingTaskSerializer(tasks, many=True).data,
        "counts": {
            "done": done,
            "skipped": skipped,
            "remaining": len(tasks) - done - skipped,
            "total": len(tasks),
        },
        "go_live_blocked": bool(blocking),
        "blocking_tasks": blocking,
        "expiry": expiry_outlook(tenant),
    }


class OnboardingStateView(OnboardingViewMixin, APIView):
    """GET /v1/onboarding/state/ - FR-002.

    Two queries: one for the progress row, one for the tasks. Nothing per task,
    which is why the payload is built from the list in memory rather than by
    asking each row about itself.

    docstring-name: Onboarding control room state
    """

    rbac_permission = PERM_PROGRESS_VIEW

    def get(self, request):
        tenant = request.tenant
        progress = _progress_or_404(tenant)
        tasks = list(rows_for(OnboardingTask, tenant).order_by("order_index"))
        return success_response(
            "Onboarding state retrieved.", _state_payload(tenant, progress, tasks),
        )


class OnboardingProvisionView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/provision/ - FR-001.

    Idempotent: re-running tops up catalog entries that are missing and leaves
    every existing row exactly as it was, status included.

    docstring-name: Provision onboarding state
    """

    rbac_permission = PERM_PROGRESS_CREATE

    def post(self, request):
        tenant = request.tenant
        progress = provision_onboarding(tenant, actor=request.user)
        tasks = list(rows_for(OnboardingTask, tenant).order_by("order_index"))
        return success_response(
            "Onboarding provisioned.",
            _state_payload(tenant, progress, tasks),
        )


class OnboardingRevalidateView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/revalidate/ - FR-004.

    Forces the one authority to run again and stamps ``last_validation_at``,
    even when nothing changed.

    docstring-name: Recompute go-live readiness
    """

    rbac_permission = PERM_PROGRESS_VIEW

    def post(self, request):
        tenant = request.tenant
        _progress_or_404(tenant)
        progress = ReadinessService.evaluate(tenant, actor=request.user)
        tasks = list(rows_for(OnboardingTask, tenant).order_by("order_index"))
        return success_response(
            "Readiness recomputed.", _state_payload(tenant, progress, tasks),
        )


class OnboardingTaskListView(OnboardingViewMixin, APIView):
    """GET /v1/onboarding/tasks/ - the tenant's tasks in catalog order.

    Deliberately unpaginated: the catalog is eight entries long and a school
    reads its whole checklist at once or not at all.

    docstring-name: List onboarding tasks
    """

    rbac_permission = PERM_PROGRESS_VIEW

    def get(self, request):
        tasks = rows_for(OnboardingTask, request.tenant).order_by("order_index")
        return success_response(
            "Onboarding tasks retrieved.",
            OnboardingTaskSerializer(tasks, many=True).data,
        )


class OnboardingTaskDetailView(OnboardingViewMixin, APIView):
    """PATCH /v1/onboarding/tasks/<key>/ - FR-003.

    Addressed by catalog key, not by database id: the key is stable across
    environments and unique per tenant, so a re-provision cannot invalidate a
    bookmarked URL and the client never has to hold an id.

    docstring-name: Transition an onboarding task
    """

    rbac_permission = PERM_TASK_UPDATE

    def patch(self, request, key: str):
        serializer = OnboardingTaskTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = transition_task(
            request.tenant,
            key,
            serializer.validated_data["status"],
            actor=request.user,
        )
        return success_response(
            "Onboarding task updated.", OnboardingTaskSerializer(task).data,
        )


class GoLiveRequestCreateView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/go-live/request/ - FR-007.

    docstring-name: Submit a go-live request
    """

    rbac_permission = PERM_GO_LIVE_SUBMIT

    def post(self, request):
        serializer = GoLiveSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        go_live_request = go_live_service.submit_go_live(
            request.tenant,
            actor=request.user,
            preferred_go_live_at=serializer.validated_data["preferred_go_live_at"],
            note=serializer.validated_data.get("note", ""),
            acknowledged=serializer.validated_data.get("acknowledged", False),
        )
        return success_response(
            "Go-live request submitted.",
            GoLiveRequestSerializer(go_live_request).data,
            status=201,
        )


class GoLiveRequestListView(OnboardingViewMixin, generics.ListAPIView):
    """GET /v1/onboarding/go-live/ - current and historical requests.

    Paginated and newest first, because this is the one list in the module that
    grows without bound: a school that has been rejected four times has four
    rows plus the one it is waiting on.

    docstring-name: List go-live requests
    """

    rbac_permission = PERM_GO_LIVE_VIEW
    serializer_class = GoLiveRequestSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        return (
            rows_for(GoLiveRequest, getattr(self.request, "tenant", None))
            .select_related("requested_by", "reviewed_by")
            .order_by("-created_at")
        )


class GoLiveApproveView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/go-live/<id>/approve/ - FR-008 then FR-009.

    ``platform_cross_tenant_param`` is what lets a Codex reviewer assert the
    school's ``?tenant=`` slug. It is set on this view and on reject, and on no
    other view in the module: a school-tenant caller reaching this endpoint for
    their own school is still refused, because the key belongs to platform
    roles.

    docstring-name: Approve a go-live request
    """

    rbac_permission = PERM_GO_LIVE_APPROVE
    platform_cross_tenant_param = True

    def post(self, request, pk: int):
        go_live_request = go_live_service.approve_go_live(
            request.tenant, pk, actor=request.user,
        )
        return success_response(
            "Go-live request approved and the school is now live.",
            GoLiveRequestSerializer(go_live_request).data,
        )


class OnboardingReinstateView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/reinstate/<slug>/ - undo an onboarding expiry.

    The school is named in the path and not in ``?tenant=``, and that is the
    one thing about this endpoint worth reading twice. A SUSPENDED tenant is
    not authenticable, so ``?tenant=<suspended slug>`` answers 404 to
    everybody, platform staff included: an endpoint addressed that way could
    never be called for the only schools it exists to serve. Platform staff
    therefore assert their own tenant and name the school here.

    Two gates, not one. The permission key is granted to platform roles only,
    and the caller's asserted tenant must be the platform tenant, so a school
    admin who somehow held the key still cannot reinstate anybody - including
    themselves.

    docstring-name: Reinstate a suspended school
    """

    rbac_permission = PERM_PROGRESS_REACTIVATE

    def post(self, request, slug: str):
        from rest_framework.exceptions import NotFound, PermissionDenied

        from vs_tenants.models import Tenant

        if getattr(request.tenant, "kind", None) != Tenant.Kind.PLATFORM:
            raise PermissionDenied(
                "Only platform staff may reinstate a suspended school.",
            )

        target = Tenant.objects.filter(
            slug=(slug or "").strip().lower(), kind=Tenant.Kind.SCHOOL,
        ).first()
        if target is None:
            raise NotFound("No school matches this identifier.")

        tenant = reinstate_school(target, actor=request.user)
        return success_response(
            "The school has been returned to onboarding.",
            {
                "tenant": tenant.slug,
                "status": tenant.status,
                "pending_since": tenant.pending_since,
                "expires_in_days": ONBOARDING_EXPIRY_DAYS,
            },
        )


class GoLiveRejectView(OnboardingViewMixin, APIView):
    """POST /v1/onboarding/go-live/<id>/reject/ - FR-008.

    docstring-name: Reject a go-live request
    """

    rbac_permission = PERM_GO_LIVE_REJECT
    platform_cross_tenant_param = True

    def post(self, request, pk: int):
        serializer = GoLiveRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        go_live_request = go_live_service.reject_go_live(
            request.tenant,
            pk,
            actor=request.user,
            rejection_reason=serializer.validated_data.get("rejection_reason", ""),
        )
        return success_response(
            "Go-live request rejected.",
            GoLiveRequestSerializer(go_live_request).data,
        )
