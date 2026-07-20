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

from .views.jobs import MyTasksSummaryView, MyTasksView
from .views import (
    # Auth
    LoginView,
    LogoutView,
    SpecialLoginPreviewView,
    PasswordResetPreviewView,
    TokenRefreshView,
    CurrentUserView,
    MySecurityStatsView,
    MyPasswordResetsView,
    # Activation — UUID-based, no token
    ActivationPreviewView,
    ActivationView,
    InvitationResendView,
    # Password
    PasswordPolicyView,
    PasswordChangeView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
    AdminPasswordResetView,
    PasswordResetListView,
    RevokePasswordResetView,
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
    # Platform staff profiles
    PlatformStaffProfileViewSet,
    # Organogram
    OrgNodeViewSet,
    PositionViewSet,
    PositionAssignmentViewSet,
    MatrixReportViewSet,
)

# ── Router-registered viewsets ────────────────────────────────────────────────
router = DefaultRouter()
router.register(r'users',             UserAccountViewSet,         basename='users')
router.register(r'sessions',          SessionViewSet,             basename='sessions')
router.register(r'auth-attempts',     AuthAttemptViewSet,         basename='auth-attempts')
router.register(r'account-lockouts',  AccountLockoutViewSet,      basename='account-lockouts')
router.register(r'auth-events',       AuthEventLogViewSet,        basename='auth-events')
router.register(r'platform-staff-profiles', PlatformStaffProfileViewSet, basename='platform-staff-profiles')
router.register(r'organogram/nodes',        OrgNodeViewSet,             basename='org-nodes')
router.register(r'organogram/positions',    PositionViewSet,            basename='org-positions')
router.register(r'organogram/assignments',  PositionAssignmentViewSet,  basename='org-assignments')
router.register(r'organogram/matrix-reports', MatrixReportViewSet,      basename='org-matrix-reports')

urlpatterns = [

    # ── Authentication ────────────────────────────────────────────────────────
    path('auth/login/',                         LoginView.as_view(),               name='auth-login'),
    path('auth/logout/',                        LogoutView.as_view(),               name='auth-logout'),
    path('auth/token/refresh/',                 TokenRefreshView.as_view(),         name='auth-token-refresh'),
    path('auth/me/',                            CurrentUserView.as_view(),          name='auth-me'),
    path('me/tasks/',                           MyTasksView.as_view(),              name='me-tasks'),
    path('me/tasks/summary/',                   MyTasksSummaryView.as_view(),       name='me-tasks-summary'),
    path('auth/me/stats/',                      MySecurityStatsView.as_view(),      name='auth-me-stats'),
    path('auth/me/password-resets/',            MyPasswordResetsView.as_view(),     name='auth-me-password-resets'),
    path('auth/special_login/preview/',         SpecialLoginPreviewView.as_view(),  name='special-login-preview'),

    # ── Activation ────────────────────────────────────────────────────────────
    # GET  → ActivationPreviewView (pre-fill form)
    # POST → ActivationView (set password, activate)
    path('auth/activate/<uuid:activation_key>/preview/',  ActivationPreviewView.as_view(), name='auth-activate-preview'),
    path('auth/activate/<uuid:activation_key>/',          ActivationView.as_view(),        name='auth-activate'),

    # ── Password ──────────────────────────────────────────────────────────────
    path('auth/password/policy/',            PasswordPolicyView.as_view(),          name='password-policy'),
    path('auth/password/change/',            PasswordChangeView.as_view(),          name='password-change'),
    path('auth/password/reset/request/',     PasswordResetRequestView.as_view(),    name='password-reset-request'),
    path('auth/reset-password/<uuid:activation_key>/preview/', PasswordResetPreviewView.as_view(),    name='password-reset-preview'),
    path('auth/password/reset/<uuid:activation_key>/confirm/', PasswordResetConfirmView.as_view(),    name='password-reset-confirm'),

    # ── User management actions ───────────────────────────────────────────────
    path('<str:user_id>/email/change/',   UserEmailChangeView.as_view(),   name='user-email-change'),
    path('<str:user_id>/invite/resend/',  InvitationResendView.as_view(),  name='user-invite-resend'),
    path('<str:user_id>/suspend/',        UserSuspendView.as_view(),       name='user-suspend'),
    path('<str:user_id>/reactivate/',     UserReactivateView.as_view(),    name='user-reactivate'),
    path('<str:user_id>/unlock/',         UserUnlockView.as_view(),        name='user-unlock'),
    path('<str:user_id>/password-reset/', AdminPasswordResetView.as_view(),name='user-password-reset'),
    path('password-resets/',              PasswordResetListView.as_view(),  name='password-reset-list'),
    path('password-resets/<int:pk>/revoke/', RevokePasswordResetView.as_view(), name='password-reset-revoke'),

    # ── Router URLs ───────────────────────────────────────────────────────────
    path('', include(router.urls)),
]