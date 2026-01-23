from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    AdminActionLog,
    FeatureFlag,
    ImpersonationSession,
    ImportJobLog,
    ProvisioningEvent,
)
from .permissions import IsVisionStaff, StaffReadOnlyOrSuperuserWrite
from .serializers import (
    AdminActionLogCreateSerializer,
    AdminActionLogSerializer,
    DashboardFilterSerializer,
    FeatureFlagSerializer,
    FeatureFlagUpsertSerializer,
    ImportJobLogSerializer,
    ImportRetrySerializer,
    ImpersonationEndSerializer,
    ImpersonationSessionSerializer,
    ImpersonationStartSerializer,
    InstitutionDashboardItemSerializer,
    ProvisioningEventSerializer,
    ProvisioningRetrySerializer,
)

class AdminActionLogViewSet(viewsets.ModelViewSet):
    """
    Basic CRUD for AdminActionLog.
    - actor is always request.user (not accepted from client)
    """
    permission_classes = [IsVisionStaff]
    queryset = AdminActionLog.objects.select_related("actor").order_by("-created_at")
    
    def get_serializer_class(self):
        if self.action in {'create', 'update', 'partial_update'}:
            return AdminActionLogCreateSerializer
        return AdminActionLogSerializer
    
    def perform_create(self, serializer):
        serializer.save(actor=self.request.user)
        
    #   simple filtering by institution/action/result
    def get_queryset(self):
        qs = super().get_queryset()
        institution_id = self.request.query_param.get("institution_id")
        action = self.request.query_param.get("action")
        result = self.request.query_param.get("result")
        
        if institution_id:
            qs = qs.filter(institution_id=institution_id)
        if action:
            qs = qs.filter(action=action)      
        if result:
            qs = qs.filter(result=result)
            
class FeatureFlagViewSet(viewsets.ModelViewSet):
    permission_classes = [IsVisionStaff]
    queryset =FeatureFlag.objects.select_related('updated_by').order_by('-updated_at')
    serializer_class = FeatureFlagSerializer
    
    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user)
        
    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
        
    def get_queryset(self):
        qs = super().get_queryset()
        institution_id= self.request.query_param.get("institution")
        key = self.request.query_param.get("key")
        if institution:
            qs = qs.filter(institution_id=institution_id)
        if key:
            qs = qs.filter(key=key)
        return qs
    
    @action(detail=False, methods=["post"], url_path="upsert")
    def upsert(self, request):
        """
        POST /feature-flags/upsert/
        Payload: FeatureFlagUpsertSerializer
        - Creates or updates (institution, key)
        """
        ser = FeatureFlagUpsertSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        
        flag, created = FeatureFlag.objects.get_or_create(
            institution_id=data["institution"],
            key=data["key"],
            defaults={
                "enabled": data["enabled"],
                "reason": data.get("reason", ""),
                "updated_by": request.user,
            },
        )
        if not created:
            flag.enabled = data["enabled"]
            flag.reason = data.get("reason", "")
            flag.updated_by = request.user
            flag.save(update_fields=["enabled", "reason", "updated_by", "updated_at"])

        return Response(FeatureFlagSerializer(flag).data, status=status.HTTP_200_OK)
    
class ProvisioningEventViewSet(viewsets.ModelViewSet):
    """
    CRUD for ProvisioningEvent records.
    In a real system, these are often created by workers, not by humans,
    but keeping it editable helps early-stage debugging.
    """
    permission_classes = [IsVisionStaff]
    queryset = ProvisioningEvent.objects.order_by("-created_at")
    serializer_class = ProvisioningEventSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        institution_id = self.request.query_params.get("institution")
        if institution_id:
            qs = qs.filter(institution_id=institution_id)
        return qs
    
    @action(detail=False, methods=["post"], url_path="retry")
    def retry_step(self, request):
        """
        POST /provisioning-events/retry/
        Payload: ProvisioningRetrySerializer

        This does NOT run the provisioning step.
        It only:
          1) performs basic rule checks
          2) writes an AdminActionLog entry
        Your actual worker trigger should be called in the marked place.
        """
        ser = ProvisioningRetrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        
        institution_id = data["institution"]
        step = data["step"]
        
        latest = (
            ProvisioningEvent.objects.filter(institution_id=institution_id, step=step)
            .order_by("-created_at")
            .first()
        )
        
        if latest and latest.status == 'RUNNING':
            return Response(
                {"detail": "Cannot retry while the step is still RUNNING."},
                status=status.HTTP_400_BAD_REQUEST,
            )
            
        AdminActionLog.objects.create(
            actor=request.user,
            institution_id=institution_id,
            action='PROVISIONING_RETRY',
            result='SUCCESS',
            reason=data['reason'],
            metadata={'step': step},
        )
        
        return Response({"detail": "Retry requested (logged)."}, status=status.HTTP_200_OK)
    
class ImportJobLogViewSet(viewsets.ModelViewSet):
    permission_classes = [IsVisionStaff]
    queryset = ImportJobLog.objects.order_by("-created_at")
    serializer_class = ImportJobLogSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        institution_id = self.request.query_params.get("institution")
        job_type = self.request.query_params.get("job_type")
        if institution_id:
            qs = qs.filter(institution_id=institution_id)
        if job_type:
            qs = qs.filter(job_type=job_type)
        return qs

    @action(detail=False, methods=["post"], url_path="retry")
    def retry_job(self, request):
        """
        POST /import-job-logs/retry/
        Payload: ImportRetrySerializer

        Same approach as provisioning retry:
        - do basic checks
        - log action
        - call your import engine trigger where indicated
        """
        ser = ImportRetrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        
        institution_id = data["institution"]
        job_type = data["job_type"]
        
        latest = (
            ImportJobLog.objects.filter(institution_id=institution_id, job_type=job_type)
            .order_by("-created_at")
            .first()
        )
        
        if latest and latest.status == 'RUNNING':
            return Response(
                {"detail": "Cannot retry while the job is still RUNNING."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        AdminActionLog.objects.create(
            actor=request.user,
            institution_id=institution_id,
            action='IMPORT_RETRY',
            result='SUCCESS',
            reason=data['reason'],
            metadata={'job_type': job_type},
        )
        
        return Response({"detail": "Retry requested (logged)."}, status=status.HTTP_200_OK)
    
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
            
            AdminActionLog.objects.create(
                actor=request.user,
                institution_id=data["institution"],
                action='IMPERSONATION_START',
                result='SUCCESS',
                reason=data["justification"],
                metadata={'target_user_id': data["target_user"], 'duration_minutes': duration},
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
            
        AdminActionLog.objects.create(
            actor=request.user,
            institution_id=session.institution_id,
            action='IMPERSONATION_END',
            result='SUCCESS',
            reason='Ended impersonation session',
            metadata={'session_id': session_id, 'target_user_id': session.target_user_id},
        )
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

            
        
    