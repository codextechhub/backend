"""Corrections to published results: the drawer, raising one, the list, approving and rejecting.

``GET /v1/gradebook/amendments/form/``, ``POST /v1/gradebook/amendments/`` and
the list at the same route are read and raised with ``gradebook.amendment.create``
(the list and the drawer are read by a holder of either amendment key); deciding
needs ``gradebook.amendment.approve``. A class, student, subject or amendment of
another school, or in a branch the caller cannot see, answers 404. The rules a
request is judged by are in :mod:`..services.amendments`.
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from schools.vs_academics.models import AcademicTerm
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    AMENDMENT_FIELD_RESOURCE,
    PERM_AMENDMENT_APPROVE,
    PERM_AMENDMENT_CREATE,
    SHEET_FIELD_RESOURCE,
)
from ..serializers import (
    AmendmentCellsBodySerializer,
    AmendmentDecisionBodySerializer,
    AmendmentFormSerializer,
    AmendmentRaiseBodySerializer,
    AmendmentRowSerializer,
)
from ..services import amendments
from .base import GradebookViewMixin

READERS = [PERM_AMENDMENT_CREATE, PERM_AMENDMENT_APPROVE]


def _sent(request, names) -> list:
    """The names in *names* the request body carries, however empty their values."""
    body = request.data
    return [name for name in names if isinstance(body, Mapping) and name in body]


class _AmendmentView(GradebookViewMixin, APIView):
    def row(self, amendment) -> dict:
        """The amendment as the caller may read it."""
        payload = amendments.one_payload(self.tenant, amendment, self.request.user)
        return dict(AmendmentRowSerializer(payload, context={"request": self.request}).data)

    def decision_note(self) -> str:
        """The note sent with a decision, once the caller's right to write it is judged."""
        assert_writable(
            self.request, AMENDMENT_FIELD_RESOURCE, _sent(self.request, ("note", "decision_note")),
            aliases={"note": "decision_note"},
        )
        body = AmendmentDecisionBodySerializer(
            data=self.request.data, context={"request": self.request},
        )
        body.is_valid(raise_exception=True)
        return body.validated_data.get("decision_note") or ""


class AmendmentFormView(_AmendmentView):
    """GET /v1/gradebook/amendments/form/?term=&class=&student=&subject=

    What the drawer shows before a correction is raised: the columns of the
    result as published, the student's marks in the subject (``cells``) with the
    total, percentage, grade and remark they earned, the report card ``version``
    and ``pending``, the correction already waiting for this student's subject
    or null. The ``class``, ``student`` and ``subject`` are required (400 when one is
    missing) and the term is the current one when absent. A
    class that is not published for the term is ``NOT_PUBLISHED``; a subject not
    on the student's card is ``SUBJECT_NOT_TAKEN``. Needs either amendment key.

    docstring-name: Amendment form
    """

    rbac_permission = READERS

    def _id(self, name) -> int:
        raw = self.request.query_params.get(name)
        if not raw:
            raise ValidationError({name: [f"Choose a {name}."]})
        if not (raw.isdigit() and int(raw) < 2**62):
            raise NotFound(f"No such {name} at this school.")
        return int(raw)

    def get(self, request):
        target = amendments.open_target(
            self.tenant, request.user, self._id("class"), self.term_from_query(),
            self._id("student"), self._id("subject"),
        )
        body = dict(AmendmentFormSerializer(
            amendments.form(target), context={"request": request},
        ).data)
        return success_response(data=body)


