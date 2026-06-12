"""Internal staff: profiles, org nodes, positions, assignments, matrix reports.
"""
# views.py
# All views for the vs_users module in one flat file.
#
# Contents (in order):
#   AUTH       - LoginView, LogoutView, TokenRefreshView
#   INVITATION - ActivationPreviewView, ActivationView, InvitationResendView
#   PASSWORD   - PasswordChangeView, PasswordResetRequestView, PasswordResetConfirmView, AdminPasswordResetView
#   USERS      - UserAccountViewSet, UserEmailChangeView, UserSuspendView, UserReactivateView, UserUnlockView
#   SECURITY   - SessionViewSet, AuthAttemptViewSet, AccountLockoutViewSet, AuthEventLogViewSet

from __future__ import annotations
from django.db.models import Q
from rest_framework import status, viewsets, mixins
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from core.mixins import (
    XVSModelViewSetMixin,
    RetrieveModelMixin, CreateModelMixin, UpdateModelMixin,
)
from core.pagination import XVSPagination
from core.response import success_response, error_response
from ..models import (
    User, PlatformStaffProfile, OrgNode, Position,
    PositionAssignment, MatrixReport,
)
from ..serializers import (
    PlatformStaffProfileSerializer, PlatformStaffProfileListSerializer, OrgNodeSerializer, PositionSerializer,
    PositionAssignmentSerializer, MatrixReportSerializer,
    OrgTreeNodeSerializer,
)
from ..services.organogram import OrganogramService

from django.utils.dateparse import parse_date as _parse_date


# =============================================================================
# # PLATFORM STAFF PROFILE VIEWS
# =============================================================================

class PlatformStaffProfileViewSet(
    RetrieveModelMixin, CreateModelMixin, UpdateModelMixin,
    mixins.ListModelMixin, viewsets.GenericViewSet,
):
    """
    CX Staff HR / personal profiles. One profile per CX_STAFF user.

    GET    /platform-staff-profiles/         — list (slim, no payroll)
    POST   /platform-staff-profiles/         — create a profile for a CX staff user
    GET    /platform-staff-profiles/{id}/    — retrieve full profile
    PATCH  /platform-staff-profiles/{id}/    — update profile
    GET    /platform-staff-profiles/me/      — own profile (self-service)
    PATCH  /platform-staff-profiles/me/      — edit own profile (self-service)

    Sensitive payroll fields (bank_name, account_name, account_number) are
    gated by FLS — only callers holding platform.staff_payroll.view/manage
    can read/write them, regardless of endpoint.

    Permission matrix:
      list / retrieve:        platform.staff_profile.view
      create:                 platform.staff_profile.create
      update / partial_update: platform.staff_profile.update
      me:                     IsAuthenticatedAndActive (self-service)

    docstring-name: Staff profiles
    """

    pagination_class = XVSPagination

    def get_serializer_class(self):
        if self.action == 'list':
            return PlatformStaffProfileListSerializer
        return PlatformStaffProfileSerializer

    def get_permissions(self):
        if self.action == 'me':
            return [IsAuthenticatedAndActive()]
        action_permissions = {
            'list':           'platform.staff_profile.view',
            'retrieve':       'platform.staff_profile.view',
            'create':         'platform.staff_profile.create',
            'update':         'platform.staff_profile.update',
            'partial_update': 'platform.staff_profile.update',
        }
        self.rbac_permission = action_permissions.get(self.action, 'platform.staff_profile.view')
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            PlatformStaffProfile.objects
            .select_related('user', 'position', 'position__org_node')
            .filter(user__user_type=User.UserType.CX_STAFF)
            .order_by('-created_at')
        )

        if user := params.get('user'):
            # Look a profile up by its owner — powers Team Management's
            # "View Details", which knows the user id but not the profile id.
            qs = qs.filter(user_id=user)

        if org_node := params.get('org_node'):
            # The org node the person's seat belongs to. Accept PK or code.
            if str(org_node).isdigit():
                qs = qs.filter(position__org_node_id=org_node)
            else:
                qs = qs.filter(position__org_node__code__iexact=org_node)

        if position := params.get('position'):
            qs = qs.filter(position_id=position)

        if employment_status := params.get('employment_status'):
            qs = qs.filter(employment_status=employment_status)

        if employment_type := params.get('employment_type'):
            qs = qs.filter(employment_type=employment_type)

        if search := params.get('search'):
            if len(search) > 64:
                raise ValidationError({'search': 'Search query must be 64 characters or fewer.'})
            qs = qs.filter(
                Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(employee_id__icontains=search)
                | Q(job_title__icontains=search)
            )

        return qs

    @action(detail=False, methods=['get', 'patch'], url_path='me')
    def me(self, request):
        if getattr(request.user, 'user_type', None) != User.UserType.CX_STAFF:
            return error_response(
                message="Only CX staff have a platform staff profile.",
                status=status.HTTP_404_NOT_FOUND,
            )

        profile, _ = PlatformStaffProfile.objects.select_related(
            'user', 'position', 'position__org_node',
        ).get_or_create(user=request.user)

        if request.method.lower() == 'patch':
            ser = PlatformStaffProfileSerializer(
                profile, data=request.data, partial=True,
                context={'request': request},
            )
            ser.is_valid(raise_exception=True)
            ser.save()
            return success_response(message="Profile updated successfully.", data=ser.data)

        ser = PlatformStaffProfileSerializer(profile, context={'request': request})
        return success_response(message="Profile retrieved successfully.", data=ser.data)


