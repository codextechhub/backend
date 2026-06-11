"""User account management: CRUD, email change, suspend/reactivate/unlock.
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
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from core.mixins import (
    XVSModelViewSetMixin,
)
from core.pagination import XVSPagination
from core.response import success_response, error_response
from ..models import (
    User,
)
from ..serializers import (
    UserReadSerializer, UserListSerializer, UserCreateSerializer, UserUpdateSerializer,
    EmailChangeSerializer,
)
from ..services.user       import UserCreationService, EmailChangeService, UserStatusService
from vs_workflow.services.submission import submit_for_approval as _wf_submit
from vs_workflow.serializers import WorkflowInstanceListSerializer as _WFInstanceSerializer


from .me import _get_date_param


# =============================================================================
# # USER MANAGEMENT VIEWS
# =============================================================================

class UserAccountViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """
    GET    /users/          — list users scoped to requesting admin's school
    POST   /users/          — create user + dispatch invitation email
    GET    /users/{id}/     — retrieve full user profile
    PATCH  /users/{id}/     — update profile fields (not email, not status)
    DELETE /users/{id}/     — soft-deactivate (never hard-delete)

    Permission matrix (TODO — wire up RBAC):
      list:           IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create (read access implied by create)
      create:         IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create
      retrieve:       IsAuthenticatedAndActive (owner or admin)
                      RBAC: identity.user_account.create (for non-owner access)
      partial_update: IsAuthenticatedAndActive (owner or admin)
                      RBAC: identity.user_account.create (for non-owner access)
      destroy:        IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create
                      + must not deactivate self (already enforced in service)
                      + tenant boundary check
    """

    pagination_class = XVSPagination

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        if self.action == 'list':
            return UserListSerializer
        return UserReadSerializer

    def get_queryset(self):
        user   = self.request.user
        params = self.request.query_params

        qs = User.objects.select_related('school', 'branch', 'invited_by', 'invitation')

        if getattr(user, 'user_type', None) == User.UserType.CX_STAFF:
            pass  # no tenant boundary — sees all users
        else:
            qs = qs.filter(school=user.school)

        qs = qs.exclude(status__in=[User.Status.PENDING_APPROVAL, User.Status.REJECTED])

        if status_val := params.get('status'):
            qs = qs.filter(status=status_val)

        if exclude_status := params.get('exclude_status'):
            qs = qs.exclude(status=exclude_status)

        if user_type := params.get('user_type'):
            qs = qs.filter(user_type=user_type)

        if branch_id := params.get('branch_id'):
            qs = qs.filter(branch_id=branch_id)

        if search := params.get('search'):
            if len(search) > 64:
                raise ValidationError({'search': 'Search query must be 64 characters or fewer.'})
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )

        if role := params.get('role'):
            qs = qs.filter(role__iexact=role)

        if invited_by := params.get('invited_by'):
            qs = qs.filter(invited_by_name__icontains=invited_by)

        if date_from := _get_date_param(params, 'date_from'):
            qs = qs.filter(created_at__date__gte=date_from)

        if date_to := _get_date_param(params, 'date_to'):
            qs = qs.filter(created_at__date__lte=date_to)

        _allowed_orderings = {
            'first_name', '-first_name',
            'email', '-email',
            'role', '-role',
            'status', '-status',
            'created_at', '-created_at',
        }
        ordering = params.get('ordering', '').strip()
        if ordering in _allowed_orderings:
            qs = qs.order_by(ordering)

        return qs

    def get_permissions(self):
        action_permissions = {
            'list':           'platform.team.view',
            'retrieve':       'platform.team.view',
            'create':         'platform.team.create',
            'update':         'platform.team.update',
            'partial_update': 'platform.team.update',
            'destroy':        'platform.team.delete',
        }
        self.rbac_permission = action_permissions.get(self.action, 'platform.team.view')
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Workflow gate only applies to platform (CX_STAFF) user creation.
        if serializer.validated_data.get("user_type") == User.UserType.CX_STAFF:
            user = UserCreationService.create_pending(
                validated_data=serializer.validated_data,
                requesting_user=request.user,
                request=request,
            )
            wf_instance = _wf_submit(document=user, requested_by=request.user)
            return Response({
                "user": UserReadSerializer(user).data,
                "workflow_instance": _WFInstanceSerializer(wf_instance).data,
            }, status=status.HTTP_201_CREATED)

        # All other user types: create and invite immediately.
        user = UserCreationService.create_pending(
            validated_data=serializer.validated_data,
            requesting_user=request.user,
            request=request,
        )
        UserCreationService.finalize_invitation(user=user, requested_by=request.user)
        return Response(UserReadSerializer(user).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Never hard-delete. Records and audit history are always preserved.
        UserStatusService.deactivate(
            target_user=instance,
            requesting_user=self.request.user,
            request=self.request,
        )


class UserEmailChangeView(APIView):
    """
    PATCH /user/{user_id}/email/change/
    Admin-only. Change takes effect immediately and ends all active sessions.
    The user logs in again with the new email and their existing password.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.email_address.verify
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.update"

    def patch(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        ser = EmailChangeSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        if ser.validated_data['email'] == user.email:
            return error_response(message="New email is the same as the current email.")

        try:
            updated = EmailChangeService.change_email(
                target_user=user,
                new_email=ser.validated_data['email'],
                requesting_user=request.user,
                request=request,
            )
        except Exception as e:
            detail = e.args[0] if e.args else {}
            http_status = (
                status.HTTP_409_CONFLICT
                if isinstance(detail, dict) and detail.get('error_code') == 'DUPLICATE_EMAIL'
                else status.HTTP_400_BAD_REQUEST
            )
            message = detail.get('detail', 'Email change failed.') if isinstance(detail, dict) else str(detail)
            return error_response(message=message, error=detail, status=http_status)

        return success_response(
            message="Email updated successfully.",
            data=UserListSerializer(updated).data,
        )


class UserSuspendView(APIView):
    """
    POST /{user_id}/suspend/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.lock
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.suspend"

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.suspend(user, request.user, request)
        except Exception as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Suspend failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return success_response(
            message="User suspended successfully.",
            data=UserListSerializer(updated).data,
        )


class UserReactivateView(APIView):
    """
    POST /{user_id}/reactivate/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.unlock
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.reactivate"

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.reactivate(user, request.user, request)
        except Exception as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Reactivation failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return success_response(
            message="User reactivated successfully.",
            data=UserListSerializer(updated).data,
        )


class UserUnlockView(APIView):
    """
    POST /{user_id}/unlock/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.unlock
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.reactivate"

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.unlock(user, request.user, request)
        except Exception as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Unlock failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return success_response(
            message="User unlocked successfully.",
            data=UserListSerializer(updated).data,
        )

