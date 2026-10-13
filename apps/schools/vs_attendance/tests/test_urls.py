"""The mount, the route names, and the surface attendance is closed to a school not yet live.

Attendance is the daily work of a live school. A school that has not gone live
has no students placed in classes and nothing to register, so no view here
joins the pending-tenant surface and every route answers ``TENANT_NOT_LIVE``
for a pending school.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import get_resolver, resolve, reverse

from core.test_utils import TenantAPIClient
from vs_tenants.models import Tenant

from ..views.corrections import (
    CorrectionApproveView,
    CorrectionListCreateView,
    CorrectionRejectView,
)
from ..views.lessons import (
    LessonFillPresentView,
    LessonListView,
    LessonMarkView,
    LessonSubmitView,
    LessonView,
)
from ..views.registers import (
    FillPresentView,
    MarkView,
    RegisterView,
    RemindView,
    SubmitView,
)
from ..views.reports import ClassChronicView, ReportExportView, ReportView
from ..views.settings import (
    ReasonCodeDetailView,
    ReasonCodeListCreateView,
    ReasonCodeRestoreView,
    ReasonCodeRetireView,
    SettingsView,
)
from ..views.staff import (
    StaffFillPresentView,
    StaffMarkView,
    StaffRegistersView,
    StaffSubmitView,
)
from ..views.students import StudentAttendanceView
from ..views.today import TodayMineView, TodayView
from .base import AttendanceFixture

ROUTES = {
    "attendance-register": (RegisterView, "", {}),
    "attendance-mark": (MarkView, "marks/9/", {"student_id": 9}),
    "attendance-fill-present": (FillPresentView, "fill-present/", {}),
    "attendance-submit": (SubmitView, "submit/", {}),
    "attendance-remind": (RemindView, "remind/", {}),
}

#: The routes that are not under a class and a date: ``(view, path, kwargs)``.
OTHER_ROUTES = {
    "attendance-corrections": (CorrectionListCreateView, "/v1/attendance/corrections/", {}),
    "attendance-correction-approve": (
        CorrectionApproveView, "/v1/attendance/corrections/7/approve/", {"correction_id": 7},
    ),
    "attendance-correction-reject": (
        CorrectionRejectView, "/v1/attendance/corrections/7/reject/", {"correction_id": 7},
    ),
    "attendance-lessons": (LessonListView, "/v1/attendance/lessons/", {}),
    "attendance-lesson": (
        LessonView, "/v1/attendance/lessons/12/2025-10-13/",
        {"slot_id": 12, "on_date": "2025-10-13"},
    ),
    "attendance-lesson-mark": (
        LessonMarkView, "/v1/attendance/lessons/12/2025-10-13/marks/9/",
        {"slot_id": 12, "on_date": "2025-10-13", "student_id": 9},
    ),
    "attendance-lesson-fill-present": (
        LessonFillPresentView, "/v1/attendance/lessons/12/2025-10-13/fill-present/",
        {"slot_id": 12, "on_date": "2025-10-13"},
    ),
    "attendance-lesson-submit": (
        LessonSubmitView, "/v1/attendance/lessons/12/2025-10-13/submit/",
        {"slot_id": 12, "on_date": "2025-10-13"},
    ),
    "attendance-staff-registers": (
        StaffRegistersView, "/v1/attendance/staff-registers/2025-10-13/",
        {"on_date": "2025-10-13"},
    ),
    "attendance-staff-mark": (
        StaffMarkView, "/v1/attendance/staff-registers/3/2025-10-13/marks/9/",
        {"branch_id": 3, "on_date": "2025-10-13", "staff_id": 9},
    ),
    "attendance-staff-fill-present": (
        StaffFillPresentView, "/v1/attendance/staff-registers/3/2025-10-13/fill-present/",
        {"branch_id": 3, "on_date": "2025-10-13"},
    ),
    "attendance-staff-submit": (
        StaffSubmitView, "/v1/attendance/staff-registers/3/2025-10-13/submit/",
        {"branch_id": 3, "on_date": "2025-10-13"},
    ),
    "attendance-today": (TodayView, "/v1/attendance/today/", {}),
    "attendance-today-mine": (TodayMineView, "/v1/attendance/today/mine/", {}),
    "attendance-student": (
        StudentAttendanceView, "/v1/attendance/students/12/", {"student_id": 12},
    ),
    "attendance-reports": (ReportView, "/v1/attendance/reports/", {}),
    "attendance-report-export": (
        ReportExportView, "/v1/attendance/reports/export/", {},
    ),
    "attendance-class-chronic": (
        ClassChronicView, "/v1/attendance/classes/7/chronic/", {"class_id": 7},
    ),
    "attendance-settings": (SettingsView, "/v1/attendance/settings/", {}),
    "attendance-reason-codes": (
        ReasonCodeListCreateView, "/v1/attendance/reason-codes/", {},
    ),
    "attendance-reason-code": (
        ReasonCodeDetailView, "/v1/attendance/reason-codes/5/", {"reason_id": 5},
    ),
    "attendance-reason-code-retire": (
        ReasonCodeRetireView, "/v1/attendance/reason-codes/5/retire/", {"reason_id": 5},
    ),
    "attendance-reason-code-restore": (
        ReasonCodeRestoreView, "/v1/attendance/reason-codes/5/restore/", {"reason_id": 5},
    ),
}
VIEWS = {view for view, _, _ in ROUTES.values()} | {view for view, _, _ in OTHER_ROUTES.values()}


def _walk(pattern):
    if hasattr(pattern, "url_patterns"):
        for child in pattern.url_patterns:
            yield from _walk(child)
    elif hasattr(pattern, "callback"):
        yield pattern


class MountTests(SimpleTestCase):
    def test_every_route_is_mounted_under_v1_attendance(self):
        for name, (view, tail, extra) in ROUTES.items():
            with self.subTest(route=name):
                path = reverse(name, kwargs={"class_id": 4, "on_date": "2025-10-13", **extra})
                self.assertEqual(path, f"/v1/attendance/registers/4/2025-10-13/{tail}")
                self.assertIs(resolve(path).func.cls, view)

    def test_the_other_routes_are_mounted_under_v1_attendance(self):
        for name, (view, path, kwargs) in OTHER_ROUTES.items():
            with self.subTest(route=name):
                self.assertEqual(reverse(name, kwargs=kwargs), path)
                self.assertIs(resolve(path).func.cls, view)

    def test_every_view_of_the_module_is_routed(self):
        routed = {
            entry.callback.cls
            for pattern in get_resolver().url_patterns
            for entry in _walk(pattern)
            if getattr(entry.callback, "cls", None) is not None
            and entry.callback.cls.__module__.startswith("schools.vs_attendance")
        }
        self.assertEqual(routed, VIEWS)

    def test_no_view_joins_the_pending_tenant_surface(self):
        for pattern in get_resolver().url_patterns:
            for entry in _walk(pattern):
                view = getattr(entry.callback, "cls", None)
                if view is not None and view.__module__.startswith("schools.vs_attendance"):
                    with self.subTest(view=view.__name__):
                        self.assertFalse(getattr(view, "pending_tenant_surface", False))

    def test_every_view_names_the_permission_it_demands(self):
        for view in VIEWS:
            with self.subTest(view=view.__name__):
                self.assertTrue(view.rbac_permission)
                self.assertIn("docstring-name:", view.__doc__)


class PendingTenantTests(AttendanceFixture):
    def test_a_school_still_being_set_up_is_refused_every_route(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.PENDING)
        lesson = {"slot_id": 1, "on_date": "2025-10-13"}
        staff = {"branch_id": 1, "on_date": "2025-10-13"}
        responses = (
            self.read(self.admin, self.jss1a),
            self.mark(self.admin, self.jss1a, self.tunde, "P"),
            self.fill(self.admin, self.jss1a),
            self.submit(self.admin, self.jss1a),
            self.remind(self.admin, self.jss1a),
            self.corrections(self.admin),
            self.correct(self.admin, self.jss1a, self.tunde, "P"),
            self.decide(self.admin, 1, "approve"),
            self.today(self.admin),
            TenantAPIClient(self.admin).get(reverse("attendance-lessons")),
            TenantAPIClient(self.admin).get(reverse("attendance-lesson", kwargs=lesson)),
            TenantAPIClient(self.admin).put(
                reverse("attendance-lesson-mark", kwargs={**lesson, "student_id": 9}),
                {"mark": "P"}, format="json",
            ),
            TenantAPIClient(self.admin).post(
                reverse("attendance-lesson-fill-present", kwargs=lesson), {}, format="json",
            ),
            TenantAPIClient(self.admin).post(
                reverse("attendance-lesson-submit", kwargs=lesson), {}, format="json",
            ),
            TenantAPIClient(self.admin).get(
                reverse("attendance-staff-registers", kwargs={"on_date": "2025-10-13"}),
            ),
            TenantAPIClient(self.admin).put(
                reverse("attendance-staff-mark", kwargs={**staff, "staff_id": 9}),
                {"mark": "P"}, format="json",
            ),
            TenantAPIClient(self.admin).post(
                reverse("attendance-staff-fill-present", kwargs=staff), {}, format="json",
            ),
            TenantAPIClient(self.admin).post(
                reverse("attendance-staff-submit", kwargs=staff), {}, format="json",
            ),
            self.today_mine(self.eze),
            self.student_tab(self.admin, self.tunde),
            TenantAPIClient(self.admin).get(reverse("attendance-reports")),
            TenantAPIClient(self.admin).get(
                reverse("attendance-report-export"), {"table": "by_class"},
            ),
            TenantAPIClient(self.admin).get(reverse("attendance-settings")),
            TenantAPIClient(self.admin).get(reverse("attendance-reason-codes")),
        )
        for response in responses:
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(self.code(response), "TENANT_NOT_LIVE")