# =============================================================================
# Organogram — Department / Position / PositionAssignment / MatrixReport
# =============================================================================

class OrgNodeViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    CX org nodes (hierarchical): Division → Department → Team.

    Read endpoints require platform.organogram.view; writes require
    platform.organogram.manage.

    docstring-name: Org nodes
    """

    serializer_class = OrgNodeSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve'}
        self.rbac_permission = (
            'platform.organogram.view' if self.action in read_actions
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            OrgNode.objects
            .select_related('parent', 'head_position')
            .prefetch_related('head_position__assignments__user')
            .order_by('-updated_at')
        )

        if (is_active := params.get('is_active')) is not None:
            qs = qs.filter(is_active=str(is_active).lower() in ('1', 'true', 'yes'))
        if kind := params.get('kind'):
            qs = qs.filter(kind=kind.upper())
        if parent := params.get('parent'):
            qs = qs.filter(parent_id=parent)
        if (roots := params.get('roots')) and str(roots).lower() in ('1', 'true', 'yes'):
            qs = qs.filter(parent__isnull=True)
        if search := params.get('search'):
            qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return qs


class PositionViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    Seats in the org chart. People are attached via position assignments.

    Read endpoints require platform.organogram.view; writes require
    platform.organogram.manage.

    docstring-name: Positions
    """

    serializer_class = PositionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve', 'tree', 'vacancies'}
        self.rbac_permission = (
            'platform.organogram.view' if self.action in read_actions
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            Position.objects
            .select_related('org_node', 'reports_to', 'default_role')
            .prefetch_related('assignments__user')
            .order_by('title')
        )

        if org_node := params.get('org_node'):
            qs = qs.filter(org_node_id=org_node)
        if reports_to := params.get('reports_to'):
            qs = qs.filter(reports_to_id=reports_to)
        if (is_active := params.get('is_active')) is not None:
            qs = qs.filter(is_active=str(is_active).lower() in ('1', 'true', 'yes'))
        if search := params.get('search'):
            qs = qs.filter(Q(title__icontains=search) | Q(code__icontains=search))
        return qs

    @action(detail=False, methods=['get'], url_path='tree')
    def tree(self, request):
        """Full position tree (solid reporting lines), nested from the roots."""
        root_id = request.query_params.get('root')
        root = None
        if root_id:
            root = Position.objects.filter(pk=root_id).select_related('org_node').first()
            if root is None:
                return error_response(
                    message="Root position not found.",
                    status=status.HTTP_404_NOT_FOUND,
                )
        nodes = OrganogramService.build_tree(root=root)
        ser = OrgTreeNodeSerializer(nodes, many=True, context={'request': request})
        return success_response(message="Organogram retrieved successfully.", data=ser.data)

    @action(detail=False, methods=['get'], url_path='vacancies')
    def vacancies(self, request):
        """Active positions with at least one open seat."""
        positions = OrganogramService.vacancies()
        ser = PositionSerializer(positions, many=True, context={'request': request})
        return success_response(message="Vacancies retrieved successfully.", data=ser.data)


class PositionAssignmentViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    Effective-dated assignments of users to positions (full history).

    Creating / closing assignments routes through OrganogramService so the
    "one current primary per user" invariant and department sync are honoured.

    docstring-name: Position assignments
    """

    serializer_class = PositionAssignmentSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve'}
        self.rbac_permission = (
            'platform.organogram.view' if self.action in read_actions
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            PositionAssignment.objects
            .select_related('user', 'position', 'position__org_node')
            .order_by('-start_date')
        )
        if user_id := params.get('user'):
            qs = qs.filter(user_id=user_id)
        if position_id := params.get('position'):
            qs = qs.filter(position_id=position_id)
        if (current := params.get('current')) is not None:
            if str(current).lower() in ('1', 'true', 'yes'):
                qs = qs.filter(end_date__isnull=True)
            else:
                qs = qs.filter(end_date__isnull=False)
        return qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            assignment = OrganogramService.assign_position(
                user=data['user'],
                position=data['position'],
                is_primary=data.get('is_primary', True),
                is_acting=data.get('is_acting', False),
                start_date=data.get('start_date'),
                assigned_by=request.user,
            )
        except ValueError as exc:
            payload = exc.args[0] if exc.args else {'message': 'Assignment failed.'}
            return error_response(
                message=payload.get('message', 'Assignment failed.'),
                error=payload, status=status.HTTP_400_BAD_REQUEST,
            )
        out = PositionAssignmentSerializer(assignment, context={'request': request})
        return success_response(
            message="Position assigned successfully.",
            data=out.data, status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, pk=None):
        """Ends an open assignment (sets end_date)."""
        assignment = self.get_object()
        end_date = _parse_date(request.data.get('end_date', '')) if request.data.get('end_date') else None
        OrganogramService.end_assignment(assignment, end_date=end_date)
        out = PositionAssignmentSerializer(assignment, context={'request': request})
        return success_response(message="Assignment closed.", data=out.data)


class MatrixReportViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """Dotted-line (matrix) reporting between positions.

    docstring-name: Matrix reports
    """

    serializer_class = MatrixReportSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve'}
        self.rbac_permission = (
            'platform.organogram.view' if self.action in read_actions
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            MatrixReport.objects
            .select_related('position', 'reports_to')
            .order_by('-created_at')
        )
        if position_id := params.get('position'):
            qs = qs.filter(position_id=position_id)
        if reports_to := params.get('reports_to'):
            qs = qs.filter(reports_to_id=reports_to)
        return qs

