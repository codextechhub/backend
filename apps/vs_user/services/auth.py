# services/auth.py
# Authentication logic — login, school context enforcement,
# lockout handling, and JWT token issuance.

from __future__ import annotations
import email

from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone

from ..models import User, LoginSession, AccountLockout, AuthAttempt, AuthEventLog
from ..tokens import CodeXRefreshToken
from ..serializers import UserReadSerializer
from .audit import log_auth_event, record_attempt, blacklist_all_user_tokens, get_client_ip

# TODO: Default lockout threshold — overridable per school via System Config (Module 6).
DEFAULT_LOCK_THRESHOLD = 5
DEFAULT_LOCK_MINUTES   = 15


class LoginService:

    @staticmethod
    @transaction.atomic
    def login(email: str, password: str, request=None) -> dict:
        """
        Authenticates a user and returns tokens + user data.

        Steps:
          1. Resolve school from slug (if provided)
          2. Find user by email
          3. Check account lockout
          4. Enforce school context (non-Vision Staff must provide slug)
          5. Authenticate credentials
          6. Check account status
          7. Clear lockout on success
          8. Create session record
          9. Issue JWT tokens
         10. Write audit logs
        """
        # Normalize email
        email = email.lower().strip()

        # 2. Find user
        user = User.objects.filter(email__iexact=email).first()
        school_slug = user.school.slug if (user and user.school) else ''
        school      = user.school if user else None

        # 3. Check lockout before attempting the password
        if user:
            lockout = AccountLockout.objects.filter(user=user).first()
            if lockout and lockout.is_locked_now():
                record_attempt(
                    email_entered=email,
                    user=user, school=school,
                    result=AuthAttempt.Result.BLOCKED, failure_code='LOCKED',
                    request=request,
                )
                raise ValueError({
                    'error_code': 'ACCOUNT_LOCKED',
                    'message':    'Your account is locked. Please contact your administrator or reset your password.',
                })

        # 4. School context enforcement
        # Non-Vision Staff must always provide a valid school slug.
        if user and user.user_type != User.UserType.VISION_STAFF:
            if not school:
                record_attempt(
                    email_entered=email,
                    user=user, school=None,
                    result=AuthAttempt.Result.FAIL, failure_code='SCHOOL_CONTEXT_REQUIRED',
                    request=request,
                )
                # Generic message — do not reveal whether the email was found.
                raise ValueError({'error_code': 'INVALID_CREDENTIALS', 'message': 'Invalid credentials.'})

            if user.school_id != school.id:
                record_attempt(
                    email_entered=email,
                    user=user, school=user.school,
                    result=AuthAttempt.Result.FAIL, failure_code='SCHOOL_MISMATCH',
                    request=request,
                )
                raise ValueError({'error_code': 'INVALID_CREDENTIALS', 'message': 'Invalid credentials.'})

        # 5. Authenticate credentials
        authed = authenticate(request=request, username=email, password=password)
        if not authed:
            LoginService._handle_failed_attempt(user, school, school_slug, request)
            raise ValueError({'error_code': 'INVALID_CREDENTIALS', 'message': 'Invalid credentials.'})

        # 6. Check account status
        status_error = LoginService._check_status(authed)
        if status_error:
            record_attempt(
                email_entered=email,
                user=authed, school=authed.school,
                result=AuthAttempt.Result.BLOCKED, failure_code=authed.status,
                request=request,
            )
            raise ValueError(status_error)

        # 7. Clear lockout on successful login
        lockout = AccountLockout.objects.filter(user=authed).first()
        if lockout:
            lockout.clear()
            lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

        # 8. Issue JWT tokens
        refresh = CodeXRefreshToken.for_user(authed)
        tokens  = {'access': str(refresh.access_token), 'refresh': str(refresh)}

        # 9. Create session record
        session = LoginSession.objects.create(
            user=authed,
            school=authed.school,
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
            device_label='',
            refresh_jti=str(refresh['jti']),
            last_seen_at=timezone.now(),
            is_active=True,
        )

        # 10. Update last login and write audit logs
        authed.last_login_at = timezone.now()
        authed.save(update_fields=['last_login_at', 'updated_at'])

        record_attempt(
            email_entered=email,
            user=authed, school=authed.school,
            result=AuthAttempt.Result.SUCCESS, failure_code='',
            request=request,
        )
        log_auth_event(
            actor=authed, subject=authed, school=authed.school,
            event=AuthEventLog.Event.LOGIN_SUCCESS,
            request=request,
            metadata={'session_id': session.id},
        )

        return {
            'access':     tokens['access'],
            'refresh':    tokens['refresh'],
            'session_id': session.id,
            'user':       UserReadSerializer(authed).data,
            # 'cached_school': request._cached_school,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _resolve_school(slug: str):
        if not slug:
            return None
        from vs_schools.models import School
        try:
            return School.objects.get(slug=slug)
        except School.DoesNotExist:
            return None

    @staticmethod
    def _check_status(user: User) -> dict | None:
        """Returns an error payload if the account cannot log in, else None."""
        errors = {
            User.Status.PENDING: {
                'error_code': 'ACCOUNT_NOT_ACTIVATED',
                'message':    'Your account is not yet activated. Please check your invitation email.',
            },
            User.Status.LOCKED: {
                'error_code': 'ACCOUNT_LOCKED',
                'message':    'Your account is locked. Please contact your administrator or reset your password.',
            },
            User.Status.SUSPENDED: {
                'error_code': 'ACCOUNT_SUSPENDED',
                'message':    'Your account has been suspended. Please contact your administrator.',
            },
            User.Status.DEACTIVATED: {
                'error_code': 'ACCOUNT_DEACTIVATED',
                'message':    'This account has been deactivated. Please contact your administrator.',
            },
        }
        return errors.get(user.status)

    @staticmethod
    def _handle_failed_attempt(user, school, school_slug, request):
        """Increments the failure counter and locks the account if threshold is reached."""
        if user:
            lockout, _ = AccountLockout.objects.get_or_create(user=user)
            lockout.register_failure(
                ip=get_client_ip(request),
                lock_threshold=DEFAULT_LOCK_THRESHOLD,
                lock_minutes=DEFAULT_LOCK_MINUTES,
            )
            lockout.save(update_fields=[
                'failure_count', 'locked_until', 'locked_reason',
                'last_failure_at', 'last_failure_ip', 'updated_at',
            ])
            if lockout.is_locked_now():
                user.status = User.Status.LOCKED
                user.save(update_fields=['status', 'updated_at'])
                log_auth_event(
                    actor=None, subject=user, school=user.school,
                    event=AuthEventLog.Event.ACCOUNT_LOCKED, request=request,
                )

        record_attempt(
            email_entered=user.email if user else '',
            user=user, school=school,
            result=AuthAttempt.Result.FAIL, failure_code='INVALID_CREDENTIALS',
            request=request,
        )