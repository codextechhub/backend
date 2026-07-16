from __future__ import annotations

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action

from core.mixins import XVSModelViewSetMixin
from core.response import success_response, error_response
from core.pagination import XVSPagination

from .models import (
    ImpersonationSession,
)
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from vs_rbac.models import TenantUserRoleAssignment
from .serializers import (
    DashboardFilterSerializer,
    ImpersonationEndSerializer,
    ImpersonationSessionSerializer,
    ImpersonationStartSerializer,
    ImpersonationTargetSerializer,
    SchoolDashboardItemSerializer,
)


# Produce stable labels for impersonation audit summaries.
def _user_label(user) -> str:
    return user.full_name or user.email


# Write platform audit bookends for every proxy-session lifecycle change.
def _emit_proxy_lifecycle_event(*, action_type, actor, target, tenant, session, summary):
    """Write the durable, human-readable bookend for a proxy session."""
    from vs_audit.services import emit_audit_event

    emit_audit_event(
        module_key="PLATFORM",
        action_type=action_type,
        entity_type="ImpersonationSession",
        entity_id=str(session.pk),
        entity_label=_user_label(target),
        actor_user=actor,
        effective_user=target,
        tenant=tenant,
        impersonation_session=session,
        summary=summary,
        # Session status is stored in metadata so audit consumers can filter starts vs ends.
        metadata={"session_status": session.status},
    )


