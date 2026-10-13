"""The results hubs: the school's, and a teacher's own.

``GET /v1/gradebook/hub/`` needs ``gradebook.hub.view``; ``GET /v1/gradebook/hub/mine/``
needs ``gradebook.sheet.view``. Both take the term as ``?term=<id>`` (the current
term when absent). The school's hub also takes ``?branch=<id>``; a branch the
caller cannot see, or one of another school, answers 404.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.evaluator import has_permission

from ..constants import PERM_HUB_VIEW, PERM_SEE_LOCKED_ACCOUNTS, PERM_SHEET_VIEW
from ..serializers import AdminHubSerializer, TeacherHubSerializer
from ..services import hubs
from .base import GradebookViewMixin


class AdminHubView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/hub/?term=&branch=

    The term, its planned results day and its end; the six counts (sheets in of
    due, classes compiled, awaiting approval, published, held); each class with
    its stage and class teacher; and every due sheet not yet in with the one
    reason the platform knows. A teacher who cannot sign in is named as the
    reason only to a caller who may unlock staff accounts
    (``school.administrators.reactivate``), who is also given the staff record to
    open; everyone else sees how many of the roll are entered.

    docstring-name: Results hub
    """

    rbac_permission = PERM_HUB_VIEW

    def get(self, request):
        body = hubs.admin_hub(
            self.tenant, request.user, self.term_from_query(), self.branch_from_query(),
            show_locked_accounts=has_permission(
                request.user, PERM_SEE_LOCKED_ACCOUNTS, tenant=self.tenant,
            ),
        )
        return success_response(
            data=dict(AdminHubSerializer(body, context={"request": request}).data),
        )


class TeacherHubView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/hub/mine/?term=

    The sheets the caller is the main teacher of (entered of roll, examination
    paper, status and what to do next) and their other duties: one for each class
    they lead as class teacher, and one for each department they head that has
    moderated sheets this term.

    docstring-name: My score sheets
    """

    rbac_permission = PERM_SHEET_VIEW

    def get(self, request):
        body = hubs.teacher_hub(self.tenant, request.user, self.term_from_query())
        return success_response(
            data=dict(TeacherHubSerializer(body, context={"request": request}).data),
        )
