from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone

from rest_framework import status, viewsets, mixins, generics
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# If you use SimpleJWT:
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

from vs_institutions.models import Institution

from .models import (
    UserAccount,
    TemporaryPasswordIssue,
    LoginSession,
    RevokedToken,
    AuthAttempt,
    AccountLockout,
    PasswordResetRequest,
    SuspiciousLoginEvent,
    AuthEventLog,
)
from .serializers import (
    # Users
    UserAccountReadSerializer,
    UserAccountCreateSerializer,
    UserAccountUpdateSerializer,
    AdminCreateAccountSerializer,
    # Auth
    LoginRequestSerializer,
    TokenRefreshSerializer,
    TokenRevokeSerializer,
    # Password
    PasswordChangeSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    # Temp password
    TemporaryPasswordIssueReadSerializer,
    TemporaryPasswordIssueCreateSerializer,
    # Sessions
    LoginSessionReadSerializer,
    ForceLogoutSerializer,
    # Lockout / attempts
    AuthAttemptReadSerializer,
    AccountLockoutReadSerializer,
    UnlockAccountSerializer,
    # Revoked tokens
    RevokedTokenReadSerializer,
    # Security events
    SuspiciousLoginEventReadSerializer,
    # Auth events
    AuthEventLogReadSerializer,
)
from .permissions import (
    IsVisionStaff,
    IsInstitutionAdminOrVisionStaff,
    IsSelfOrVisionStaff,
    IsVisionStaffOrSuperuser,
)


# -----------------------------------------------------------------------------
# Small “services” (kept simple so you can understand)
# -----------------------------------------------------------------------------

def _get_client_ip(request) -> str | None:
    xf = request.META.get("HTTP_X_FORWARDED_FOR")
    if xf:
        return xf.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _resolve_institution_from_slug(slug: str) -> Institution | None:
    if not slug:
        return None
    try:
        return Institution.objects.get(slug=slug)
    except Institution.DoesNotExist:
        return None


def _issue_tokens_for_user(user: UserAccount) -> dict:
    """
    SimpleJWT token creation.
    """
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token), "refresh_jti": str(refresh["jti"])}


def _log_auth_event(*, actor: UserAccount | None, subject: UserAccount | None, institution: Institution | None,
                    event: str, request, metadata: dict | None = None):
    AuthEventLog.objects.create(
        actor=actor,
        subject=subject,
        institution=institution,
        event=event,
        ip_address=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        metadata=metadata or {},
    )


def _record_attempt(*, email_entered: str, institution_context: str, user: UserAccount | None,
                    institution: Institution | None, result: str, failure_code: str, request, metadata: dict | None = None):
    AuthAttempt.objects.create(
        email_entered=email_entered,
        institution_context=institution_context or "",
        user=user,
        institution=institution,
        ip_address=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        result=result,
        failure_code=failure_code or "",
        metadata=metadata or {},
    )


# -----------------------------------------------------------------------------
# USERS (FR-IDA-001)
# -----------------------------------------------------------------------------

class UserAccountViewSet(viewsets.ModelViewSet):
    """
    /users/
    - Vision staff can manage users broadly
    - Non-vision users can read/update themselves (basic profile fields)
    """
    queryset = UserAccount.objects.select_related("branch").all()
    # permission_classes = [IsVisionStaffOrSuperuser]  # default, overridden in get_permissions

    def get_serializer_class(self):
        if self.action in ("create",):
            return UserAccountCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserAccountUpdateSerializer
        return UserAccountReadSerializer

    def get_permissions(self):
        if self.action in ("create", "list", "destroy"):
            return [IsAuthenticated(), IsVisionStaffOrSuperuser()]

        if self.action in ("retrieve", "update", "partial_update"):
            return [IsAuthenticated(), IsSelfOrVisionStaff(), ]

        return [IsAuthenticated()]

    def perform_create(self, serializer):
        """
        IMPORTANT (FR-IDA-002): serializer only creates the user row.
        You should generate/send temp password in a service.
        Here we keep it simple and just create an audit-ish AuthEventLog.
        """
        user = serializer.save()
        _log_auth_event(
            actor=self.request.user,
            subject=user,
            institution=getattr(user, "institution", None),
            event=AuthEventLog.Event.USER_CREATED,
            request=self.request,
            metadata={"via": "UserAccountViewSet.create"},
        )
        
class AdminCreateAccountView(generics.CreateAPIView):
    """
    Admin endpoint to create a user with a temporary password (FR-IDA-002).
    This is separate from the regular UserAccountViewSet create to enforce temp password flow.
    """
    queryset = UserAccount.objects.all()
    serializer_class = AdminCreateAccountSerializer
    permission_classes = [AllowAny]


