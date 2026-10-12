from django.urls import path

from .views.corrections import (
    CorrectionApproveView,
    CorrectionListCreateView,
    CorrectionRejectView,
)
from .views.registers import (
    FillPresentView,
    MarkView,
    RegisterView,
    RemindView,
    SubmitView,
)
from .views.reports import ReportExportView, ReportView
from .views.settings import (
    ReasonCodeDetailView,
    ReasonCodeListCreateView,
    ReasonCodeRestoreView,
    ReasonCodeRetireView,
    SettingsView,
)
from .views.students import StudentAttendanceView
from .views.today import TodayMineView, TodayView

urlpatterns = [
    path(
        "registers/<int:class_id>/<str:on_date>/",
        RegisterView.as_view(), name="attendance-register",
    ),
    path(
        "registers/<int:class_id>/<str:on_date>/marks/<int:student_id>/",
        MarkView.as_view(), name="attendance-mark",
    ),
    path(
        "registers/<int:class_id>/<str:on_date>/fill-present/",
        FillPresentView.as_view(), name="attendance-fill-present",
    ),
    path(
        "registers/<int:class_id>/<str:on_date>/submit/",
        SubmitView.as_view(), name="attendance-submit",
    ),
    path(
        "registers/<int:class_id>/<str:on_date>/remind/",
        RemindView.as_view(), name="attendance-remind",
    ),
    path(
        "corrections/",
        CorrectionListCreateView.as_view(), name="attendance-corrections",
    ),
    path(
        "corrections/<int:correction_id>/approve/",
        CorrectionApproveView.as_view(), name="attendance-correction-approve",
    ),
    path(
        "corrections/<int:correction_id>/reject/",
        CorrectionRejectView.as_view(), name="attendance-correction-reject",
    ),
    path("today/", TodayView.as_view(), name="attendance-today"),
    path("today/mine/", TodayMineView.as_view(), name="attendance-today-mine"),
    path(
        "students/<int:student_id>/",
        StudentAttendanceView.as_view(), name="attendance-student",
    ),
    path("reports/", ReportView.as_view(), name="attendance-reports"),
    path(
        "reports/export/",
        ReportExportView.as_view(), name="attendance-report-export",
    ),
    path("settings/", SettingsView.as_view(), name="attendance-settings"),
    path(
        "reason-codes/",
        ReasonCodeListCreateView.as_view(), name="attendance-reason-codes",
    ),
    path(
        "reason-codes/<int:reason_id>/",
        ReasonCodeDetailView.as_view(), name="attendance-reason-code",
    ),
    path(
        "reason-codes/<int:reason_id>/retire/",
        ReasonCodeRetireView.as_view(), name="attendance-reason-code-retire",
    ),
    path(
        "reason-codes/<int:reason_id>/restore/",
        ReasonCodeRestoreView.as_view(), name="attendance-reason-code-restore",
    ),
]
