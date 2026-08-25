"""The structure tree and the overview.

Both are single documents rather than paginated lists, and both have their
query count asserted, because an overview that costs a query per programme is
the failure mode that only shows up on a real school.
"""
from __future__ import annotations

import datetime as dt

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_tenants.references import resolve_branch_reference

from ..constants import PERM_STRUCTURE_VIEW
from ..models import AcademicSession, SessionStatus
from ..services.reads import build_overview, build_tree
from .base import AcademicsViewMixin


class StructureTreeView(AcademicsViewMixin, APIView):
    """GET /v1/academics/structure/tree/?depth=&branch=&session=

    Five levels at ``depth=full``: the session, its programmes, their levels,
    the classes in each and the subjects offered there. The default stops at
    levels with counts, which is the row cap that makes this safe to serve
    unpaginated - the depth, not a page size.

    The session at the root labels the tree rather than filtering it. Nothing
    in this module ties a class or a subject to a year, so a different session
    changes the heading and nothing else; that is stated here because a reader
    would reasonably assume otherwise.

    docstring-name: Academic structure tree
    """

    rbac_permission = PERM_STRUCTURE_VIEW
    pagination_class = None

    def get(self, request):
        params = request.query_params
        full = str(params.get("depth", "")).lower() in ("full", "all", "classes")

        branch = None
        if self.multi_branch and (params.get("branch") or "").strip():
            branch = resolve_branch_reference(
                self.tenant, params["branch"].strip(), "branch",
            )

        session = None
        if (params.get("session") or "").strip():
            session = AcademicSession.objects.filter(
                tenant=self.tenant, pk=params["session"].strip(),
            ).first()
            if session is None:
                raise NotFound("No such session at this school.")
        else:
            session = AcademicSession.objects.filter(
                tenant=self.tenant, status=SessionStatus.ACTIVE,
            ).first()

        rows = build_tree(
            request.user, self.tenant, session=session, branch=branch,
            full=full, multi_branch=self.multi_branch,
        )
        return success_response("Structure retrieved.", data={
            "session": {"id": session.id, "name": session.name} if session else None,
            "depth": "full" if full else "levels",
            "rows": rows,
        })


class OverviewView(AcademicsViewMixin, APIView):
    """GET /v1/academics/overview/?branch=

    ``branch`` narrows the counts, and only the counts - see build_overview for
    why the live year is not filtered. It is accepted here so a screen showing a
    branch picker above these numbers is not showing a filter that does nothing.

    docstring-name: Academic structure overview
    """

    rbac_permission = PERM_STRUCTURE_VIEW
    pagination_class = None

    def get(self, request):
        branch = None
        if self.multi_branch and (request.query_params.get("branch") or "").strip():
            branch = resolve_branch_reference(
                self.tenant, request.query_params["branch"].strip(), "branch",
            )
        data = build_overview(
            request.user, self.tenant,
            today=dt.date.today(), multi_branch=self.multi_branch, branch=branch,
        )
        return success_response("Overview retrieved.", data=data)
