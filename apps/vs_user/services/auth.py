# services/auth.py
# Authentication logic - login, school context enforcement,
# lockout handling, and JWT token issuance.

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_tenants.models import Tenant

from ..models import User, LoginSession, AccountLockout, AuthAttempt, AuthEventLog
from ..tokens import CodeXRefreshToken
from ..serializers import UserReadSerializer, school_public_info
from .audit import log_auth_event, record_attempt, blacklist_all_user_tokens, get_client_ip, get_device_label
from .sign_in_scope import resolve_sign_in_account

class LoginService:

    @staticmethod
    def login(email: str, password: str, tenant: str | None = None, request=None) -> dict:
        """
        Authenticates a user and returns tokens + user data.

        NOT wrapped in @transaction.atomic at the top level - audit writes
        (record_attempt, log_auth_event) must persist even when login fails.
        A top-level atomic block would roll them back together with the raised
        ValueError. Only the success path (session + token + user update) is
        wrapped in its own atomic block.

        ``tenant`` is the asserted tenant SLUG, which the frontend reads off the
        subdomain it is served from: a school's page at
        ``bright-star.xvs.codexng.com`` sends ``bright-star``. It is now
        MANDATORY: ``sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN`` is on, so a
        sign-in that names no tenant is refused rather than resolved by an
        ambiguous email lookup. The tenant is resolved BEFORE the account
        lookup and the lookup is scoped to it.

        Steps:
          1. Resolve the asserted tenant, then find the user inside it
          2. Refuse a non-platform user whose tenant has no school profile
          3. Authenticate credentials
          4. Check account lockout - only AFTER a correct password, so the
             locked state is never revealed to someone who doesn't know it
             (prevents an account-state oracle)
          5. Check account status
          6. [atomic] Clear lockout, create session, issue tokens, update user
          7. Write audit logs
        """
        email = email.lower().strip()

        # 1. Resolve the tenant first, then find the user within it. The name
        # ``tenant`` is deliberately rebound here: it arrives as an asserted
        # slug and leaves as the row that slug resolved to, so every later step
        # reads the checked object and never the caller's raw claim.
        user, tenant, scope_failure = resolve_sign_in_account(email=email, tenant=tenant)

        if scope_failure:
            # The refusal is indistinguishable from a wrong password to the
            # caller, and the audit row names only the tenant they ASSERTED -
            # never the one the address really belongs to, and no user FK.
            # Recording either would put "this address has an account at some
            # other customer" into an attempt log that a customer's own staff
            # can read.
            record_attempt(
                email_entered=email,
                user=None, tenant=tenant,
                result=AuthAttempt.Result.FAIL, failure_code=scope_failure,
                request=request,
            )
            raise ValueError({'code': 'INVALID_CREDENTIALS', 'detail': 'Invalid credentials.'})

        # 2. School-binding enforcement - non-platform tenants must resolve to a
        # school profile. Gated by the actor's TENANT KIND.
        if user and getattr(user.tenant, 'kind', None) != Tenant.Kind.PLATFORM:
            if getattr(user.tenant, 'school_profile', None) is None:
                record_attempt(
                    email_entered=email,
                    user=user, tenant=tenant,
                    result=AuthAttempt.Result.FAIL, failure_code='SCHOOL_CONTEXT_REQUIRED',
                    request=request,
                )
                raise ValueError({'code': 'INVALID_CREDENTIALS', 'detail': 'Invalid credentials.'})

        # 3. Authenticate credentials.
        # check_password directly instead of django's authenticate() - authenticate()
        # returns None for is_active=False users, masking the real reason.
        if not user or not user.check_password(password):
            LoginService._handle_failed_attempt(user, tenant, email, request)
            raise ValueError({'code': 'INVALID_CREDENTIALS', 'detail': 'Invalid credentials.'})

        # 4. Lockout check - the caller proved they know the password, so a
        # status-specific message is safe (and genuinely useful) here.
        with transaction.atomic():
            lockout = AccountLockout.objects.select_for_update().filter(user=user).first()
            if lockout and lockout.is_locked_now():
                record_attempt(
                    email_entered=email,
                    user=user, tenant=tenant,
                    result=AuthAttempt.Result.BLOCKED, failure_code='LOCKED',
                    request=request,
                )
                raise ValueError({
                    'code':   'ACCOUNT_LOCKED',
                    'detail': 'Your account is locked. Please contact your administrator or reset your password.',
                })

        authed = user

        # 5. Check account status
        status_error = LoginService._check_status(authed)
        if status_error:
            record_attempt(
                email_entered=email,
                user=authed, tenant=authed.tenant,
                result=AuthAttempt.Result.BLOCKED, failure_code=authed.status,
                request=request,
            )
            raise ValueError(status_error)

        # 6. Success path - atomic: session + token + user update must all commit or all roll back.
        with transaction.atomic():
            lockout = AccountLockout.objects.select_for_update().filter(user=authed).first()
            if lockout:
                lockout.clear()
                lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

            refresh = CodeXRefreshToken.for_user(authed)
            tokens  = {'access': str(refresh.access_token), 'refresh': str(refresh)}

            ua_string = request.META.get('HTTP_USER_AGENT', '') if request else ''
            session = LoginSession.objects.create(
                user=authed,
                tenant=authed.tenant,
                ip_address=get_client_ip(request),
                user_agent=ua_string,
                device_label=get_device_label(ua_string, request),
                refresh_jti=str(refresh['jti']),
                last_seen_at=timezone.now(),
                is_active=True,
            )

            authed.last_login_at = timezone.now()
            authed.save(update_fields=['last_login_at', 'updated_at'])

        # 7. Audit writes - outside any transaction so they always persist.
        record_attempt(
            email_entered=email,
            user=authed, tenant=authed.tenant,
            result=AuthAttempt.Result.SUCCESS, failure_code='',
            request=request,
        )
        log_auth_event(
            actor=authed, subject=authed, tenant=authed.tenant,
            event=AuthEventLog.Event.LOGIN_SUCCESS,
            request=request,
            metadata={'session_id': session.id},
        )

        from vs_rbac.evaluator import get_effective_permissions
        from vs_tenants.context import tenant_context_block
        permissions = sorted(get_effective_permissions(authed, tenant=authed.tenant))

        return {
            'access':      tokens['access'],
            'refresh':     tokens['refresh'],
            'session_id':  session.id,
            'user':        UserReadSerializer(authed).data,
            'tenant':      tenant_context_block(authed.tenant),
            'school':      school_public_info(getattr(authed.tenant, 'school_profile', None), request),
            'permissions': permissions,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _check_status(user: User) -> dict | None:
        """Returns an error payload if the account cannot log in, else None."""
        errors = {
            User.Status.PENDING: {
                'code':   'ACCOUNT_NOT_ACTIVATED',
                'detail': 'Your account is not yet activated. Please check your invitation email or contact your administrator.',
            },
            User.Status.LOCKED: {
                'code':   'ACCOUNT_LOCKED',
                'detail': 'Your account is locked. Please contact your administrator or reset your password.',
            },
            User.Status.SUSPENDED: {
                'code':   'ACCOUNT_SUSPENDED',
                'detail': 'Your account has been suspended. Please contact your administrator.',
            },
            User.Status.DEACTIVATED: {
                'code':   'ACCOUNT_DEACTIVATED',
                'detail': 'This account has been deactivated. Please contact your administrator.',
            },
        }
        return errors.get(user.status)

    @staticmethod
    def _handle_failed_attempt(user, tenant, email_entered, request):
        """Increment the failure counter, lock the account if threshold is reached, and record the attempt.

        The lockout update is wrapped in its own atomic block so the counter
        persists regardless of what the caller does after. record_attempt is
        called outside that block for the same reason - it must never be rolled
        back by a caller's exception handler.
        """
        just_locked = False
        if user:
            from vs_config.runtime_settings import get_security_settings

            security_settings = get_security_settings(
                tenant=user.tenant, branch=user.branch,
            )

            with transaction.atomic():
                lockout, _ = AccountLockout.objects.select_for_update().get_or_create(user=user)
                lockout.register_failure(
                    ip=get_client_ip(request),
                    lock_threshold=security_settings["failed_login_threshold"],
                    lock_minutes=security_settings["account_lock_minutes"],
                )
                lockout.save(update_fields=[
                    'failure_count', 'locked_until', 'locked_reason',
                    'last_failure_at', 'last_failure_ip', 'updated_at',
                ])
                if lockout.is_locked_now():
                    user.status = User.Status.LOCKED
                    user.save(update_fields=['status', 'updated_at'])
                    just_locked = True

            if just_locked:
                log_auth_event(
                    actor=None, subject=user, tenant=user.tenant,
                    event=AuthEventLog.Event.ACCOUNT_LOCKED, request=request,
                )

        # Always record the email as ENTERED - for unknown accounts this is the
        # only identifying datum the security team has (spraying, typos, probes).
        record_attempt(
            email_entered=email_entered or (user.email if user else ''),
            user=user, tenant=tenant,
            result=AuthAttempt.Result.FAIL, failure_code='INVALID_CREDENTIALS',
            request=request,
        )