# -----------------------------------------------------------------------------
# AUTH (FR-IDA-003/004/005/012)
# -----------------------------------------------------------------------------

class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = LoginRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"].strip()
        password = ser.validated_data["password"]
        institution_slug = ser.validated_data.get("institution_slug", "").strip()
        device_label = ser.validated_data.get("device_label", "")

        institution = _resolve_institution_from_slug(institution_slug) if institution_slug else None

        # Find user (case-insensitive). This matches your uniqueness constraints.
        user = UserAccount.objects.filter(email__iexact=email).first()

        # Block “fail open” on institution context (FR-IDA-012)
        if user and user.user_type != UserAccount.UserType.VISION_STAFF:
            if not institution:
                _record_attempt(
                    email_entered=email, institution_context=institution_slug,
                    user=user, institution=None,
                    result=AuthAttempt.Result.FAIL, failure_code="INSTITUTION_CONTEXT_REQUIRED",
                    request=request,
                )
                return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

            if user.institution_id != institution.id:
                # do not reveal what was wrong
                _record_attempt(
                    email_entered=email, institution_context=institution_slug,
                    user=user, institution=institution,
                    result=AuthAttempt.Result.FAIL, failure_code="INSTITUTION_MISMATCH",
                    request=request,
                )
                SuspiciousLoginEvent.objects.create(
                    user=user,
                    email_entered=email,
                    institution_context=institution_slug,
                    ip_address=_get_client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    event_type=SuspiciousLoginEvent.EventType.INSTITUTION_MISMATCH,
                    risk_score=70,
                    decision=SuspiciousLoginEvent.Decision.BLOCK,
                    details={"expected_institution_id": user.institution_id, "got_institution_id": institution.id},
                )
                return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # Use Django auth (if your AUTHENTICATION_BACKENDS support email)
        # If not, replace with manual check_password on the user.
        authed = authenticate(request=request, email=email, password=password)
        if not authed:
            _record_attempt(
                email_entered=email, institution_context=institution_slug,
                user=user, institution=institution,
                result=AuthAttempt.Result.FAIL, failure_code="INVALID_CREDENTIALS",
                request=request,
            )
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # Business status checks
        if authed.status in (UserAccount.Status.SUSPENDED, UserAccount.Status.DELETED, UserAccount.Status.LOCKED):
            _record_attempt(
                email_entered=email, institution_context=institution_slug,
                user=authed, institution=authed.institution,
                result=AuthAttempt.Result.BLOCKED, failure_code=authed.status,
                request=request,
            )
            return Response({"detail": "Account not available."}, status=status.HTTP_403_FORBIDDEN)

        # Issue JWT
        tokens = _issue_tokens_for_user(authed)

        # Create session record (FR-IDA-009)
        session = LoginSession.objects.create(
            user=authed,
            institution=authed.institution,
            ip_address=_get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            device_label=device_label or "",
            refresh_jti=tokens.get("refresh_jti", ""),
            last_seen_at=timezone.now(),
            is_active=True,
        )

        # Update user login timestamps
        authed.last_login_at = timezone.now()
        authed.save(update_fields=["last_login_at", "updated_at"])

        _record_attempt(
            email_entered=email, institution_context=institution_slug,
            user=authed, institution=authed.institution,
            result=AuthAttempt.Result.SUCCESS, failure_code="",
            request=request,
        )
        _log_auth_event(
            actor=authed, subject=authed, institution=authed.institution,
            event=AuthEventLog.Event.LOGIN_SUCCESS,
            request=request,
            metadata={"session_id": session.id},
        )

        # Force change password gate (FR-IDA-002)
        return Response(
            {
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "must_change_password": bool(authed.must_change_password),
                "session_id": session.id,
                "user": UserAccountReadSerializer(authed).data,
            },
            status=status.HTTP_200_OK,
        )


class TokenRefreshAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = TokenRefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            refresh = RefreshToken(ser.validated_data["refresh"])
            access = str(refresh.access_token)

            # Optional rotation: if you rotate refresh, you would also store/blacklist old JTI here.
            return Response({"access": access}, status=status.HTTP_200_OK)
        except TokenError:
            return Response({"detail": "Invalid or expired token."}, status=status.HTTP_401_UNAUTHORIZED)


