from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    UserAccountViewSet,
    AdminCreateAccountView,
    LoginAPIView,
    TokenRefreshAPIView,
    TokenRevokeAPIView,
    PasswordChangeAPIView,
    PasswordResetRequestAPIView,
    PasswordResetConfirmAPIView,
    TemporaryPasswordIssueViewSet,
    SessionViewSet,
    AuthAttemptViewSet,
    AccountLockoutViewSet,
    RevokedTokenViewSet,
    SuspiciousLoginEventViewSet,
    AuthEventLogViewSet,
)

router = DefaultRouter()
router.register(r"temp-password-issues", TemporaryPasswordIssueViewSet, basename="temp-password-issues")
router.register(r"sessions", SessionViewSet, basename="sessions")
router.register(r"auth-attempts", AuthAttemptViewSet, basename="auth-attempts")
router.register(r"account-lockouts", AccountLockoutViewSet, basename="account-lockouts")
router.register(r"revoked-tokens", RevokedTokenViewSet, basename="revoked-tokens")
router.register(r"suspicious-logins", SuspiciousLoginEventViewSet, basename="suspicious-logins")
router.register(r"auth-events", AuthEventLogViewSet, basename="auth-events")

urlpatterns = [
    path("admin-create/", AdminCreateAccountView.as_view(), name="admin-create-account"),
    path("", UserAccountViewSet.as_view(), name="users"),
    path("", include(router.urls)),
    path("auth/login/", LoginAPIView.as_view(), name="auth-login"),
    path("auth/token/refresh/", TokenRefreshAPIView.as_view(), name="auth-token-refresh"),
    path("auth/token/revoke/", TokenRevokeAPIView.as_view(), name="auth-token-revoke"),

    path("auth/password/change/", PasswordChangeAPIView.as_view(), name="password-change"),
    path("auth/password/reset/request/", PasswordResetRequestAPIView.as_view(), name="password-reset-request"),
    path("auth/password/reset/confirm/", PasswordResetConfirmAPIView.as_view(), name="password-reset-confirm"),
]