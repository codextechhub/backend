from django.urls import path

from .views.corrections import (
    CorrectionApproveView,
    CorrectionListCreateView,
    CorrectionRejectView,
)
from .views.lessons import (
    LessonFillPresentView,
    LessonListView,
    LessonMarkView,
    LessonSubmitView,
    LessonView,
)
from .views.registers import (
    FillPresentView,
    MarkView,
    RegisterView,
    RemindView,
    SubmitView,
)
from .views.reports import ClassChronicView, ReportExportView, ReportView
from .views.settings import (
    ReasonCodeDetailView,
    ReasonCodeListCreateView,
    ReasonCodeRestoreView,
    ReasonCodeRetireView,
    SettingsView,
)
from .views.staff import (
    StaffFillPresentView,
    StaffMarkView,
    StaffRegistersView,
    StaffSubmitView,
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
    path("lessons/", LessonListView.as_view(), name="attendance-lessons"),
    path(
        "lessons/<int:slot_id>/<str:on_date>/",
        LessonView.as_view(), name="attendance-lesson",
    ),
    path(
        "lessons/<int:slot_id>/<str:on_date>/marks/<int:student_id>/",
        LessonMarkView.as_view(), name="attendance-lesson-mark",
    ),
    path(
        "lessons/<int:slot_id>/<str:on_date>/fill-present/",
        LessonFillPresentView.as_view(), name="attendance-lesson-fill-present",
    ),
    path(
        "lessons/<int:slot_id>/<str:on_date>/submit/",
        LessonSubmitView.as_view(), name="attendance-lesson-submit",
    ),
    path(
        "staff-registers/<str:on_date>/",
        StaffRegistersView.as_view(), name="attendance-staff-registers",
    ),
    path(
        "staff-registers/<int:branch_id>/<str:on_date>/marks/<int:staff_id>/",
        StaffMarkView.as_view(), name="attendance-staff-mark",
    ),
    path(
        "staff-registers/<int:branch_id>/<str:on_date>/fill-present/",
        StaffFillPresentView.as_view(), name="attendance-staff-fill-present",
    ),
    path(
        "staff-registers/<int:branch_id>/<str:on_date>/submit/",
        StaffSubmitView.as_view(), name="attendance-staff-submit",
    ),
    path("today/", TodayView.as_view(), name="attendance-today"),
    path("today/mine/", TodayMineView.as_view(), name="attendance-today-mine"),
    path(
        "students/<int:student_id>/",
        StudentAttendanceView.as_view(), name="attendance-student",
    ),
    path("reports/", ReportView.as_view(), name="attendance-reports"),
    path(
        "classes/<int:class_id>/chronic/",
        ClassChronicView.as_view(), name="attendance-class-chronic",
    ),
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
