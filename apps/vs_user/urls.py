from django.urls import path

from .views import (
    # Users
    UserAccountViewSet,
    AdminCreateAccountView,

    # Temp password
    TemporaryPasswordIssueViewSet,

    # Sessions
    SessionViewSet,

    # Security / logs
    AuthAttemptViewSet,
    AccountLockoutViewSet,
    RevokedTokenViewSet,
    SuspiciousLoginEventViewSet,
    AuthEventLogViewSet,

    # APIViews (auth/password)
    LoginAPIView,
    TokenRefreshAPIView,
    TokenRevokeAPIView,
    PasswordChangeAPIView,
    PasswordResetRequestAPIView,
    PasswordResetConfirmAPIView,
)

urlpatterns = [
    # -------------------------------------------------------------------------
    # AUTH (JWT)
    # -------------------------------------------------------------------------
    path("auth/login/", LoginAPIView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshAPIView.as_view(), name="auth-refresh"),
    path("auth/revoke/", TokenRevokeAPIView.as_view(), name="auth-revoke"),

    # -------------------------------------------------------------------------
    # PASSWORD
    # -------------------------------------------------------------------------
    path("auth/password/change/", PasswordChangeAPIView.as_view(), name="password-change"),
    path("auth/password/reset/request/", PasswordResetRequestAPIView.as_view(), name="password-reset-request"),
    path("auth/password/reset/confirm/", PasswordResetConfirmAPIView.as_view(), name="password-reset-confirm"),

    # -------------------------------------------------------------------------
    # USERS
    # (ModelViewSet via path using `.as_view({})`)
    # -------------------------------------------------------------------------
    
    path(
        'admin-create/', AdminCreateAccountView.as_view(), name='admin-create-account'),
    path(
        'create/', UserCreateView.as_view(), name='user-create'),
    path(
        "",
        UserAccountViewSet.as_view({"get": "list", "post": "create"}),
        name="users-list-create",
    ),
    path(
        "<int:pk>/",
        UserAccountViewSet.as_view({"get": "retrieve", "patch": "partial_update", "put": "update"}),
        name="users-detail",
    ),

    # -------------------------------------------------------------------------
    # TEMP PASSWORD ISSUES (admin operation)
    # -------------------------------------------------------------------------
    path(
        "temp-password-issues/",
        TemporaryPasswordIssueViewSet.as_view({"get": "list", "post": "create"}),
        name="temp-password-issues-list-create",
    ),

    # -------------------------------------------------------------------------
    # SESSIONS
    # -------------------------------------------------------------------------
    path(
        "sessions/",
        SessionViewSet.as_view({"get": "list"}),
        name="sessions-list",
    ),
    path(
        "sessions/force-logout/",
        SessionViewSet.as_view({"post": "force_logout"}),
        name="sessions-force-logout",
    ),

    # -------------------------------------------------------------------------
    # SECURITY / OBSERVABILITY (Vision staff only in my permissions)
    # -------------------------------------------------------------------------
    path(
        "auth-attempts/",
        AuthAttemptViewSet.as_view({"get": "list"}),
        name="auth-attempts-list",
    ),
    path(
        "lockouts/",
        AccountLockoutViewSet.as_view({"get": "list"}),
        name="lockouts-list",
    ),
    path(
        "lockouts/unlock/",
        AccountLockoutViewSet.as_view({"post": "unlock"}),
        name="lockouts-unlock",
    ),
    path(
        "revoked-tokens/",
        RevokedTokenViewSet.as_view({"get": "list"}),
        name="revoked-tokens-list",
    ),
    path(
        "suspicious-events/",
        SuspiciousLoginEventViewSet.as_view({"get": "list"}),
        name="suspicious-events-list",
    ),
    path(
        "auth-events/",
        AuthEventLogViewSet.as_view({"get": "list"}),
        name="auth-events-list",
    ),
]
