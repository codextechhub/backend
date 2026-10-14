"""Performance analytics over published results, and the two tables that export.

``GET /v1/gradebook/analytics/`` needs ``gradebook.analytics.view``;
``GET /v1/gradebook/analytics/export/`` needs ``gradebook.analytics.export``, and
holding the first alone is not enough. Both take the term as ``?term=<id>`` (the
current term when absent) and ``?branch=<id>``; a branch the caller cannot see, or
one of another school, and a term of another school, answer 404. The export names
the table as ``?table=subjects|movers`` and the file type as ``?export=csv|xlsx``
(``export`` and not ``format``, which DRF reserves; CSV by default); anything else
is a 400. What the figures mean is in :mod:`..services.analytics`.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType
from vs_finance.exports import render
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import PERM_ANALYTICS_EXPORT, PERM_ANALYTICS_VIEW
from ..serializers import AnalyticsSerializer
from ..services import analytics, audit
from ..services.analytics_export import TABLES, build_table, filename_for
from .base import GradebookViewMixin

#: The file types an analytics table can be downloaded as.
FILE_TYPES = ("csv", "xlsx")


class _AnalyticsView(GradebookViewMixin, APIView):
    def payload(self, term, branch) -> dict:
        """The analytics as the caller may read them."""
        raw = analytics.report(self.tenant, self.request.user, term, branch)
        return dict(AnalyticsSerializer(raw, context={"request": self.request}).data)


class AnalyticsView(_AnalyticsView):
    """GET /v1/gradebook/analytics/?term=&branch=

    How a term's published results read, never a judgement on a teacher: each
    subject in each published class (average, grade spread, pass rate, number
    marked); a subject offered by several published classes beside the gap
    between them; each student's change in overall average against their most
    recent earlier published term, biggest fall first, with a student who has no
    earlier published result left out; and, where the school ranks, the top of each
    class. ``scope`` names the published classes the figures come from and is
    empty, with every list, when nothing is published. A closed Field Access
    switch removes the figures it governs (``average``, ``spread``, ``pass_rate``
    and ``gap`` follow the sheet's class figures; ``from``, ``to`` and ``change``
    the overall average; ``tops`` the position and the overall average, the
    stricter winning).

    docstring-name: Performance analytics
    """

    rbac_permission = PERM_ANALYTICS_VIEW

    def get(self, request):
        return success_response(
            data=self.payload(self.term_from_query(), self.branch_from_query()),
        )


class AnalyticsExportView(_AnalyticsView):
    """GET /v1/gradebook/analytics/export/?table=subjects|movers&term=&branch=&export=csv|xlsx

    One table of the analytics as a file, cut from the payload the screen is
    drawn from, so the two cannot disagree. A column the caller's Field Access
    closes is left out. Each download is recorded in the audit trail with who took
    which table for which term.

    docstring-name: Performance analytics export
    """

    rbac_permission = PERM_ANALYTICS_EXPORT

    def get(self, request):
        table = request.query_params.get("table")
        if table not in TABLES:
            raise ValidationError({"table": "Choose which table to export: subjects or movers."})
        file_type = (request.query_params.get("export") or "csv").lower()
        if file_type not in FILE_TYPES:
            raise ValidationError({"export": f"Choose a file type: {', '.join(FILE_TYPES)}."})
        term, branch = self.term_from_query(), self.branch_from_query()
        report = self.payload(term, branch)
        narrowed = visible_branch_ids(request.user, self.tenant) is not WHOLE_TENANT
        sheet = build_table(request, report, table, branch=branch, narrowed=narrowed)
        body, content_type, extension = render(sheet, file_type)
        audit.record(
            tenant=self.tenant, user=request.user, branch=branch,
            entity_type="GradebookAnalytics", entity_id=term.pk, entity_label=sheet.title,
            action_type=AuditActionType.EXPORT_COMPLETED,
            summary=(
                f"{request.user.full_name} exported {sheet.title.lower()} for "
                f"{term.name} as {extension}."
            ),
            table=table, term_id=term.pk, file_type=extension, rows=len(sheet.rows),
        )
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = (
            f'attachment; filename="{filename_for(table, report["term"])}.{extension}"'
        )
        return response
