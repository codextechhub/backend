"""Security/audit read endpoints: sessions, attempts, lockouts, auth events.
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
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.views import APIView
from vs_rbac.permissions import IsAuthenticatedAndActive, IsVisionStaff, HasRBACPermission
from vs_tenants.models import Tenant
from core.pagination import XVSPagination
from core.response import success_response, error_response
from ..models import (
    User, LoginSession, AuthAttempt, AccountLockout,
    AuthEventLog, PasswordResetRequest,
)
from ..serializers import (
    LoginSessionReadSerializer, EndOtherSessionsSerializer, ForceLogoutSerializer, AuthAttemptReadSerializer, AccountLockoutReadSerializer,
    UnlockAccountSerializer, PasswordResetAdminSerializer,
)
from ..services.password   import PasswordService
from ..services.audit      import (
    log_auth_event, blacklist_all_user_tokens, blacklist_token_by_jti,
    blacklist_tokens_by_jti, expire_stale_login_sessions,
)


from .me import _get_date_param


# =============================================================================
# # SECURITY AND SESSION VIEWS
# =============================================================================

class SessionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /sessions/
    A user sees only their own sessions.
    Vision Staff see all sessions across the platform.

    Permission: IsAuthenticatedAndActive
    RBAC (list own):    system.session.view
    RBAC (list all):    system.session.view (Vision Staff only)
    RBAC (force-logout): system.session.force_logout + identity.user_logout.force

    docstring-name: Login sessions
    """
    serializer_class = LoginSessionReadSerializer
    pagination_class = XVSPagination
    filter_backends = [SearchFilter]
    search_fields = (
        'user__email',
        'user__first_name',
        'user__last_name',
        'ip_address',
        'device_label',
        'user_agent',
        'tenant__name',
        'tenant__slug',
    )

    def get_permissions(self):
        if self.action in {'mine', 'end_mine', 'end_other_mine', 'end_all_mine'}:
            return [IsAuthenticatedAndActive()]
        if self.action == 'force_logout':
            self.rbac_permission = 'platform.team.suspend'
            return [IsAuthenticatedAndActive(), HasRBACPermission()]
        self.rbac_permission = 'platform.security.view'
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        user = self.request.user

        # Platform-kind actors see every session in the asserted tenant scope
        # (the TenantAwareManager applies request.tenant); everyone else sees
        # only their own sessions.
        if getattr(getattr(user, 'tenant', None), 'kind', None) == Tenant.Kind.PLATFORM:
            expire_stale_login_sessions(tenant=getattr(self.request, 'tenant', None))
        else:
            expire_stale_login_sessions(user=user)

        qs = LoginSession.objects.select_related('user', 'tenant').order_by('-last_seen_at')
        if getattr(getattr(user, 'tenant', None), 'kind', None) != Tenant.Kind.PLATFORM:
            qs = qs.filter(user=user)

        if is_active := self.request.query_params.get('is_active'):
            qs = qs.filter(is_active=is_active.lower() == 'true')

        if user_id := self.request.query_params.get('user_id'):
            qs = qs.filter(user_id=user_id)

        return qs

    @action(detail=False, methods=['get'], url_path='mine')
    def mine(self, request):
        """List only the signed-in user's sessions (self-service)."""
        expire_stale_login_sessions(user=request.user)
        queryset = (
            LoginSession.objects
            .filter(user=request.user)
            .select_related('user', 'tenant')
            .order_by('-last_seen_at')
        )
        if is_active := request.query_params.get('is_active'):
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return success_response(data=serializer.data)

    @action(detail=True, methods=['post'], url_path='end-mine')
    def end_mine(self, request, pk=None):
        """End one session only when it belongs to the signed-in user."""
        session = LoginSession.objects.filter(pk=pk, user=request.user).first()
        if session is None:
            return error_response(
                message="Session not found.", status=status.HTTP_404_NOT_FOUND,
            )
        ended = 0
        if session.is_active:
            session.end(reason='FORCE_LOGOUT')
            session.save(update_fields=['is_active', 'ended_at', 'end_reason', 'updated_at'])
            blacklist_token_by_jti(session.refresh_jti)
            ended = 1
        log_auth_event(
            actor=request.user,
            subject=request.user,
            tenant=request.user.tenant,
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={'ended_sessions': ended, 'reason': 'SELF_SIGNOUT'},
        )
        return success_response(message="Session ended.", data={'ended_sessions': ended})

    @action(detail=False, methods=['post'], url_path='end-all-mine')
    def end_all_mine(self, request):
        """End all active sessions belonging to the signed-in user."""
        ended = LoginSession.all_objects.filter(
            user=request.user, is_active=True,
        ).update(
            is_active=False,
            ended_at=timezone.now(),
            end_reason='FORCE_LOGOUT',
        )
        blacklist_all_user_tokens(request.user)
        log_auth_event(
            actor=request.user,
            subject=request.user,
            tenant=request.user.tenant,
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={'ended_sessions': ended, 'reason': 'SUSPECTED_COMPROMISE'},
        )
        return success_response(
            message="All sessions ended.", data={'ended_sessions': ended},
        )

    @action(detail=False, methods=['post'], url_path='end-other-mine')
    def end_other_mine(self, request):
        """End every active session owned by the caller except this device."""
        serializer = EndOtherSessionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        current_session_id = serializer.validated_data['current_session_id']
        expire_stale_login_sessions(user=request.user)

        with transaction.atomic():
            active_sessions = list(
                LoginSession.all_objects
                .select_for_update()
                .filter(user=request.user, is_active=True)
                .values_list('pk', 'refresh_jti')
            )
            if not any(pk == current_session_id for pk, _ in active_sessions):
                return error_response(
                    message="Current session not found.",
                    status=status.HTTP_400_BAD_REQUEST,
                )

            other_sessions = [
                session for session in active_sessions
                if session[0] != current_session_id
            ]
            other_ids = [session_id for session_id, _ in other_sessions]
            ended = LoginSession.all_objects.filter(pk__in=other_ids).update(
                is_active=False,
                ended_at=timezone.now(),
                end_reason='FORCE_LOGOUT',
            )
            blacklist_tokens_by_jti(
                refresh_jti for _, refresh_jti in other_sessions
            )

        log_auth_event(
            actor=request.user,
            subject=request.user,
            tenant=request.user.tenant,
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={'ended_sessions': ended, 'reason': 'SELF_SIGNOUT_OTHERS'},
        )
        return success_response(
            message="Other sessions ended.", data={'ended_sessions': ended},
        )

    @action(detail=False, methods=['post'], url_path='force-logout')
    def force_logout(self, request):
        """
        POST /sessions/force-logout/
        Ends sessions for a specific user or a specific session.
        Also blacklists all outstanding JWT tokens for the user.
        """
        ser = ForceLogoutSerializer(data=request.data, context={'request': request})
        ser.is_valid(raise_exception=True)

        target_user = ser.validated_data.get('user_id')
        session     = ser.validated_data.get('session_id')
        reason      = ser.validated_data['reason']
        ended       = 0

        if session:
            session.end(reason='FORCE_LOGOUT')
            session.save(update_fields=['is_active', 'ended_at', 'end_reason', 'updated_at'])
            # Without this the refresh token tied to this session keeps working
            # and the user can transparently get a new access token after we say
            # we revoked their device.
            blacklist_token_by_jti(session.refresh_jti)
            ended = 1

        if target_user:
            # all_objects: the target is explicitly authorized via RBAC and may
            # live outside the ambient tenant (platform actor acting on a
            # school user) - every one of their sessions must end.
            ended = LoginSession.all_objects.filter(user=target_user, is_active=True).update(
                is_active=False,
                ended_at=timezone.now(),
                end_reason='FORCE_LOGOUT',
            )
            blacklist_all_user_tokens(target_user)

        log_auth_event(
            actor=request.user,
            subject=target_user if target_user else (session.user if session else None),
            tenant=request.user.tenant,
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={'ended_sessions': ended, 'reason': reason},
        )
        return success_response(
            message="Force logout executed.",
            data={'ended_sessions': ended},
        )


class AuthAttemptViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /auth-attempts/
    Vision Staff only. Shows all login attempts across the platform.

    Permission: IsAuthenticatedAndActive, IsVisionStaff
    RBAC: identity.authentication_events.log + system.audit.view

    docstring-name: Auth attempts
    """
    serializer_class   = AuthAttemptReadSerializer
    pagination_class   = XVSPagination

    def get_permissions(self):
        if self.action == 'mine':
            return [IsAuthenticatedAndActive()]
        self.rbac_permission = 'platform.security.view'
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        params = self.request.query_params
        qs = AuthAttempt.objects.select_related('user', 'tenant').order_by('-created_at')

        if user_id := params.get('user_id'):
            qs = qs.filter(user_id=user_id)

        if tenant_id := params.get('tenant_id'):
            qs = qs.filter(tenant_id=tenant_id)

        if email := params.get('email'):
            qs = qs.filter(email_entered__icontains=email)

        if ip_address := params.get('ip_address'):
            qs = qs.filter(ip_address__icontains=ip_address)

        if result := params.get('result'):
            qs = qs.filter(result=result)

        if failure_code := params.get('failure_code'):
            qs = qs.filter(failure_code=failure_code)

        if date_from := _get_date_param(params, 'date_from'):
            qs = qs.filter(created_at__date__gte=date_from)

        if date_to := _get_date_param(params, 'date_to'):
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    @action(detail=False, methods=['get'], url_path='mine')
    def mine(self, request):
        """List only the signed-in user's authentication attempts."""
        queryset = AuthAttempt.objects.filter(user=request.user).order_by('-created_at')
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return success_response(data=serializer.data)


