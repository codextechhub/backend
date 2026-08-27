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
from django.db.models import Count, Prefetch, Q
from rest_framework import status, viewsets, mixins
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from vs_rbac.permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    HasRBACPermission,
    is_vision_super_admin,
)
from vs_rbac.evaluator import has_permission
from vs_tenants.models import Tenant
from core.media import signed_url
from core.mixins import (
    XVSModelViewSetMixin,
    RetrieveModelMixin, CreateModelMixin, UpdateModelMixin,
)
from core.pagination import XVSPagination
from core.response import success_response, error_response
from ..models import (
    PlatformStaffProfile, OrgNode, Position,
    PositionAssignment, MatrixReport,
)
from ..serializers import (
    PlatformStaffProfileSerializer, PlatformStaffProfileBriefSerializer,
    PlatformStaffProfileListSerializer, OrgNodeSerializer, PositionSerializer,
    PositionAssignmentSerializer, MatrixReportSerializer,
    OrgTreeNodeSerializer, OrganogramCurrentAssignmentSerializer,
)
from ..services.organogram import OrganogramService

from django.utils.dateparse import parse_date as _parse_date


# =============================================================================
# Who may see an organogram row
# =============================================================================
#
# Every table in this file is CX-internal by construction, not by convention:
# ``PlatformStaffProfile.clean`` and ``PositionAssignment.clean`` both refuse a
# user who is not on a PLATFORM tenant, ``OrganogramService.assign_position``
# refuses the same, and the write serializers declare
# ``queryset=User.objects.filter(tenant__kind=PLATFORM)``. There is no such
# thing as a school's organogram row, so there is no legitimate tenant caller
# for any of these surfaces.
#
# That was already asserted on the chart reads, which pair
# ``IsVisionStaff`` - a question about the caller's tenant kind - with
# authentication. It was NOT asserted anywhere the only gate was an RBAC key.
# Those surfaces were safe because ``platform.organogram.*`` and
# ``platform.staff_profile.*`` are ``PermissionScope.PLATFORM`` and ad41a03
# refuses such a key to a tenant role, which is a real guard and stays. But it
# is a statement about who holds the key, and this codebase has now twice
# found that argument false in practice: 02c42e6 (go-live approve/reject) and
# 677b469 / 297814b (the six account actions). It also has one live hole of its
# own: ``is_vision_super_admin`` asks only for an ACTIVE assignment to a role
# keyed ``xvs_super_admin`` *in the caller's own tenant*, and never asks that
# the tenant be PLATFORM-kind - and ``HasRBACPermission`` returns True for such
# a caller without consulting the key at all.
#
# So the tenant-kind question is now asked directly wherever a key was the only
# gate, and the one table that carries a tenant dimension - assignments, via
# ``user.tenant`` - is confined by the query as well. Two independent answers,
# neither leaning on the other.


def scope_assignments_to_caller(queryset, request):
    """Confine a ``PositionAssignment`` read to the caller's own tenant.

    A platform-tenant caller is returned untouched: reading the whole CX chart
    is the point of the console, and every row in the table is theirs anyway.

    Everyone else is confined to assignments held by users of their own tenant,
    which is the empty set today - the model forbids assigning anyone else - and
    that is the correct answer rather than a coincidence. It means an escalated
    tenant caller who gets past the gate is handed nothing instead of the full
    CX reporting line: who reports to whom, who is acting in a vacant seat, and
    when each tenure started and ended.

    The gate is the caller's *home* tenant kind, the discriminator 1da5c2a and
    677b469 both use. ``?tenant=`` cannot move it (a non-platform caller cannot
    assert another tenant at all), and under impersonation ``request.user`` is
    the effective user, so a CX staffer proxied into a school stays confined to
    the school - which is what being proxied means.
    """
    user = getattr(request, 'user', None)
    home = getattr(user, 'tenant', None)
    if getattr(home, 'kind', None) == Tenant.Kind.PLATFORM:
        return queryset
    tenant = getattr(request, 'tenant', None) or home
    if tenant is None:
        # Unreachable today: ``User.tenant`` is non-null and every
        # authenticated request carries one. It fails closed so that it stays
        # unreachable if that ever changes.
        return queryset.none()
    return queryset.filter(user__tenant=tenant)