class AmendmentsView(_AmendmentView):
    """GET, POST /v1/gradebook/amendments/

    ``GET ?term=&branch=&status=&page=`` lists the term's corrections in the
    caller's branches, waiting ones first and then newest first, paginated. Each
    row carries the student, class, subject, ``amendment_reason``, who raised it
    and when, how it was decided (``decided_by``, ``decided_at``,
    ``decision_note``), the report card ``version`` an approval produced, and
    ``before`` and ``after`` (the subject's cells with the total and grade they
    earn). ``status`` is PENDING, APPROVED or REJECTED. ``can_decide`` is true
    for a correction still waiting that the caller did not raise (deciding also
    needs ``gradebook.amendment.approve``). The answer also carries
    ``branch_dimension``; ``branch`` is null on a row at a school with one
    branch. Needs either amendment key.

    ``POST`` raises a correction with ``gradebook.amendment.create``:
    ``{"term": id, "class": id, "student": id, "subject": id, "reason": "...",
    "cells": [{"component": id, "mark": int|null, "marker": "ABS"|"EXC"|null}]}``.
    ``cells`` names the columns being changed. Each is judged as a sheet save
    judges it (a refusal is a 422 whose ``error.detail.cells`` lists each cell as
    ``{student, component, code, message}``), and a published result has a value
    in every column, so a blank cell is ``BLANK_CELL``. Refused with
    ``NOT_PUBLISHED``, ``SUBJECT_NOT_TAKEN``, ``REASON_REQUIRED``,
    ``NOTHING_CHANGED`` (the marks are the published ones) and
    ``AMENDMENT_PENDING`` (one correction waits at a time for a student's subject
    in a term). A role that may not write marks or the reason is refused the
    request. Answers 201 with the amendment.

    docstring-name: Amendments
    """

    rbac_permission = READERS

    def get_permissions(self):
        self.rbac_permission = PERM_AMENDMENT_CREATE if self.request.method == "POST" else READERS
        return super().get_permissions()

    def get(self, request):
        query = amendments.listing(
            self.tenant, request.user, self.term_from_query(), self.branch_from_query(),
            request.query_params.get("status") or None,
        )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(query, request, view=self)
        payload = amendments.rows_payload(self.tenant, page, request.user)
        rows = AmendmentRowSerializer(
            payload["rows"], many=True, context={"request": request},
        ).data
        response = paginator.get_paginated_response(rows)
        response.data["branch_dimension"] = payload["branch_dimension"]
        return response

    def post(self, request):
        assert_writable(
            request, AMENDMENT_FIELD_RESOURCE, _sent(request, ("reason", "amendment_reason")),
            aliases={"reason": "amendment_reason"},
        )
        assert_writable(request, SHEET_FIELD_RESOURCE, ["cells"])
        head = AmendmentRaiseBodySerializer(data=request.data, context={"request": request})
        head.is_valid(raise_exception=True)
        marks = AmendmentCellsBodySerializer(data=request.data, context={"request": request})
        marks.is_valid(raise_exception=True)
        data = head.validated_data

        term = AcademicTerm.objects.filter(tenant=self.tenant, pk=data["term"]).first()
        if term is None:
            raise NotFound("No such term at this school.")
        target = amendments.open_target(
            self.tenant, request.user, data["class"], term, data["student"], data["subject"],
        )
        amendment = amendments.raise_amendment(
            target, data.get("amendment_reason"), marks.validated_data["cells"],
        )
        return success_response("Correction asked for.", self.row(amendment), status=201)


class AmendmentApproveView(_AmendmentView):
    """POST /v1/gradebook/amendments/<id>/approve/

    Body ``{"note": "..."}``, optional. Applies the correction: the sheet's
    marks take the corrected cells, the student's report card is re-issued as the
    next version with the earlier one kept, and only that student's card changes
    (their position is worked out again against the classmates' published
    averages, and the class figures of the amended subject for this card from the
    classmates' published totals and the new total). When the result was released
    the primary guardian is told an updated card is ready. Needs
    ``gradebook.amendment.approve``. The person who raised the correction cannot
    decide it (``SELF_DECISION``, 403); one already decided is ``NOT_PENDING``.
    Answers with the amendment.

    docstring-name: Approve a correction
    """

    rbac_permission = PERM_AMENDMENT_APPROVE

    def post(self, request, pk):
        note = self.decision_note()
        found = amendments.open_for_decision(self.tenant, request.user, pk)
        decided = amendments.approve(self.tenant, request.user, found, note)
        return success_response("Correction approved.", self.row(decided))


class AmendmentRejectView(_AmendmentView):
    """POST /v1/gradebook/amendments/<id>/reject/

    Body ``{"note": "..."}``, required (``NOTE_REQUIRED``). Closes the correction
    without changing the sheet or the report card, and keeps the note. Needs
    ``gradebook.amendment.approve``. The person who raised the correction cannot
    decide it (``SELF_DECISION``, 403); one already decided is ``NOT_PENDING``.
    Answers with the amendment.

    docstring-name: Reject a correction
    """

    rbac_permission = PERM_AMENDMENT_APPROVE

    def post(self, request, pk):
        note = self.decision_note()
        found = amendments.open_for_decision(self.tenant, request.user, pk)
        decided = amendments.reject(self.tenant, request.user, found, note)
        return success_response("Correction rejected.", self.row(decided))