class TokenRevokeAPIView(APIView):
    """
    Revoke by JTI (FR-IDA-005).
    For SimpleJWT, you’ll typically revoke refresh tokens (and optionally access tokens).
    """
    permission_classes = [IsAuthenticated, IsInstitutionAdminOrVisionStaff]

    def post(self, request):
        ser = TokenRevokeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        jti = ser.validated_data["jti"]
        token_type = ser.validated_data["token_type"]
        reason = ser.validated_data.get("reason", "")

        # Store JTI in revocation store
        RevokedToken.objects.get_or_create(
            jti=jti,
            defaults={
                "user": request.user,
                "token_type": token_type,
                "expires_at": None,
                "reason": reason or "logout",
                "revoked_by": request.user,
            },
        )

        _log_auth_event(
            actor=request.user, subject=request.user, institution=getattr(request.user, "institution", None),
            event=AuthEventLog.Event.TOKEN_REVOKED, request=request,
            metadata={"jti": jti, "token_type": token_type, "reason": reason},
        )

        return Response({"detail": "Token revoked."}, status=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# PASSWORD CHANGE / RESET (FR-IDA-002/011)
# -----------------------------------------------------------------------------

class PasswordChangeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = PasswordChangeSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        u: UserAccount = request.user
        u.set_password(ser.validated_data["new_password"])
        u.must_change_password = False
        u.password_changed_at = timezone.now()
        u.save(update_fields=["password", "must_change_password", "password_changed_at", "updated_at"])

        _log_auth_event(
            actor=u, subject=u, institution=u.institution,
            event=AuthEventLog.Event.PASSWORD_CHANGED,
            request=request,
            metadata={"forced_change_flow": True},
        )
        return Response({"detail": "Password updated."}, status=status.HTTP_200_OK)


class PasswordResetRequestAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        email = ser.validated_data["email"].strip()
        institution_slug = (ser.validated_data.get("institution_slug") or "").strip()
        institution = _resolve_institution_from_slug(institution_slug) if institution_slug else None

        # Generic response regardless of existence (avoid enumeration)
        user_qs = UserAccount.objects.filter(email__iexact=email)
        if institution:
            user_qs = user_qs.filter(institution=institution)
        user = user_qs.first()

        if user and user.status != UserAccount.Status.DELETED:
            # Create a reset request with a hashed token.
            raw_token = secrets.token_urlsafe(32)  # send this to user email via your notification service
            token_hash = PasswordResetRequest.hash_token(raw_token)
            PasswordResetRequest.objects.create(
                user=user,
                token_hash=token_hash,
                expires_at=timezone.now() + timedelta(minutes=30),
                requested_ip=_get_client_ip(request),
                requested_user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
            _log_auth_event(
                actor=None, subject=user, institution=user.institution,
                event=AuthEventLog.Event.PASSWORD_RESET_REQUESTED,
                request=request,
                metadata={"institution_slug": institution_slug},
            )
            # NOTE: send raw_token via email/SMS here (not shown)

        return Response({"detail": "If the account exists, reset instructions have been sent."}, status=status.HTTP_200_OK)


class PasswordResetConfirmAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetConfirmSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        raw_token = ser.validated_data["token"]
        token_hash = PasswordResetRequest.hash_token(raw_token)

        pr = PasswordResetRequest.objects.filter(token_hash=token_hash, used_at__isnull=True).first()
        if not pr or pr.is_expired():
            return Response({"detail": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

        user = pr.user
        user.set_password(ser.validated_data["new_password"])
        user.must_change_password = False
        user.password_changed_at = timezone.now()

        with transaction.atomic():
            user.save(update_fields=["password", "must_change_password", "password_changed_at", "updated_at"])
            pr.mark_used()
            pr.save(update_fields=["used_at", "updated_at"])

        _log_auth_event(
            actor=None, subject=user, institution=user.institution,
            event=AuthEventLog.Event.PASSWORD_RESET_COMPLETED,
            request=request,
        )
        return Response({"detail": "Password reset successful."}, status=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# TEMP PASSWORD ISSUANCE (FR-IDA-002) — admin operation
# -----------------------------------------------------------------------------

class TemporaryPasswordIssueViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = TemporaryPasswordIssue.objects.select_related("user").all()

    def get_serializer_class(self):
        if self.action == "create":
            return TemporaryPasswordIssueCreateSerializer
        return TemporaryPasswordIssueReadSerializer

    def get_permissions(self):
        # Typically Vision staff only; adjust to allow institution admins if policy allows
        return [IsAuthenticated(), IsVisionStaff()]

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user: UserAccount = ser.validated_data["user"]
        channel = ser.validated_data["channel"]
        delivered_to = ser.validated_data["delivered_to"]
        ttl_minutes = ser.validated_data["ttl_minutes"]

        # Generate + hash verifier (do NOT store plaintext)
        temp_password = TemporaryPasswordIssue.generate_temp_password()
        verifier = TemporaryPasswordIssue.build_verifier(user.id, temp_password)

        issue = TemporaryPasswordIssue.objects.create(
            user=user,
            channel=channel,
            delivered_to=delivered_to,
            expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
            delivered_at=None,
            delivery_status="PENDING",
            verifier_hash=verifier,
        )

        # Enforce must-change on next login (FR-IDA-002)
        user.set_password(temp_password)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password", "updated_at"])

        _log_auth_event(
            actor=request.user, subject=user, institution=user.institution,
            event=AuthEventLog.Event.TEMP_PASSWORD_ISSUED,
            request=request,
            metadata={"issue_id": issue.id, "channel": channel, "delivered_to": delivered_to},
        )

        # NOTE: Send temp_password via email in your notification service (not shown).
        # Update issue.delivery_status to SENT/FAILED based on delivery result.

        return Response(TemporaryPasswordIssueReadSerializer(issue).data, status=status.HTTP_201_CREATED)


# -----------------------------------------------------------------------------
# SESSIONS (FR-IDA-009/010)
# -----------------------------------------------------------------------------

class SessionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    /sessions/
    - list your own active sessions
    - Vision staff can list all (optional)
    """
    serializer_class = LoginSessionReadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        u = self.request.user
        qs = LoginSession.objects.select_related("user", "institution").order_by("-last_seen_at")
        if getattr(u, "user_type", None) == "VISION_STAFF":
            return qs
        return qs.filter(user=u)

    @action(detail=False, methods=["post"], url_path="force-logout", permission_classes=[IsAuthenticated, IsInstitutionAdminOrVisionStaff])
    def force_logout(self, request):
        ser = ForceLogoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = ser.validated_data.get("user_id")
        session = ser.validated_data.get("session_id")
        reason = ser.validated_data["reason"]

        ended = 0
        if session:
            session.end(reason="FORCE_LOGOUT")
            session.save(update_fields=["is_active", "ended_at", "end_reason", "updated_at"])
            ended = 1

        if user:
            sessions = LoginSession.objects.filter(user=user, is_active=True)
            for s in sessions:
                s.end(reason="FORCE_LOGOUT")
                s.save(update_fields=["is_active", "ended_at", "end_reason", "updated_at"])
                ended = sessions.count()

        _log_auth_event(
            actor=request.user,
            subject=user if user else (session.user if session else None),
            institution=getattr(request.user, "institution", None),
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={"ended_sessions": ended, "reason": reason},
        )
        return Response({"detail": "Force logout executed.", "ended_sessions": ended}, status=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# BRUTE FORCE / LOCKOUT (FR-IDA-006/007/008)
# -----------------------------------------------------------------------------

class AuthAttemptViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = AuthAttemptReadSerializer
    permission_classes = [IsAuthenticated, IsVisionStaff]
    queryset = AuthAttempt.objects.select_related("user", "institution").order_by("-created_at")


class AccountLockoutViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = AccountLockoutReadSerializer
    permission_classes = [IsAuthenticated, IsVisionStaff]
    queryset = AccountLockout.objects.select_related("user").order_by("-updated_at")

    @action(detail=False, methods=["post"], url_path="unlock", permission_classes=[IsAuthenticated, IsVisionStaff])
    def unlock(self, request):
        ser = UnlockAccountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user: UserAccount = ser.validated_data["user"]
        force_reset = ser.validated_data["force_password_reset"]
        reason = ser.validated_data.get("reason", "")

        lockout, _ = AccountLockout.objects.get_or_create(user=user)
        lockout.clear()
        lockout.save(update_fields=["failure_count", "locked_until", "locked_reason", "updated_at"])

        # Also update user status if it was locked
        if user.status == UserAccount.Status.LOCKED:
            user.status = UserAccount.Status.ACTIVE
            user.save(update_fields=["status", "updated_at"])

        _log_auth_event(
            actor=request.user, subject=user, institution=user.institution,
            event=AuthEventLog.Event.ACCOUNT_UNLOCKED,
            request=request,
            metadata={"force_password_reset": force_reset, "reason": reason},
        )

        return Response({"detail": "Account unlocked."}, status=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# TOKEN REVOCATION STORE (FR-IDA-005)
# -----------------------------------------------------------------------------

class RevokedTokenViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = RevokedTokenReadSerializer
    permission_classes = [IsAuthenticated, IsVisionStaff]
    queryset = RevokedToken.objects.select_related("user", "session", "revoked_by").order_by("-created_at")


# -----------------------------------------------------------------------------
# SECURITY EVENTS (optional surfaces)
# -----------------------------------------------------------------------------

class SuspiciousLoginEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = SuspiciousLoginEventReadSerializer
    permission_classes = [IsAuthenticated, IsVisionStaff]
    queryset = SuspiciousLoginEvent.objects.select_related("user").order_by("-created_at")


class AuthEventLogViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = AuthEventLogReadSerializer
    permission_classes = [IsAuthenticated, IsVisionStaff]
    queryset = AuthEventLog.objects.select_related("actor", "subject", "institution").order_by("-created_at")