def filter_by_id(queryset, **lookups):
    """Apply pk-valued query-parameter filters, safely.

    The values come from the caller, and ``qs.filter(user_id='abc')`` raises
    ``ValueError`` inside the ORM, which DRF renders as a 500. 677b469 settled
    what a malformed reference means: it is one of the ways an id can fail to
    name a row, not a server error. Nothing has the id ``'abc'``, so the honest
    answer is an empty page.

    Every caller applies this *after* the queryset has been confined, so a
    filter can only narrow what the caller may already see.
    """
    for field, raw in lookups.items():
        value = str(raw).strip()
        if not value.isdigit() or int(value) > 9_223_372_036_854_775_807:
            return queryset.none()
        queryset = queryset.filter(**{field: int(value)})
    return queryset


# =============================================================================
# # PLATFORM STAFF PROFILE VIEWS
# =============================================================================

class PlatformStaffProfileViewSet(
    RetrieveModelMixin, CreateModelMixin, UpdateModelMixin,
    mixins.ListModelMixin, viewsets.GenericViewSet,
):
    """
    Platform staff HR / personal profiles. One profile per platform user.

    GET    /platform-staff-profiles/         - list (slim, no payroll)
    POST   /platform-staff-profiles/         - create a profile for a CX staff user
    GET    /platform-staff-profiles/{id}/    - retrieve brief or authorised full profile
    PATCH  /platform-staff-profiles/{id}/    - update profile
    GET    /platform-staff-profiles/me/      - own profile (self-service)
    PATCH  /platform-staff-profiles/me/      - edit own profile (self-service)

    Sensitive payroll fields (bank_name, account_name, account_number) are
    gated by FLS - only callers holding platform.staff_payroll.view/manage
    can read/write them, regardless of endpoint.

    Permission matrix:
      list:                   any active user, current-tenant staff only
      retrieve:               any active user; brief unless owner or authorised
      create:                 platform staff + platform.staff_profile.create
      update / partial_update: platform staff + platform.staff_profile.update
      me:                     IsAuthenticatedAndActive (self-service)

    The reads are open to any authenticated caller because the queryset answers
    the question for them: it is filtered to the caller's own tenant, and no
    profile can belong to a tenant user, so a school caller gets an empty list
    and a 404 on any id. The writes are not open in the same way - ``create``
    resolves its target through the serializer's own PLATFORM-only queryset
    rather than through this one - so they ask the tenant-kind question
    directly instead of resting on the scope of the key.

    docstring-name: Staff profiles
    """

    pagination_class = XVSPagination

    def get_serializer_class(self):
        if self.action == 'list':
            return PlatformStaffProfileListSerializer
        return PlatformStaffProfileSerializer

    def get_permissions(self):
        if self.action in ('me', 'photos'):
            return [IsAuthenticatedAndActive()]
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticatedAndActive()]
        action_permissions = {
            'create':         'platform.staff_profile.create',
            'update':         'platform.staff_profile.update',
            'partial_update': 'platform.staff_profile.update',
        }
        self.rbac_permission = action_permissions.get(self.action, 'platform.staff_profile.view')
        return [IsAuthenticatedAndActive(), IsVisionStaff(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        tenant = (
            getattr(self.request, 'tenant', None)
            or getattr(self.request.user, 'tenant', None)
        )
        qs = (
            PlatformStaffProfile.objects
            .select_related(
                'user', 'position', 'position__org_node',
                'position__org_node__parent',
                'position__org_node__parent__parent',
                'position__reports_to',
            )
            .filter(
                user__tenant=tenant,
            )
            .order_by('-created_at')
        )

        if user := params.get('user'):
            # Look a profile up by its owner - powers Team Management's
            # "View Details", which knows the user id but not the profile id.
            # Applied after the tenant clause above, so it narrows.
            qs = filter_by_id(qs, user_id=user)

        if org_node := params.get('org_node'):
            # The org node the person's seat belongs to. Accept PK or code.
            if str(org_node).isdigit():
                qs = qs.filter(position__org_node_id=org_node)
            else:
                qs = qs.filter(position__org_node__code__iexact=org_node)

        if position := params.get('position'):
            qs = filter_by_id(qs, position_id=position)

        if employment_status := params.get('employment_status'):
            qs = qs.filter(employment_status=employment_status)

        if employment_type := params.get('employment_type'):
            qs = qs.filter(employment_type=employment_type)

        if search := params.get('search'):
            if len(search) > 64:
                raise ValidationError({'search': 'Search query must be 64 characters or fewer.'})
            # Apply each word independently so a natural full-name query such
            # as "Ada Lovelace" can match across first_name + last_name. Every
            # word must match at least one chart-safe searchable field.
            for term in search.split():
                qs = qs.filter(
                    Q(user__first_name__icontains=term)
                    | Q(user__last_name__icontains=term)
                    | Q(user__email__icontains=term)
                    | Q(employee_id__icontains=term)
                    | Q(job_title__icontains=term)
                )

        return qs

    def retrieve(self, request, *args, **kwargs):
        profile = self.get_object()
        tenant = getattr(request, 'tenant', None) or request.user.tenant
        can_view_full = (
            profile.user_id == request.user.id
            or is_vision_super_admin(request.user)
            or has_permission(
                request.user,
                'platform.staff_profile.view',
                tenant=tenant,
                branch=getattr(request, 'branch', None),
            )
        )
        serializer_class = (
            PlatformStaffProfileSerializer
            if can_view_full
            else PlatformStaffProfileBriefSerializer
        )
        serializer = serializer_class(profile, context=self.get_serializer_context())
        return success_response(
            message="Staff profile retrieved successfully.",
            data=serializer.data,
        )

    @action(detail=False, methods=['get', 'patch'], url_path='me')
    def me(self, request):
        # Tenant-kind gate: only platform-tenant users have a staff profile.
        if getattr(getattr(request.user, 'tenant', None), 'kind', None) != Tenant.Kind.PLATFORM:
            return error_response(
                message="Only platform staff have a platform staff profile.",
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

    @action(detail=False, methods=['get'], url_path='photos')
    def photos(self, request):
        """Map of user_id → absolute profile-photo URL for staff who have one.

        The single source of truth for avatars across the whole console: the
        client fetches this once, caches it, and resolves any user's photo by
        id - so individual serializers don't each need to carry the photo.
        Any authenticated user may read it (avatars are low-sensitivity; the
        image bytes themselves stay auth-gated by MediaView). Absolute URLs are
        required because /media/ sits outside the API's /v1 prefix.
        """
        tenant = getattr(request, 'tenant', None) or request.user.tenant
        rows = (
            PlatformStaffProfile.objects
            .filter(user__tenant=tenant, profile_photo__isnull=False)
            .exclude(profile_photo='')
            .only('user_id', 'profile_photo')
        )
        mapping = {
            str(p.user_id): signed_url(p.profile_photo.name, absolute_for=request)
            for p in rows
        }
        return success_response(message="Staff photos retrieved successfully.", data=mapping)


# =============================================================================
# Organogram - Department / Position / PositionAssignment / MatrixReport
# =============================================================================

class OrgNodeViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    CX org nodes (hierarchical): Division → Department → Team.

    Active platform employees may read the organisational structure. Writes
    require platform staff plus platform.organogram.manage.

    This table has no tenant column - the CX org tree belongs to CX and to
    nobody else - so there is nothing for a queryset to narrow. The boundary
    can only be the caller's tenant kind, which the reads already asserted and
    the writes now assert too.

    docstring-name: Org nodes
    """

    serializer_class = OrgNodeSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve'}
        if self.action in read_actions:
            return [IsAuthenticatedAndActive(), IsVisionStaff()]
        self.rbac_permission = 'platform.organogram.manage'
        return [IsAuthenticatedAndActive(), IsVisionStaff(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        # Prefetch each head seat's CURRENT holders once (primary first) and
        # annotate the child count, so the serializer's `head`/`children_count`
        # cost no per-row queries - the list was N+1 (minutes over a high-latency
        # DB). `_current_assignments` and `_children_count` back those fields.
        current = (
            PositionAssignment.objects
            .filter(end_date__isnull=True, user__is_active=True)
            .select_related('user')
            .order_by('-is_primary', 'id')
        )
        qs = (
            OrgNode.objects
            .select_related('parent', 'head_position')
            .prefetch_related(
                Prefetch('head_position__assignments', queryset=current, to_attr='_current_assignments')
            )
            .annotate(_children_count=Count('children'))
            .order_by('-updated_at')
        )

        if (is_active := params.get('is_active')) is not None:
            qs = qs.filter(is_active=str(is_active).lower() in ('1', 'true', 'yes'))
        if kind := params.get('kind'):
            qs = qs.filter(kind=kind.upper())
        if parent := params.get('parent'):
            qs = filter_by_id(qs, parent_id=parent)
        if (roots := params.get('roots')) and str(roots).lower() in ('1', 'true', 'yes'):
            qs = qs.filter(parent__isnull=True)
        if search := params.get('search'):
            qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return qs


class PositionViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    Seats in the org chart. People are attached via position assignments.

    Active platform employees may read seats and the reporting tree. Summary
    data such as vacancies requires platform staff plus platform.organogram.view;
    writes require platform staff plus platform.organogram.manage.

    Like OrgNode, a seat belongs to the CX chart and carries no tenant column,
    so the caller's tenant kind is the only boundary available - and vacancies
    is a read of the same chart the list actions already gate that way.

    docstring-name: Positions
    """

    serializer_class = PositionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        chart_actions = {'list', 'retrieve', 'tree'}
        if self.action in chart_actions:
            return [IsAuthenticatedAndActive(), IsVisionStaff()]
        self.rbac_permission = (
            'platform.organogram.view' if self.action == 'vacancies'
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), IsVisionStaff(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        # Prefetch the CURRENT holders once so the serializer's occupancy fields
        # (current_holders / is_vacant / open_seats) cost no per-row queries -
        # the list was N+1 (3 queries per seat → minutes over a high-latency DB).
        current = (
            PositionAssignment.objects
            .filter(end_date__isnull=True, user__is_active=True)
            .select_related('user')
            .order_by('-is_primary', 'id')
        )
        qs = (
            Position.objects
            .select_related('org_node', 'reports_to', 'default_role')
            .prefetch_related(
                Prefetch('assignments', queryset=current, to_attr='_current_assignments')
            )
            .order_by('title')
        )

        if org_node := params.get('org_node'):
            qs = filter_by_id(qs, org_node_id=org_node)
        if reports_to := params.get('reports_to'):
            qs = filter_by_id(qs, reports_to_id=reports_to)
        if (is_active := params.get('is_active')) is not None:
            qs = qs.filter(is_active=str(is_active).lower() in ('1', 'true', 'yes'))
        if search := params.get('search'):
            qs = qs.filter(Q(title__icontains=search) | Q(code__icontains=search))

        _SAFE_ORDERINGS = {
            'title', '-title',
            'created_at', '-created_at',
            'updated_at', '-updated_at',
        }
        if (ordering := params.get('ordering')) and ordering in _SAFE_ORDERINGS:
            qs = qs.order_by(ordering)

        return qs

    @action(detail=False, methods=['get'], url_path='tree')
    def tree(self, request):
        """Full position tree (solid reporting lines), nested from the roots."""
        root_id = request.query_params.get('root')
        root = None
        if root_id:
            root = filter_by_id(
                Position.objects.select_related('org_node'), pk=root_id,
            ).first()
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

    Every action except ``mine`` is platform staff only, asserted here and
    confined again by ``scope_assignments_to_caller`` in the queryset. ``mine``
    is self-service and bounded by the caller's own id, so it needs neither.

    docstring-name: Position assignments
    """

    serializer_class = PositionAssignmentSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.action in {'mine', 'current'}:
            permissions = [IsAuthenticatedAndActive()]
            if self.action == 'current':
                permissions.append(IsVisionStaff())
            return permissions
        read_actions = {'list', 'retrieve'}
        self.rbac_permission = (
            'platform.staff_profile.view' if self.action in read_actions
            else 'platform.organogram.manage'
        )
        return [IsAuthenticatedAndActive(), IsVisionStaff(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        # Confine first, filter second. ``?user=`` is a user id chosen by the
        # caller: applied to the whole table it selected any CX employee's
        # tenure history, and applied here it can only pick from the rows the
        # caller was already entitled to.
        qs = scope_assignments_to_caller(
            PositionAssignment.objects
            .select_related('user', 'position', 'position__org_node')
            .order_by('-start_date'),
            self.request,
        )
        if user_id := params.get('user'):
            qs = filter_by_id(qs, user_id=user_id)
        if position_id := params.get('position'):
            qs = filter_by_id(qs, position_id=position_id)
        if (current := params.get('current')) is not None:
            if str(current).lower() in ('1', 'true', 'yes'):
                qs = qs.filter(end_date__isnull=True)
            else:
                qs = qs.filter(end_date__isnull=False)
        return qs

    @action(detail=False, methods=['get'], url_path='mine')
    def mine(self, request):
        """Return only the signed-in user's position history (self-service)."""
        queryset = (
            PositionAssignment.objects
            .filter(user=request.user)
            .select_related('user', 'position', 'position__org_node')
            .order_by('-start_date')
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return success_response(
            message="Position history retrieved successfully.",
            data=serializer.data,
        )

    @action(detail=False, methods=['get'], url_path='current')
    def current(self, request):
        """Chart-safe current assignments for acting-seat badges.

        Full assignment history remains behind platform.organogram.view.  The
        public organogram needs only the holder, seat and acting flag, so avoid
        exposing tenure dates or historical rows here.
        """
        queryset = scope_assignments_to_caller(
            PositionAssignment.objects
            .filter(end_date__isnull=True, user__is_active=True)
            .select_related('user', 'position', 'position__org_node')
            .order_by('position__title', '-is_primary', 'id'),
            request,
        )
        data = [
            {
                'user': assignment.user,
                'position': assignment.position,
                'is_acting': assignment.is_acting,
            }
            for assignment in queryset
        ]
        serializer = OrganogramCurrentAssignmentSerializer(data, many=True)
        return success_response(
            message="Current organogram assignments retrieved successfully.",
            data=serializer.data,
        )

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

    Two CX seats and nothing else, so - as with OrgNode and Position - the
    caller's tenant kind is the only boundary there is, and the writes now ask
    for it rather than trusting the scope of the key.

    docstring-name: Matrix reports
    """

    serializer_class = MatrixReportSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        read_actions = {'list', 'retrieve'}
        if self.action in read_actions:
            return [IsAuthenticatedAndActive(), IsVisionStaff()]
        self.rbac_permission = 'platform.organogram.manage'
        return [IsAuthenticatedAndActive(), IsVisionStaff(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = (
            MatrixReport.objects
            .select_related('position', 'reports_to')
            .order_by('-created_at')
        )
        if position_id := params.get('position'):
            qs = filter_by_id(qs, position_id=position_id)
        if reports_to := params.get('reports_to'):
            qs = filter_by_id(qs, reports_to_id=reports_to)
        return qs
