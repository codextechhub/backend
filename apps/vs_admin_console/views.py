from __future__ import annotations

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from core.mixins import XVSModelViewSetMixin
from core.response import success_response, error_response
from core.pagination import XVSPagination

from .models import (
    ImpersonationSession,
)
from .overview import console_overview
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


# Impersonation is initiated by either PLATFORM (CX) staff or a school actor.
# The actor's home tenant kind - never the asserted tenant - decides which
# permission namespace, target pool and audit module apply.
def is_platform_actor(actor) -> bool:
    """True when *actor* belongs to the PLATFORM (Codex) tenant."""
    from vs_tenants.models import Tenant

    return getattr(getattr(actor, "tenant", None), "kind", None) == Tenant.Kind.PLATFORM


# Write audit bookends for every proxy-session lifecycle change.
def _emit_proxy_lifecycle_event(*, action_type, actor, target, tenant, session, summary):
    """Write the durable, human-readable bookend for a proxy session."""
    from vs_audit.services import emit_audit_event

    emit_audit_event(
        # Scope the row to the surface that initiated it: a school-initiated
        # proxy is a SCHOOL event (already tenant-scoped to that school), so it
        # never lands in the platform-only audit stream.
        module_key="PLATFORM" if is_platform_actor(actor) else "SCHOOL",
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
        # ``__tenant`` on both users because the type label reads the tenant's
        # kind now - see ImpersonationSessionSerializer._staff_type_label -
        # and without the join that is one extra query per row, twice.
        "staff_user__tenant", "target_user__tenant", "tenant",
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
        # The actor's home tenant picks the namespace, and the two sets are never
        # unioned: school actors are authorised only by school.impersonation.*,
        # platform staff only by platform.impersonation.*.
        actor = getattr(self.request, "actor_user", None) or getattr(
            self.request, "user", None,
        )
        if is_platform_actor(actor):
            self.rbac_permission = self._platform_rbac_permission()
        else:
            self.rbac_permission = {
                # One start key covers the whole (own-tenant) target pool -
                # there is no tiering to do when reach stops at the tenant edge.
                "targets": "school.impersonation.start",
                "start": "school.impersonation.start",
                # A starter must always be able to exit their own session; the
                # dedicated end key is the school's kill switch for ANY session
                # in the tenant (mirrors the platform contract).
                "end": [
                    "school.impersonation.end",
                    "school.impersonation.start",
                ],
                "list": "school.impersonation.view",
                "retrieve": "school.impersonation.view",
            }.get(self.action, "school.impersonation.view")
        return super().get_permissions()

    def _platform_rbac_permission(self):
        """The tiered platform.impersonation.* matrix (CX staff only)."""
        # Target search accepts any start permission; final scope is enforced in the queryset.
        if self.action == "targets":
            return [
                "platform.impersonation.start_all",
                "platform.impersonation.start_cx",
                "platform.impersonation.start_school",
            ]
        if self.action == "start":
            # The target lives in the asserted tenant, so its kind picks the key.
            # Any-of: start_all always suffices, the narrow key covers its own kind.
            tenant = getattr(self.request, "tenant", None)
            # Starting a CX proxy and a school proxy are distinct RBAC capabilities.
            if getattr(tenant, "kind", None) == "PLATFORM":
                return [
                    "platform.impersonation.start_all",
                    "platform.impersonation.start_cx",
                ]
            return [
                "platform.impersonation.start_all",
                "platform.impersonation.start_school",
            ]
        return {
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

    # Search the users the actor may impersonate in the scope their tenant allows.
    @action(detail=False, methods=["get"], url_path="targets")
    def targets(self, request):
        """Search active users the original actor may proxy.

        Platform (CX) staff search the tiered cross-tenant pool; a school actor
        searches only their own tenant. Either way the pool is a *predicate*,
        never a caller-supplied filter, so scope cannot be widened by input.
        """
        from vs_rbac.evaluator import get_effective_permissions
        from vs_rbac.permissions import is_vision_super_admin
        from vs_tenants.models import Tenant
        from vs_user.models import User

        # Impersonation is always initiated by the original actor, never by the
        # effective (proxied) user - that would allow proxy chaining.
        actor = getattr(request, "actor_user", request.user)
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

        if is_platform_actor(actor):
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
        else:
            # School actor: every active user in their own tenant is eligible, self
            # excluded below. Pinned to actor.tenant_id, so no ?tenant= value and no
            # sibling school can widen the pool.
            eligible_kind = Q(tenant_id=actor.tenant_id)

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
            from vs_tenants.models import Tenant

            # Shares the tenant-row lock with transition_tenant_status, so a start and
            # a shutdown cannot interleave into a session outliving the shutdown.
            tenant = (
                Tenant.objects
                .select_for_update()
                .filter(
                    pk=request.tenant.pk,
                    status__in=Tenant.AUTHENTICABLE_STATUSES,
                )
                .first()
            )
            if tenant is None:
                return error_response(
                    message="Target user was not found in this tenant.",
                    status=status.HTTP_404_NOT_FOUND,
                )
            actor = getattr(request, "actor_user", request.user)
            # A school actor proxies only inside their own tenant. Re-asserted here so
            # the rule survives independently of the auth layer's view flag, and stays
            # a non-enumerating 404 rather than a 403 that confirms the tenant exists.
            if not is_platform_actor(actor) and tenant.pk != actor.tenant_id:
                return error_response(
                    message="Target user was not found in this tenant.",
                    status=status.HTTP_404_NOT_FOUND,
                )
            from vs_user.models import User
            # Lock the actor row so two simultaneous start/switch requests
            # cannot create concurrent ACTIVE sessions.
            actor = User.objects.select_for_update().get(pk=actor.pk)
            target = User.objects.filter(
                # Targets are pinned to the asserted tenant to prevent cross-tenant proxy jumps.
                pk=data["target_user"], tenant=tenant, is_active=True, status="ACTIVE",
            ).exclude(
                # Self-proxy would give a session one identity on both sides, defeating the
                # dual-identity audit trail.
                pk=actor.pk,
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
        sessions = ImpersonationSession.objects.select_related(
            "staff_user", "target_user", "tenant",
        )
        if not is_platform_actor(actor):
            # A school actor's kill switch stops at their own tenant edge; without it
            # school.impersonation.end terminates any session in the system by pk.
            # Platform staff keep the global switch, having tenant reach already.
            sessions = sessions.filter(tenant_id=actor.tenant_id)
        session = sessions.filter(id=session_id).first()
        if session is not None and session.staff_user_id != actor.pk:
            # The kill-switch key is namespace-matched to the actor's tenant. start_*
            # holders reach this action for self-exit only; another actor's session is
            # a non-enumerating 404 without the dedicated end key.
            kill_switch_key = (
                "platform.impersonation.end" if is_platform_actor(actor)
                else "school.impersonation.end"
            )
            can_end_any = is_vision_super_admin(actor) or (
                kill_switch_key
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


class ConsoleOverviewView(APIView):
    """
    GET /admin/dashboard/overview/

    Everything the console landing screen renders, in one response: school and
    team counts, the caller's task headline and next few tasks, their approval
    queue, returned submissions, unread notifications, open tickets and system
    posture.

    Replaces eight separate dashboard calls. Assembly and - importantly - the
    per-section permission gating live in ``overview.py``; a section the caller
    may not see is omitted rather than zeroed, so this is not a way to read a
    number they could not fetch directly.

    Permission: IsAuthenticatedAndActive. This is the landing screen every
    signed-in user gets; the sections inside carry their own keys.

    docstring-name: Console overview
    """

    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        return success_response(
            message="Overview retrieved successfully.",
            data=console_overview(request),
        )
