from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action

from core.mixins import XVSModelViewSetMixin
from core.response import success_response, error_response

from .models import (
    ImpersonationSession,
)
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from .serializers import (
    DashboardFilterSerializer,
    ImpersonationEndSerializer,
    ImpersonationSessionSerializer,
    ImpersonationStartSerializer,
    SchoolDashboardItemSerializer,
)


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
    queryset = ImpersonationSession.objects.select_related("staff_user", "target_user", "tenant")
    serializer_class = ImpersonationSessionSerializer
    # Lets a PLATFORM actor assert ?tenant=<school-slug> to start/list/end
    # impersonation sessions for that school tenant (see TenantJWTAuthentication).
    platform_cross_tenant_param = True

    def get_permissions(self):
        if self.action == "start":
            # The required scope depends on WHO is being impersonated: the target
            # lives in the asserted tenant (request.tenant), so its kind decides
            # the key. Any-of — start_all always suffices; the narrow key covers
            # only its own tenant kind. request.tenant is bound by auth before
            # permission checks run.
            tenant = getattr(self.request, "tenant", None)
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
                "end": "platform.impersonation.end",
                "list": "platform.impersonation.view",
                "retrieve": "platform.impersonation.view",
            }.get(self.action, "platform.impersonation.view")
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        tenant = getattr(self.request, "tenant", None)
        status_param = self.request.query_params.get("status")
        if tenant:
            qs = qs.filter(tenant=tenant)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

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
        
        duration = data.get("duration_minutes", 30)
        started_at = timezone.now()
        ends_at = started_at + timezone.timedelta(minutes=duration)
        
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
            if ImpersonationSession.objects.filter(
                staff_user=actor, status="ACTIVE", ends_at__gt=started_at,
            ).exists():
                return error_response(message="End the existing impersonation session first.")
            from vs_user.models import User
            target = User.objects.filter(
                pk=data["target_user"], tenant=tenant, is_active=True, status="ACTIVE",
            ).first()
            if target is None:
                return error_response(
                    message="Target user was not found in this tenant.",
                    status=status.HTTP_404_NOT_FOUND,
                )
            session = ImpersonationSession.objects.create(
                staff_user=actor,
                tenant=tenant,
                target_user=target,
                justification=data["justification"],
                started_at=started_at,
                ends_at=ends_at,
                status='ACTIVE',
            )
            
            return success_response(
                message="Impersonation session started.",
                data=ImpersonationSessionSerializer(session).data,
                status=status.HTTP_201_CREATED,
            )
        
    @action(detail=False, methods=["post"], url_path="end")
    def end(self, request):
        """
        POST /impersonations/end/
        Payload: ImpersonationEndSerializer
        Ends an ACTIVE session and logs the action.
        """
        ser = ImpersonationEndSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        session_id = ser.validated_data["session_id"]
        
        actor = getattr(request, "actor_user", request.user)
        session = ImpersonationSession.objects.filter(id=session_id, staff_user=actor).first()
        if not session:
            return error_response(message="Impersonation session not found.", status=status.HTTP_404_NOT_FOUND)
        if session.status != 'ACTIVE':
            return error_response(message="Impersonation session is not ACTIVE.")

        with transaction.atomic():
            session.end()

        return success_response(
            message="Impersonation session ended.",
            data=ImpersonationSessionSerializer(session).data,
        )

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
        # Validate query params (optional)
        filter_ser = DashboardFilterSerializer(data=request.query_params)
        filter_ser.is_valid(raise_exception=True)

        # TODO: Build actual dashboard items here using School model.
        # Return list of dicts matching SchoolDashboardItemSerializer fields.
        items = []

        return success_response(
            message="Dashboard data retrieved.",
            data=self.serializer_class(items, many=True).data,
        )

            
        
    
