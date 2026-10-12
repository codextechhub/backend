"""The attendance report.

``GET /v1/attendance/reports/?term=<id>&branch=<id>`` needs
``attendance.report.view``. It covers one term (the one containing today unless
``term`` names another) for the branches the caller can see, narrowed by
``branch``. A branch the caller cannot see answers 404 like one that does not
exist, and so does a term that is not this school's.

``GET /v1/attendance/reports/export/?table=by_class|chronic|not_taken&term=&branch=&export=csv|xlsx|pdf``
downloads one table of the same report and needs ``attendance.report.export``
(holding ``attendance.report.view`` alone is not enough). The parameter is
``export``, not ``format``, because DRF reserves ``?format=``; the default is
CSV. Each download is recorded in the audit trail with who took which table for
which term.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType
from vs_finance.exports import EXPORT_FORMATS, render
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import PERM_REPORT_EXPORT, PERM_REPORT_VIEW
from ..serializers import ReportSerializer
from ..services import audit
from ..services.export import TABLES, build_table, filename_for
from ..services.report import build_report
from .base import AttendanceViewMixin


def _term_id(request):
    """The term named by ``?term=``, or None for the default term."""
    raw = request.query_params.get("term")
    if raw in (None, ""):
        return None
    if str(raw).isdigit():
        return int(raw)
    raise ValidationError({"term": "Choose a term from the list."})


class ReportView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/reports/

    docstring-name: Attendance reports
    """

    rbac_permission = PERM_REPORT_VIEW

    def get(self, request):
        body = build_report(
            self.tenant, request.user, term_id=_term_id(request),
            branch=self.branch_filter(),
        )
        return success_response(
            data=ReportSerializer(body, context={"request": request}).data,
        )


class ReportExportView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/reports/export/

    docstring-name: Attendance report export
    """

    rbac_permission = PERM_REPORT_EXPORT

    def get(self, request):
        table = request.query_params.get("table")
        if table not in TABLES:
            raise ValidationError({
                "table": "Choose which table to export: by_class, chronic or not_taken.",
            })
        fmt = (request.query_params.get("export") or "csv").lower()
        if fmt not in EXPORT_FORMATS:
            raise ValidationError({
                "export": f"Choose a file type: {', '.join(EXPORT_FORMATS)}.",
            })
        branch = self.branch_filter()
        raw = build_report(
            self.tenant, request.user, term_id=_term_id(request), branch=branch,
        )
        report = ReportSerializer(raw, context={"request": request}).data
        narrowed = visible_branch_ids(request.user, self.tenant) is not WHOLE_TENANT
        sheet = build_table(
            request, self.tenant, report, table, branch=branch, narrowed=narrowed,
        )
        body, content_type, extension = render(sheet, fmt)
        term = report.get("term")
        audit.record(
            tenant=self.tenant, user=request.user, branch=branch,
            entity_type="AttendanceReport",
            entity_id=term["id"] if term else "none", entity_label=sheet.title,
            action_type=AuditActionType.EXPORT_COMPLETED,
            summary=(
                f"{request.user.full_name} exported {sheet.title.lower()}"
                f"{' for ' + term['name'] if term else ''} as {extension}."
            ),
            table=table, term_id=term["id"] if term else None, file_type=extension,
            rows=len(sheet.rows),
        )
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = (
            f'attachment; filename="{filename_for(table, term)}.{extension}"'
        )
        return response
