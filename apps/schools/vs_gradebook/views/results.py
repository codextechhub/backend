"""Release, report cards and a student's results.

* ``release/``, ``release/<class_id>/`` and ``report-cards/...`` need ``gradebook.release.view``
  and take the term as ``?term=<id>`` (the current term when absent).
* ``students/<student_id>/results/`` needs ``gradebook.result.view``.

A class of another school, or in a branch the caller cannot see, answers 404, and
so does a student of another school or branch. A card exists only for a published
class (``NOT_PUBLISHED``, 409, otherwise). Everything is rendered through the
serializers that carry Field Access, from the frozen snapshot.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_RELEASE_VIEW, PERM_RESULT_VIEW
from ..serializers import (
    ClassCardsSerializer,
    ReleaseSerializer,
    ReportCardSerializer,
    StudentResultsSerializer,
)
from ..services import classbook, reportcards, student_results
from .base import GradebookViewMixin


class _ReleaseView(GradebookViewMixin, APIView):
    rbac_permission = PERM_RELEASE_VIEW

    def open(self, class_id):
        return classbook.open_class(
            self.tenant, self.request.user, class_id, self.term_from_query(),
            rule=classbook.RELEASE,
        )


class ReleaseClassesView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/release/?term=&branch=

    The classes whose release list the caller may open, for choosing one:
    ``{term, branch_dimension, classes}``, each class ``{class, branch, stage,
    published_at}`` in level order. ``stage`` is where the class stands in the
    chain (ENTERING, COMPILING, AWAITING or PUBLISHED); ``branch`` is null at a
    school with one branch. Every active class of the term in the caller's
    branches is listed, published or not.

    docstring-name: Release classes
    """

    rbac_permission = PERM_RELEASE_VIEW

    def get(self, request):
        return success_response(data=reportcards.release_classes(
            self.tenant, request.user, self.term_from_query(), self.branch_from_query(),
        ))


class ReleaseListView(_ReleaseView):
    """GET /v1/gradebook/release/<class_id>/?term=

    The class's release list: ``class``, ``term``, ``stage``, ``published_at``,
    ``class_size``, ``held_count`` and ``rows``, one per student by name. A row is
    ``{student, guardian, result_state, email_state, emailed_at, released_at}``.
    ``result_state`` is NOT_PUBLISHED, HELD or RELEASED; ``email_state`` is null
    before publication, then SENT, NO_EMAIL, HELD, or NOT_SENT for a released
    result whose email has no recorded outcome (never read as sent). ``guardian``
    is ``{name, email}`` of the primary guardian, null when there is none, and
    absent or cut down where Field Access hides it.

    docstring-name: Release list
    """

    def get(self, request, class_id):
        body = reportcards.release_list(self.open(class_id))
        return success_response(
            data=dict(ReleaseSerializer(body, context={"request": request}).data),
        )


class ReportCardView(_ReleaseView):
    """GET /v1/gradebook/report-cards/<class_id>/<student_id>/?term=

    One student's report card, rendered from the snapshot published with the
    class and never recomputed, with the school's present letterhead. Only a
    published class has cards (``NOT_PUBLISHED``, 409); a student the class did
    not publish is a 404. A held student's card is readable by staff and says so
    (``held: true``). Lines for a figure the caller may not read are absent.

    docstring-name: Report card
    """

    def get(self, request, class_id, student_id):
        body = reportcards.one_card(self.open(class_id), student_id, request.user)
        return success_response(
            data=dict(ReportCardSerializer(body, context={"request": request}).data),
        )


class ClassCardsView(_ReleaseView):
    """GET /v1/gradebook/report-cards/<class_id>/?term=

    Every released student's card, in roll order, for printing the class:
    ``{class, term, cards, left_out, left_out_count}``. ``left_out`` names each
    student without a card and why (HELD, or NOT_RELEASED for a student the class
    did not publish). Only a published class can be printed (``NOT_PUBLISHED``).

    docstring-name: Class report cards
    """

    def get(self, request, class_id):
        body = reportcards.class_cards(self.open(class_id), request.user)
        return success_response(
            data=dict(ClassCardsSerializer(body, context={"request": request}).data),
        )


class StudentResultsView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/students/<student_id>/results/

    The Results tab of a student: ``student``, ``guardian`` and ``terms``, every
    term that has begun in the student's school years, newest first, each either
    published (overall average, position where that term ranked, release state
    in words, the subjects with total, grade and remark) or one calm line saying
    nothing has been published. ``history`` lists the student's holds placed and
    lifted, newest first, with the reason where the caller may read it.

    docstring-name: Student results
    """

    rbac_permission = PERM_RESULT_VIEW

    def get(self, request, student_id):
        body = student_results.results_tab(self.tenant, request.user, student_id)
        return success_response(
            data=dict(StudentResultsSerializer(body, context={"request": request}).data),
        )
