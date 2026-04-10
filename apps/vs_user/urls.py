# urls.py
# URL configuration for vs_users.
# Wire into your root urls.py like this:
#
#   from django.urls import path, include
#   urlpatterns = [
#       path('api/v1/', include('vs_users.urls')),
#       ...
#   ]
#
# All routes below are relative to that prefix.

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    # Auth
    LoginView,
    LogoutView,
    TokenRefreshView,
    # Activation — UUID-based, no token
    ActivationPreviewView,
    ActivationView,
    InvitationResendView,
    # Password
    PasswordChangeView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
    AdminPasswordResetView,
    # User management
    UserAccountViewSet,
    UserEmailChangeView,
    UserSuspendView,
    UserReactivateView,
    UserUnlockView,
    # Security & sessions
    SessionViewSet,
    AuthAttemptViewSet,
    AccountLockoutViewSet,
    AuthEventLogViewSet,
    SuspiciousLoginEventViewSet,
)

# ── Router-registered viewsets ────────────────────────────────────────────────
router = DefaultRouter()
router.register(r'users',             UserAccountViewSet,         basename='users')
router.register(r'sessions',          SessionViewSet,             basename='sessions')
router.register(r'auth-attempts',     AuthAttemptViewSet,         basename='auth-attempts')
router.register(r'account-lockouts',  AccountLockoutViewSet,      basename='account-lockouts')
router.register(r'auth-events',       AuthEventLogViewSet,        basename='auth-events')
router.register(r'suspicious-logins', SuspiciousLoginEventViewSet, basename='suspicious-logins')

urlpatterns = [

    # ── Authentication ────────────────────────────────────────────────────────
    path('auth/login/',                  LoginView.as_view(),               name='auth-login'),
    path('auth/logout/',                 LogoutView.as_view(),               name='auth-logout'),
    path('auth/token/refresh/',          TokenRefreshView.as_view(),         name='auth-token-refresh'),

    # ── Activation ────────────────────────────────────────────────────────────
    path('auth/activate/<uuid:user_id>/',    ActivationPreviewView.as_view(), name='auth-activate-preview'),
    path('auth/activate/<uuid:user_id>/',    ActivationView.as_view(),        name='auth-activate'),

    # ── Password ──────────────────────────────────────────────────────────────
    path('auth/password/change/',            PasswordChangeView.as_view(),          name='password-change'),
    path('auth/password/reset/request/',     PasswordResetRequestView.as_view(),    name='password-reset-request'),
    path('auth/password/reset/confirm/',     PasswordResetConfirmView.as_view(),    name='password-reset-confirm'),

    # ── User management actions ───────────────────────────────────────────────
    path('users/<uuid:user_id>/email/',          UserEmailChangeView.as_view(),   name='user-email-change'),
    path('users/<uuid:user_id>/invite/resend/',  InvitationResendView.as_view(),  name='user-invite-resend'),
    path('users/<uuid:user_id>/suspend/',        UserSuspendView.as_view(),       name='user-suspend'),
    path('users/<uuid:user_id>/reactivate/',     UserReactivateView.as_view(),    name='user-reactivate'),
    path('users/<uuid:user_id>/unlock/',         UserUnlockView.as_view(),        name='user-unlock'),
    path('users/<uuid:user_id>/password-reset/', AdminPasswordResetView.as_view(),name='user-password-reset'),

    # ── Router URLs ───────────────────────────────────────────────────────────
    path('', include(router.urls)),
]