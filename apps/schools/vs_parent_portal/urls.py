"""Routes for the parent portal, mounted at ``/v1/portal/parent/``.

``notices/read-all/`` is declared before ``notices/<int:pk>/`` so it resolves
as itself.
"""
from django.urls import path

from .views import (
    ParentAppIconView,
    ParentAppManifestView,
    AttendanceView,
    ChildRecordView,
    FeesView,
    HomeView,
    InvoiceDetailView,
    MeView,
    NoticeDetailView,
    NoticeFeedView,
    NoticeReadAllView,
    NoticeReadView,
    PayInvoiceView,
    PaymentHistoryView,
    ProfilePhotoView,
    ProfileView,
    ReceiptView,
    ReportCardView,
    ResultsView,
    SessionResultsView,
    TimetableView,
    TransferAccountView,
    TransferDepositsView,
)

urlpatterns = [
    path(
        "app/<slug:slug>/manifest.webmanifest", ParentAppManifestView.as_view(),
        name="parent-app-manifest",
    ),
    path("app/<slug:slug>/icons/<str:name>", ParentAppIconView.as_view(), name="parent-app-icon"),
    path("me/", MeView.as_view(), name="parent-portal-me"),
    path("profile/", ProfileView.as_view(), name="parent-portal-profile"),
    path(
        "profile/photo/", ProfilePhotoView.as_view(),
        name="parent-portal-profile-photo",
    ),
    path("home/", HomeView.as_view(), name="parent-portal-home"),
    path("children/<int:child_id>/", ChildRecordView.as_view(), name="parent-portal-child"),
    path(
        "children/<int:child_id>/timetable/", TimetableView.as_view(),
        name="parent-portal-timetable",
    ),
    path(
        "children/<int:child_id>/attendance/", AttendanceView.as_view(),
        name="parent-portal-attendance",
    ),
    path(
        "children/<int:child_id>/results/", ResultsView.as_view(),
        name="parent-portal-results",
    ),
    path(
        "children/<int:child_id>/results/session/", SessionResultsView.as_view(),
        name="parent-portal-results-session",
    ),
    path(
        "children/<int:child_id>/results/report-card/", ReportCardView.as_view(),
        name="parent-portal-report-card",
    ),
    path("fees/", FeesView.as_view(), name="parent-portal-fees"),
    path("fees/payments/", PaymentHistoryView.as_view(), name="parent-portal-payments"),
    path(
        "fees/payments/<int:payment_ref>/receipt/", ReceiptView.as_view(),
        name="parent-portal-receipt",
    ),
    path(
        "fees/invoices/<int:invoice_ref>/", InvoiceDetailView.as_view(),
        name="parent-portal-invoice",
    ),
    path(
        "fees/invoices/<int:invoice_ref>/pay/", PayInvoiceView.as_view(),
        name="parent-portal-pay",
    ),
    path(
        "children/<int:child_id>/fees/transfer-account/", TransferAccountView.as_view(),
        name="parent-portal-transfer-account",
    ),
    path(
        "children/<int:child_id>/fees/transfer-account/deposits/",
        TransferDepositsView.as_view(), name="parent-portal-transfer-deposits",
    ),
    path("notices/", NoticeFeedView.as_view(), name="parent-portal-notices"),
    path(
        "notices/read-all/", NoticeReadAllView.as_view(),
        name="parent-portal-notices-read-all",
    ),
    path("notices/<int:pk>/", NoticeDetailView.as_view(), name="parent-portal-notice"),
    path(
        "notices/<int:pk>/read/", NoticeReadView.as_view(),
        name="parent-portal-notice-read",
    ),
]