# Manage platform staff proxy sessions into tenant users.
class ImpersonationSessionViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    Basic CRUD + start/end actions.

    In many teams, you'd disable update/delete and only allow:
      - list/retrieve
      - start (create)
      - end (custom action)
    But leaving ModelViewSet keeps it simple for now.

    docstring-name: Impersonation sessions
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    # Stable ordering keeps pagination consistent between pages.
    queryset = ImpersonationSession.objects.order_by("-started_at", "-pk").select_related(
        "staff_user", "target_user", "tenant",
    ).prefetch_related(
        Prefetch(
            "staff_user__tenant_role_assignments",
            queryset=TenantUserRoleAssignment.objects.select_related("role").filter(
                assignment_status="ACTIVE",
            ),
            to_attr="_active_proxy_roles",
        ),
        Prefetch(
            "target_user__tenant_role_assignments",
            queryset=TenantUserRoleAssignment.objects.select_related("role").filter(
                assignment_status="ACTIVE",
            ),
            to_attr="_active_proxy_roles",
        ),
    )
    serializer_class = ImpersonationSessionSerializer
    pagination_class = XVSPagination
    # Lets a PLATFORM actor assert ?tenant=<school-slug> to start/list/end
    # impersonation sessions for that school tenant (see TenantJWTAuthentication).
    platform_cross_tenant_param = True

    def get_permissions(self):
        # Target search accepts any start permission; final scope is enforced in the queryset.
        if self.action == "targets":
            self.rbac_permission = [
                "platform.impersonation.start_all",
                "platform.impersonation.start_cx",
                "platform.impersonation.start_school",
            ]
        elif self.action == "start":
            # The required scope depends on WHO is being impersonated: the target
            # lives in the asserted tenant (request.tenant), so its kind decides
            # the key. Any-of — start_all always suffices; the narrow key covers
            # only its own tenant kind. request.tenant is bound by auth before
            # permission checks run.
            tenant = getattr(self.request, "tenant", None)
            # Starting a CX proxy and a school proxy are distinct RBAC capabilities.
            if getattr(tenant, "kind", None) == "PLATFORM":
                self.rbac_permission = [
                    "platform.impersonation.start_all",
                    "platform.impersonation.start_cx",
                ]
            else:
                self.rbac_permission = [
                    "platform.impersonation.start_all",
                    "platform.impersonation.start_school",
                ]
        else:
            self.rbac_permission = {
                # A starter must always be able to exit their own session.
                # Inside the action, start_* keys stay owner-only while the
                # dedicated end key is the admin kill switch for ANY session.
                "end": [
                    "platform.impersonation.end",
                    "platform.impersonation.start_all",
                    "platform.impersonation.start_cx",
                    "platform.impersonation.start_school",
                ],
                "list": "platform.impersonation.view",
                "retrieve": "platform.impersonation.view",
            }.get(self.action, "platform.impersonation.view")
        return super().get_permissions()

    # Search the users a platform actor may impersonate in the asserted tenant scope.
    @action(detail=False, methods=["get"], url_path="targets")
    def targets(self, request):
        """Search active users the original platform actor may proxy."""
        from vs_rbac.evaluator import get_effective_permissions
        from vs_rbac.permissions import is_vision_super_admin
        from vs_tenants.models import Tenant
        from vs_user.models import User

        actor = getattr(request, "actor_user", request.user)
        # Impersonation is always initiated by the original platform actor.
        if getattr(actor.tenant, "kind", None) != Tenant.Kind.PLATFORM:
            return error_response(
                message="Only platform staff may search proxy targets.",
                status=status.HTTP_403_FORBIDDEN,
            )
        query = request.query_params.get("search", "").strip()
        if len(query) < 2:
            return error_response(
                message="Enter at least 2 characters to search.",
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(query) > 64:
            return error_response(
                message="Search query must be 64 characters or fewer.",
                status=status.HTTP_400_BAD_REQUEST,
            )

        permission_keys = get_effective_permissions(actor, tenant=actor.tenant)
        # start_all widens both CX and school target pools; narrower keys only add their own kind.
        can_all = is_vision_super_admin(actor) or "platform.impersonation.start_all" in permission_keys
        can_cx = can_all or "platform.impersonation.start_cx" in permission_keys
        can_school = can_all or "platform.impersonation.start_school" in permission_keys

        # Start from an empty predicate so users with no start grant see no targets.
        eligible_kind = Q(pk__in=[])
        if can_cx:
            eligible_kind |= Q(tenant__kind=Tenant.Kind.PLATFORM)
        if can_school:
            eligible_kind |= ~Q(tenant__kind=Tenant.Kind.PLATFORM)

        terms = query.split()
        if len(terms) == 1:
            # A single value may be a first name, last name, or email fragment.
            term = terms[0]
            search_filter = (
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(email__icontains=term)
            )
        else:
            first_term = terms[0]
            last_term = " ".join(terms[1:])
            search_filter = (
                (
                    Q(first_name__icontains=first_term)
                    & Q(last_name__icontains=last_term)
                )
                | (
                    Q(last_name__icontains=first_term)
                    & Q(first_name__icontains=last_term)
                )
            )

        queryset = (
            User.objects.select_related("tenant__school_profile")
            .filter(
                eligible_kind,
                search_filter,
                is_active=True,
                status=User.Status.ACTIVE,
            )
            .exclude(pk=actor.pk)
            .order_by("first_name", "last_name", "email")
        )
        page = self.paginate_queryset(queryset)
        serializer = ImpersonationTargetSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def get_queryset(self):
        if self.action == "list":
            # The monitoring screen must never show abandoned sessions as
            # ACTIVE: expire idle/overdue rows before they are listed.
            from .services import sweep_stale_impersonations
            sweep_stale_impersonations()
        qs = super().get_queryset()
        tenant = getattr(self.request, "tenant", None)
        status_param = self.request.query_params.get("status")
        if tenant:
            # TenantJWTAuthentication binds platform-cross-tenant queries before list/retrieve.
            qs = qs.filter(tenant=tenant)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    # Start or switch a proxy session for the currently asserted tenant.
    @action(detail=False, methods=["post"], url_path="start")
    def start(self, request):
        """
        POST /impersonations/start/
        Payload: ImpersonationStartSerializer

        Creates an ACTIVE session and logs the action.
        """
        ser = ImpersonationStartSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        duration = data.get("duration_minutes")
        started_at = timezone.now()
        ends_at = (
            started_at + timezone.timedelta(minutes=duration)
            if duration is not None
            else None
        )

        with transaction.atomic():
            tenant = request.tenant
            actor = getattr(request, "actor_user", request.user)
            # Impersonation is a platform capability: only CX (PLATFORM-tenant)
            # staff may impersonate, regardless of which start key a role carries.
            if getattr(getattr(actor, "tenant", None), "kind", None) != "PLATFORM":
                return error_response(
                    message="Only platform staff may impersonate.",
                    status=status.HTTP_403_FORBIDDEN,
                )
            from vs_user.models import User
            # Lock the actor row so two simultaneous start/switch requests
            # cannot create concurrent ACTIVE sessions.
            actor = User.objects.select_for_update().get(pk=actor.pk)
            target = User.objects.filter(
                # Targets are pinned to the asserted tenant to prevent cross-tenant proxy jumps.
                pk=data["target_user"], tenant=tenant, is_active=True, status="ACTIVE",
            ).first()
            if target is None:
                return error_response(
                    message="Target user was not found in this tenant.",
                    status=status.HTTP_404_NOT_FOUND,
                )
            # Starting another target is an atomic switch. Validation happens
            # first, so a failed selection never disrupts the current proxy.
            active_sessions = list(ImpersonationSession.objects.filter(
                staff_user=actor, status="ACTIVE",
            ).select_related("target_user", "tenant"))
            ImpersonationSession.objects.filter(
                pk__in=[active.pk for active in active_sessions],
            ).update(status="ENDED", ended_at=started_at)
            for active in active_sessions:
                # Emit one audit end event per replaced session, even though the DB update was bulk.
                active.status = "ENDED"
                active.ended_at = started_at
                _emit_proxy_lifecycle_event(
                    action_type="IMPERSONATION_ENDED",
                    actor=actor,
                    target=active.target_user,
                    tenant=active.tenant,
                    session=active,
                    summary=(
                        f"{_user_label(actor)} ended the proxy session as "
                        f"{_user_label(active.target_user)} to proxy another user"
                    ),
                )
            session = ImpersonationSession.objects.create(
                # The new session is created after old sessions end, preserving a single active proxy.
                staff_user=actor,
                tenant=tenant,
                target_user=target,
                justification=data.get("justification") or "Started from proxy user menu.",
                started_at=started_at,
                ends_at=ends_at,
                status='ACTIVE',
            )
            _emit_proxy_lifecycle_event(
                action_type="IMPERSONATION_STARTED",
                actor=actor,
                target=target,
                tenant=tenant,
                session=session,
                summary=f"{_user_label(actor)} started a proxy session as {_user_label(target)}",
            )

            return success_response(
                message="Impersonation session started.",
                data=ImpersonationSessionSerializer(session).data,
                status=status.HTTP_201_CREATED,
            )

    # End a proxy session: owners exit their own; the end key is the admin
    # kill switch and may terminate ANY active session.
    @action(detail=False, methods=["post"], url_path="end")
    def end(self, request):
        """
        POST /impersonations/end/
        Payload: ImpersonationEndSerializer
        Ends an ACTIVE session and logs the action.
        """
        from vs_rbac.evaluator import get_effective_permissions
        from vs_rbac.permissions import is_vision_super_admin

        ser = ImpersonationEndSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        session_id = ser.validated_data["session_id"]

        actor = getattr(request, "actor_user", request.user)
        session = ImpersonationSession.objects.select_related(
            "staff_user", "target_user", "tenant",
        ).filter(id=session_id).first()
        if session is not None and session.staff_user_id != actor.pk:
            # start_* holders reached this action for self-exit only; without
            # the dedicated end key another actor's session stays a
            # non-enumerating 404.
            can_end_any = is_vision_super_admin(actor) or (
                "platform.impersonation.end"
                in get_effective_permissions(actor, tenant=actor.tenant)
            )
            if not can_end_any:
                session = None
        if not session:
            return error_response(message="Impersonation session not found.", status=status.HTTP_404_NOT_FOUND)
        if session.status != 'ACTIVE':
            return error_response(message="Impersonation session is not ACTIVE.")

        ended_by_owner = session.staff_user_id == actor.pk
        summary = (
            f"{_user_label(actor)} ended the proxy session as {_user_label(session.target_user)}"
            if ended_by_owner
            else (
                f"{_user_label(actor)} terminated {_user_label(session.staff_user)}'s "
                f"proxy session as {_user_label(session.target_user)}"
            )
        )
        with transaction.atomic():
            session.end()
            _emit_proxy_lifecycle_event(
                action_type="IMPERSONATION_ENDED",
                actor=actor,
                target=session.target_user,
                tenant=session.tenant,
                session=session,
                summary=summary,
            )

        return success_response(
            message="Impersonation session ended.",
            data=ImpersonationSessionSerializer(session).data,
        )

# Placeholder platform dashboard endpoint for future cross-module school health rows.
class DashboardViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /dashboard/
    A clean place to assemble data from multiple modules.

    For now it’s a stub that returns an empty list.
    You’ll implement it by querying School (Module 1) and joining:
      - latest ProvisioningEvent
      - latest ImportJobLog
      - suspension state from School model

    docstring-name: Admin dashboard
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.dashboard.view"
    serializer_class = SchoolDashboardItemSerializer

    def list(self, request, *args, **kwargs):
        # Validate dashboard filters now so the eventual implementation keeps the same contract.
        filter_ser = DashboardFilterSerializer(data=request.query_params)
        filter_ser.is_valid(raise_exception=True)

        # TODO: Build actual dashboard items here using School model.
        # Return list of dicts matching SchoolDashboardItemSerializer fields.
        items = []

        return success_response(
            message="Dashboard data retrieved.",
            data=self.serializer_class(items, many=True).data,
        )