class PasswordResetListView(APIView):
    """
    GET /user/password-resets/
    Vision Staff only. Lists active (unused, unexpired) reset tokens.

    docstring-name: Password reset requests
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionStaff]

    def get(self, request):
        resets = PasswordResetRequest.objects.filter(
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).select_related('user').order_by('-created_at')
        ser = PasswordResetAdminSerializer(resets, many=True)
        return success_response(data=ser.data)


class RevokePasswordResetView(APIView):
    """
    POST /user/password-resets/{pk}/revoke/
    Vision Staff only. Marks the token as used, invalidating it immediately.

    docstring-name: Revoke a password reset
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionStaff]

    def post(self, request, pk):
        try:
            reset = PasswordResetRequest.objects.get(pk=pk, used_at__isnull=True)
        except PasswordResetRequest.DoesNotExist:
            return error_response(
                message="Reset token not found or already used.",
                status=status.HTTP_404_NOT_FOUND,
            )
        reset.used_at = timezone.now()
        reset.save(update_fields=['used_at'])
        return success_response(message="Reset token revoked.")


class AccountLockoutViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /account-lockouts/
    Vision Staff only - lists all locked accounts.

    POST /account-lockouts/unlock/
    School Admins and Vision Staff can unlock accounts.
    Optionally triggers a 24-hour admin password reset email.

    Permission (list):   IsAuthenticatedAndActive, IsVisionStaff
    RBAC (list):         identity.user_account.lock + system.audit.view
    Permission (unlock): IsAuthenticatedAndActive, HasRBACPermission
    RBAC (unlock):       identity.user_account.unlock

    docstring-name: Account lockouts
    """
    serializer_class = AccountLockoutReadSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.action == 'unlock':
            self.rbac_permission = 'platform.team.reactivate'
            return [IsAuthenticatedAndActive(), HasRBACPermission()]
        return [IsAuthenticatedAndActive(), IsVisionStaff()]

    def get_queryset(self):
        params = self.request.query_params
        user = self.request.user
        qs = AccountLockout.objects.select_related('user').order_by('-updated_at')

        # Non-platform actors only see lockouts inside the asserted tenant;
        # platform-kind actors keep the platform-wide view (IsVisionStaff +
        # RBAC gate the endpoint).
        if getattr(getattr(user, 'tenant', None), 'kind', None) != Tenant.Kind.PLATFORM:
            qs = qs.filter(user__tenant=getattr(self.request, 'tenant', None) or user.tenant)

        if user_id := params.get('user_id'):
            qs = qs.filter(user_id=user_id)

        if locked_reason := params.get('locked_reason'):
            qs = qs.filter(locked_reason=locked_reason)

        if last_failure_ip := params.get('last_failure_ip'):
            qs = qs.filter(last_failure_ip=last_failure_ip)

        if is_locked := params.get('is_locked'):
            now = timezone.now()
            if is_locked.lower() == 'true':
                qs = qs.filter(locked_until__gt=now)
            else:
                qs = qs.filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))

        if date_from := _get_date_param(params, 'date_from'):
            qs = qs.filter(created_at__date__gte=date_from)

        if date_to := _get_date_param(params, 'date_to'):
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    @action(detail=False, methods=['post'], url_path='unlock')
    def unlock(self, request):
        ser = UnlockAccountSerializer(data=request.data, context={'request': request})
        ser.is_valid(raise_exception=True)

        user         = ser.validated_data['user']
        force_reset  = ser.validated_data['force_password_reset']
        reason       = ser.validated_data.get('reason', '')

        # Clear the lockout record.
        lockout, _ = AccountLockout.objects.get_or_create(user=user)
        lockout.clear()
        lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

        # Restore user status to ACTIVE if it was LOCKED.
        if user.status == User.Status.LOCKED:
            user.status = User.Status.ACTIVE
            user.save(update_fields=['status', 'updated_at'])

        # Optionally trigger a 24-hour admin password reset.
        if force_reset:
            PasswordService.admin_reset(
                target_user=user,
                requesting_user=request.user,
                request=request,
            )

        log_auth_event(
            actor=request.user,
            subject=user,
            tenant=user.tenant,
            event=AuthEventLog.Event.ACCOUNT_UNLOCKED,
            request=request,
            metadata={'force_password_reset': force_reset, 'reason': reason},
        )
        return success_response(message="Account unlocked successfully.")


class AuthEventLogViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /auth-events/
    Returns identity/auth AuditEvents from the central vs_audit store.
    Filters: actor_id, subject_id (entity_id), school_id, event (action_type),
             ip_address, date_from, date_to.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: platform.audit.view

    docstring-name: Auth event log
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    # The rows this returns are ``AuditEvent`` rows, pre-filtered to the identity
    # module, so the gate is the one every other read of that table already uses
    # (``vs_audit.views``: list, detail, entity trails, filter options). A second
    # key here would mean two different answers to "who may read this row",
    # decided by which URL it was asked for. ``platform.security.view``, which
    # the sessions and attempts viewsets above name, is deliberately not reused:
    # no seeder defines it, so it can be granted to nobody and would shut a
    # school's own auditor out of their own trail.
    #
    # The key is ``PermissionScope.TENANT`` on purpose - audit officers hold it
    # inside a tenant - so the key alone does not decide isolation. The queryset
    # does, below.
    rbac_permission    = 'platform.audit.view'
    pagination_class   = XVSPagination

    def get_serializer_class(self):
        from vs_audit.serializers import AuditEventListSerializer
        return AuditEventListSerializer

    def _scope_to_tenant(self, qs):
        """Narrow the trail to the caller's own tenant unless they are platform.

        Platform-kind actors keep the cross-tenant view; that is what the
        ``platform.`` surfaces are for, and the Event Explorer in ``vs_audit``
        divides the two the same way.

        The null-tenant half of the predicate is not defensive padding.
        ``AuditEvent.tenant`` is nullable and identity events only started
        carrying it in d1ceccb (2026-08-19), which deliberately did not backfill.
        Every identity row written before that has ``tenant = NULL`` while still
        recording the tenant's pk in ``metadata['tenant_id']`` - ``log_auth_event``
        has written that since 661a73a (2026-07-14). Matching on the column alone
        would hide Bright Star's own history from Bright Star's auditor; widening
        to every null row would hand them Greenfield's. Matching the id that was
        recorded at the time returns exactly the rows that are theirs, recovered
        rather than inferred. Rows older than 661a73a carry no id and stay
        platform-only, which is the safe direction to be wrong in.
        """
        user = self.request.user
        if getattr(getattr(user, 'tenant', None), 'kind', None) == Tenant.Kind.PLATFORM:
            return qs
        tenant = getattr(self.request, 'tenant', None) or user.tenant
        return qs.filter(
            Q(tenant=tenant)
            | Q(tenant__isnull=True, metadata__tenant_id=str(tenant.pk))
        )

    def get_queryset(self):
        from vs_audit.models import AuditEvent, AuditModuleKey
        params = self.request.query_params
        qs = self._scope_to_tenant(
            AuditEvent.objects.filter(module_key=AuditModuleKey.IDENTITY)
        ).select_related('actor_user').order_by('-event_at')

        if actor_id := params.get('actor_id'):
            qs = qs.filter(actor_user_id=actor_id)

        if subject_id := params.get('subject_id'):
            # subject is stored as entity_id (the User the action targeted)
            qs = qs.filter(entity_id=subject_id)

        if school_id := params.get('school_id'):
            qs = qs.filter(metadata__school_id=school_id)

        if event := params.get('event'):
            qs = qs.filter(action_type=event)

        if ip_address := params.get('ip_address'):
            qs = qs.filter(metadata__ip_address=ip_address)

        if date_from := _get_date_param(params, 'date_from'):
            qs = qs.filter(event_at__date__gte=date_from)

        if date_to := _get_date_param(params, 'date_to'):
            qs = qs.filter(event_at__date__lte=date_to)

        return qs
