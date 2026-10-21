"""Routes for the pupil portal, mounted at ``/v1/portal/student/``.

``notices/read-all/`` is declared before ``notices/<int:pk>/`` so it resolves
as itself.
"""
from django.urls import include, path

from .views import (
    AttendanceView,
    FeesView,
    HomeView,
    PayInvoiceView,
    PaymentHistoryView,
    ReceiptView,
    MeView,
    NoticeDetailView,
    NoticeFeedView,
    NoticeReadAllView,
    NoticeReadView,
    ProfilePhotoView,
    ProfileView,
    ReportCardView,
    ResultsView,
    SessionResultsView,
    StudentAppIconView,
    StudentAppManifestView,
    TimetableView,
    TransferAccountView,
    TransferDepositsView,
)

urlpatterns = [
    path("me/", MeView.as_view(), name="student-portal-me"),
    # Read with no session, by the phone installing the app.
    path(
        "app/<slug:slug>/manifest.webmanifest", StudentAppManifestView.as_view(),
        name="student-app-manifest",
    ),
    path(
        "app/<slug:slug>/icons/<str:name>", StudentAppIconView.as_view(),
        name="student-app-icon",
    ),
    path("home/", HomeView.as_view(), name="student-portal-home"),
    path("profile/", ProfileView.as_view(), name="student-portal-profile"),
    path(
        "profile/photo/", ProfilePhotoView.as_view(),
        name="student-portal-profile-photo",
    ),
    path("timetable/", TimetableView.as_view(), name="student-portal-timetable"),
    path("attendance/", AttendanceView.as_view(), name="student-portal-attendance"),
    path("fees/", FeesView.as_view(), name="student-portal-fees"),
    path("fees/payments/", PaymentHistoryView.as_view(), name="student-portal-payments"),
    path(
        "fees/payments/<int:payment_ref>/receipt/", ReceiptView.as_view(),
        name="student-portal-receipt",
    ),
    path(
        "fees/invoices/<int:invoice_ref>/pay/", PayInvoiceView.as_view(),
        name="student-portal-pay",
    ),
    path(
        "fees/transfer-account/", TransferAccountView.as_view(),
        name="student-portal-transfer-account",
    ),
    path(
        "fees/transfer-account/deposits/", TransferDepositsView.as_view(),
        name="student-portal-transfer-deposits",
    ),
    path("results/", ResultsView.as_view(), name="student-portal-results"),
    path(
        "results/session/", SessionResultsView.as_view(),
        name="student-portal-results-session",
    ),
    path(
        "results/report-card/", ReportCardView.as_view(),
        name="student-portal-report-card",
    ),
    path("notices/", NoticeFeedView.as_view(), name="student-portal-notices"),
    path(
        "notices/read-all/", NoticeReadAllView.as_view(),
        name="student-portal-notices-read-all",
    ),
    path(
        "notices/<int:pk>/", NoticeDetailView.as_view(),
        name="student-portal-notice",
    ),
    path(
        "notices/<int:pk>/read/", NoticeReadView.as_view(),
        name="student-portal-notice-read",
    ),
    path("messages/", include("schools.vs_messages.portal_urls_student")),
]
