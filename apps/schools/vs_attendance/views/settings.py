"""The school's attendance settings and its list of excused-absence reasons.

``GET /v1/attendance/settings/`` needs ``attendance.register.view``, because the
guardian-notice switch, the edit window and the chronic threshold shape every
register screen and a teacher is told them. ``PATCH`` needs
``attendance.settings.update`` and a caller whose reach is the whole school,
because the settings and the reasons bind every branch: a branch-bound caller
holding the key is refused with a 403 (``SHARED_RECORD_READ_ONLY``) and nothing
is written.

The reasons live at ``/v1/attendance/reason-codes/``. A reason is never
deleted, so the only ways to take one out of use are ``retire`` and its undo,
``restore``. A reason of another school answers 404.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from schools.vs_schools.settings_field_access import assert_settings_write, settings_body
from vs_rbac.scoping import assert_caller_may_configure

from ..constants import PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE
from ..serializers import (
    ReasonCodeSerializer,
    ReasonCodeWriteSerializer,
    SettingsWriteSerializer,
)
from ..services import configuration, reasons
from .base import AttendanceViewMixin

_ONLY_WHOLE_SCHOOL = (
    "Only a school-wide administrator can change the school's attendance "
    "settings, because they apply to every branch."
)
_SETTINGS_FIELDS = {name: name for name in (
    "guardian_notices", "chronic_threshold", "lesson_marking_levels", "edit_window_days",
)}
_REASON_FIELDS = {
    "name": "reason_code_name", "counts_as_authorised": "counts_as_authorised",
    "requires_note": "requires_note", "retired": "retired",
}


def _settings_body(request, tenant, rules=None):
    """Filter settings and the per-level echo of lesson marking."""
    body = settings_body(request, configuration.settings_body(tenant, rules), _SETTINGS_FIELDS)
    if "lesson_marking_levels" not in body:
        body["levels"] = [
            {key: value for key, value in row.items() if key != "lesson_marking"}
            for row in body["levels"]
        ]
    return body


class _ConfigurationView(AttendanceViewMixin, APIView):
    """Reads need the register key, writes the settings key and a whole-school reach."""

    rbac_permission = PERM_REGISTER_VIEW
    write_methods = ("POST", "PATCH", "PUT", "DELETE")

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SETTINGS_UPDATE if self.request.method in self.write_methods
            else PERM_REGISTER_VIEW
        )
        return super().get_permissions()

    def assert_may_configure(self):
        assert_caller_may_configure(
            self.request.user, self.tenant, message=_ONLY_WHOLE_SCHOOL,
        )

    def render_reason(self, code) -> dict:
        row = reasons.reason_code_row(self.tenant, code)
        return ReasonCodeSerializer(row, context={"request": self.request}).data


class SettingsView(_ConfigurationView):
    """GET, PATCH /v1/attendance/settings/

    PATCH takes any of ``guardian_notices``, ``chronic_threshold``,
    ``lesson_marking_levels`` and ``edit_window_days`` and answers with the GET
    body. The levels travel with the school's levels of the active year, each
    saying whether it marks every lesson, and with the school, year and term
    the settings are for.

    docstring-name: Attendance settings
    """

    def get(self, request):
        return success_response(data=_settings_body(request, self.tenant))

    def patch(self, request):
        self.assert_may_configure()
        writer = SettingsWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        assert_settings_write(
            request, writer.validated_data, _SETTINGS_FIELDS,
            current=configuration.settings_body(self.tenant),
        )
        rules = configuration.write_settings(
            self.tenant, request.user, dict(writer.validated_data),
        )
        return success_response(
            "Attendance settings saved.", _settings_body(request, self.tenant, rules),
        )


class ReasonCodeListCreateView(_ConfigurationView):
    """GET, POST /v1/attendance/reason-codes/

    ``GET`` lists every reason, retired ones included, with how many marks used
    each this term; ``?active=1`` lists only the reasons a drawer offers.

    docstring-name: Reasons for excused absences
    """

    def get(self, request):
        active_only = request.query_params.get("active") in ("1", "true", "True")
        rows = reasons.reason_code_rows(self.tenant, active_only=active_only)
        return success_response(
            data=ReasonCodeSerializer(rows, many=True, context={"request": request}).data,
        )

    def post(self, request):
        self.assert_may_configure()
        writer = ReasonCodeWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        assert_settings_write(request, writer.validated_data, _REASON_FIELDS)
        code = reasons.create_reason_code(self.tenant, request.user, **writer.validated_data)
        return success_response("Reason added.", self.render_reason(code), status=201)


class ReasonCodeDetailView(_ConfigurationView):
    """PATCH /v1/attendance/reason-codes/<id>/

    Renames a reason or changes whether it asks for a note or counts as
    authorised.

    docstring-name: A reason for excused absences
    """

    def patch(self, request, reason_id):
        self.assert_may_configure()
        code = reasons.get_reason_code(self.tenant, reason_id)
        writer = ReasonCodeWriteSerializer(data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        assert_settings_write(
            request, writer.validated_data, _REASON_FIELDS,
            current=reasons.reason_code_row(self.tenant, code),
        )
        code = reasons.update_reason_code(
            self.tenant, request.user, code, dict(writer.validated_data),
        )
        return success_response("Reason saved.", self.render_reason(code))


class ReasonCodeRetireView(_ConfigurationView):
    """POST /v1/attendance/reason-codes/<id>/retire/

    docstring-name: Retire a reason for excused absences
    """

    def post(self, request, reason_id):
        self.assert_may_configure()
        assert_settings_write(request, {"retired": True}, _REASON_FIELDS)
        code = reasons.retire_reason_code(
            self.tenant, request.user, reasons.get_reason_code(self.tenant, reason_id),
        )
        return success_response("Reason retired.", self.render_reason(code))


class ReasonCodeRestoreView(_ConfigurationView):
    """POST /v1/attendance/reason-codes/<id>/restore/

    docstring-name: Restore a reason for excused absences
    """

    def post(self, request, reason_id):
        self.assert_may_configure()
        assert_settings_write(request, {"retired": False}, _REASON_FIELDS)
        code = reasons.restore_reason_code(
            self.tenant, request.user, reasons.get_reason_code(self.tenant, reason_id),
        )
        return success_response("Reason restored.", self.render_reason(code))
