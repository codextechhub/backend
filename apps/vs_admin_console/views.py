from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    ImpersonationSession,
)
from .permissions import IsVisionStaff
from .serializers import (
    DashboardFilterSerializer,
    ImpersonationEndSerializer,
    ImpersonationSessionSerializer,
    ImpersonationStartSerializer,
    InstitutionDashboardItemSerializer,
)


class ImpersonationSessionViewSet(viewsets.ModelViewSet):
    """
    Basic CRUD + start/end actions.

    In many teams, you'd disable update/delete and only allow:
      - list/retrieve
      - start (create)
      - end (custom action)
    But leaving ModelViewSet keeps it simple for now.
    """
    permission_classes = [IsVisionStaff]
    queryset = ImpersonationSession.objects.select_related("staff_user", "target_user").order_by("-created_at")
    serializer_class = ImpersonationSessionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        institution_id = self.request.query_params.get("institution")
        status_param = self.request.query_params.get("status")
        if institution_id:
            qs = qs.filter(institution_id=institution_id)
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
            session = ImpersonationSession.objects.create(
                staff_user=request.user,
                institution_id=data["institution"],
                target_user_id=data["target_user"],
                justification=data["justification"],
                started_at=started_at,
                ends_at=ends_at,
                status='ACTIVE',
            )
            
            return Response(ImpersonationSessionSerializer(session).data, status=status.HTTP_201_CREATED)
        
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
        
        session = ImpersonationSession.objects.filter(id=session_id).first()
        if not session:
            return Response({"detail": "Impersonation session not found."}, status=status.HTTP_404_NOT_FOUND)
        if session.status != 'ACTIVE':
            return Response({"detail": "Impersonation session is not ACTIVE."}, status=status.HTTP_400_BAD_REQUEST)
        
        with transaction.atomic():
            session.end()

        return Response(ImpersonationSessionSerializer(session).data, status=status.HTTP_200_OK)
    
class DashboardViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /dashboard/
    A clean place to assemble data from multiple modules.

    For now it's a stub that returns an empty list.
    You’ll implement it by querying Institution (Module 1) and joining:
      - latest ProvisioningEvent
      - latest ImportJobLog
      - suspension state from Institution model
    """
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionDashboardItemSerializer
    
    def list(self, request, *args, **kwargs):
        # Validate query params (optional)
        filter_ser = DashboardFilterSerializer(data=request.query_params)
        filter_ser.is_valid(raise_exception=True)

        # TODO: Build actual dashboard items here using Institution model.
        # Return list of dicts matching InstitutionDashboardItemSerializer fields.
        items = []

        return Response(self.serializer_class(items, many=True).data, status=status.HTTP_200_OK)

            
        
    