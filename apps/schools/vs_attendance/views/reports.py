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
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType
from vs_finance.exports import EXPORT_FORMATS, render
from schools.vs_academics.models import SchoolClass
from vs_rbac.evaluator import has_permission
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import PERM_REGISTER_VIEW, PERM_REPORT_EXPORT, PERM_REPORT_VIEW
from ..exceptions import NotYourClass
from ..serializers import ClassChronicSerializer, ReportSerializer
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


class ClassChronicView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/classes/<class_id>/chronic/

    The pupils of one class whose rate for the term (``?term=``, the current
    one by default) is below the school's threshold: the report's chronic list,
    narrowed to the class, counting only the days they spent in it.

    A reader of the reports may ask about any class in their branches. The
    class's own teacher may ask about it with the register key alone, because
    knowing which of their own pupils are slipping is part of teaching them;
    any other class is refused. A class of another school answers 404.

    docstring-name: Chronic absence in one class
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request, class_id):
        school_class = (
            SchoolClass.objects.filter(tenant=self.tenant, pk=class_id, is_active=True)
            .select_related("class_teacher").first()
        )
        if school_class is None:
            raise NotFound("No such class at this school.")
        leads = (
            school_class.class_teacher is not None
            and school_class.class_teacher.user_id == request.user.pk
        )
        if not (leads or has_permission(request.user, PERM_REPORT_VIEW, tenant=self.tenant)):
            raise NotYourClass(
                f"You can see who is slipping only in the class you lead. "
                f"Ask an administrator about {school_class.name}.",
            )
        body = build_report(
            self.tenant, request.user, term_id=_term_id(request), class_ids={school_class.pk},
        )
        body["school_class"] = {"id": school_class.pk, "name": school_class.name}
        return success_response(
            data=ClassChronicSerializer(body, context={"request": request}).data,
        )
