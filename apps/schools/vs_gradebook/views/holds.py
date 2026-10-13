"""Fee holds: who is held, and holding and releasing a result by hand.

``GET /v1/gradebook/holds/`` needs ``gradebook.hold.view``; placing a hold
(``POST`` on the same route) and lifting one need ``gradebook.hold.update``. A
student, class or hold of another school, or outside the caller's branches,
answers 404. The list never carries a score, a grade or an average.
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from schools.vs_academics.models import AcademicTerm
from vs_rbac.field_enforcement import assert_writable, can_read

from ..constants import HOLD_FIELD_RESOURCE, PERM_HOLD_UPDATE, PERM_HOLD_VIEW
from ..serializers import (
    HoldLiftBodySerializer,
    HoldLiftResultSerializer,
    HoldPlaceBodySerializer,
    HoldRowSerializer,
)
from ..services import holds
from .base import GradebookViewMixin

BALANCE_FIELD = "gradebook.hold.balance"


def _sent(request, names) -> list:
    body = request.data
    return [name for name in names if isinstance(body, Mapping) and name in body]


class _HoldView(GradebookViewMixin, APIView):
    def row(self, term, seat) -> dict:
        """The student's holds-list row as the caller may read it."""
        payload = holds.rows_payload(self.tenant, term, [seat])["rows"][0]
        return dict(HoldRowSerializer(payload, context={"request": self.request}).data)


class HoldsView(_HoldView):
    """GET, POST /v1/gradebook/holds/

    ``GET ?term=&class=&branch=&page=`` lists the roll students of the term's
    classes in the caller's branches, by class and student name, paginated. Each
    row is ``{student, class, branch, balance, oldest_unpaid_on, days_owed, hold,
    released, released_on}``: the balance is kobo from Finance (a student Finance
    does not name owes 0), ``hold`` is null unless the result is held, and
    ``released`` rules out a new hold. The response also carries ``as_of`` (when
    Finance was read) and ``finance_available``; when Finance cannot answer every
    balance is null, never zero. No score, grade or average appears.

    ``POST`` places a hold: ``{"student": id, "term": id, "reason": "..."}``. The
    reason is required (``REASON_REQUIRED``); a released result cannot be held
    (``ALREADY_RELEASED``) and a held one cannot be held again
    (``ALREADY_HELD``). Holds are only ever placed by hand. Answers with the
    student's row.

    docstring-name: Fee holds
    """

    rbac_permission = PERM_HOLD_VIEW

    def get_permissions(self):
        self.rbac_permission = PERM_HOLD_UPDATE if self.request.method == "POST" else PERM_HOLD_VIEW
        return super().get_permissions()

    def class_from_query(self):
        raw = self.request.query_params.get("class")
        if not raw:
            return None
        if not (raw.isdigit() and int(raw) < 2**62):
            raise NotFound("No such class at this school.")
        return int(raw)

    def get(self, request):
        term = self.term_from_query()
        seats = holds.seats(
            self.tenant, request.user, term, self.branch_from_query(), self.class_from_query(),
        )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(seats, request, view=self)
        payload = holds.rows_payload(self.tenant, term, page)
        rows = HoldRowSerializer(
            payload["rows"], many=True, context={"request": request},
        ).data
        response = paginator.get_paginated_response(rows)
        if can_read(request, BALANCE_FIELD):
            response.data["as_of"] = payload["as_of"]
            response.data["finance_available"] = payload["finance_available"]
        return response

    def post(self, request):
        assert_writable(request, HOLD_FIELD_RESOURCE, _sent(request, ("reason",)))
        body = HoldPlaceBodySerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        data = body.validated_data
        term = AcademicTerm.objects.filter(tenant=self.tenant, pk=data["term"]).first()
        if term is None:
            raise NotFound("No such term at this school.")
        seat = holds.place(self.tenant, request.user, term, data["student"], data.get("reason"))
        return success_response("Result held.", self.row(term, seat), status=201)


class HoldLiftView(_HoldView):
    """POST /v1/gradebook/holds/<hold_id>/lift/

    Body ``{"note": "..."}``, optional. Lifts the hold. When the student's class
    is already published the result is released by lifting and the guardian is
    told that the report card is ready; before publication lifting only lifts.
    A hold already lifted is ``HOLD_NOT_OPEN``. Answers
    ``{hold, released, row}``.

    docstring-name: Lift a hold
    """

    rbac_permission = PERM_HOLD_UPDATE

    def post(self, request, hold_id):
        assert_writable(
            request, HOLD_FIELD_RESOURCE, _sent(request, ("note", "lift_note")),
            aliases={"note": "lift_note"},
        )
        body = HoldLiftBodySerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        hold, seat, released = holds.lift(
            self.tenant, request.user, hold_id, body.validated_data.get("lift_note"),
        )
        answer = {
            "hold": holds.hold_payload(hold), "released": released,
            "row": holds.rows_payload(self.tenant, hold.term, [seat])["rows"][0],
        }
        return success_response(
            "Hold lifted." if not released else "Hold lifted and the result released.",
            dict(HoldLiftResultSerializer(answer, context={"request": request}).data),
        )
