"""The parent portal's routes.

Every view here answers for the signed-in parent and their own children. A
route that names a child resolves it against the parent's children on every
request, so another family's child and a child that does not exist answer with
the same 404. ``pending_tenant_surface`` is left at its closed default,
because a school that has not gone live has no parents using the portal.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from .permissions import PORTAL_PERMISSIONS, child_of, children_of, linked_guardian


class PortalView(APIView):
    """What every portal view shares: the permissions, and the guardian."""

    permission_classes = PORTAL_PERMISSIONS

    @property
    def guardian(self):
        return linked_guardian(self.request)

    @property
    def children(self):
        return children_of(self.request)


class ChildView(PortalView):
    """A portal view of one child, named by ``child_id`` in the address."""

    @property
    def child(self):
        return child_of(self.request, self.kwargs["child_id"])


class MeView(PortalView):
    """GET /v1/portal/parent/me/

    Who the signed-in parent is, for the portal's shell: their name, their
    school, the year and term the school is in, and each of their children on
    the roll with name, face, class, admission number and the parent's
    relationship to them. A child's branch appears only at a school with more
    than one branch. A school app asks this after sign-in: 200 means a parent,
    404 means anybody else.

    docstring-name: The signed-in parent
    """

    def get(self, request):
        from .services import me

        return success_response(data=me(self.guardian, self.children, request=request))


class ChildRecordView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/

    One child's record as a parent may see it: name, admission number, class,
    year, date of birth, photograph, and the other guardians on the record by
    name and relationship only. Another family's child answers 404.

    docstring-name: My child
    """

    def get(self, request, child_id):
        from .services import child_record

        return success_response(
            data=child_record(self.guardian, self.child, request=request),
        )


class TimetableView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/timetable/

    The child's class timetable for the active year, as the pupil sees it.
    ``published: false`` with no days when the school has not published it.

    docstring-name: My child's timetable
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.services import timetable

        return success_response(data=timetable(self.child))


class AttendanceView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/attendance/?term=<id>

    The child's attendance for a term (the current one by default), as the
    pupil sees it. A term that is not this school's answers 404.

    docstring-name: My child's attendance
    """

    def get(self, request, child_id):
        from schools.vs_attendance.services.student_tab import pupil_attendance

        term = request.query_params.get("term") or None
        if term is not None and not str(term).isdigit():
            raise NotFound("No such term at this school.")
        child = self.child
        return success_response(data=pupil_attendance(child.tenant, child, term_id=term))


class ResultsView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/?term=<id>

    One term's released result for the child (the latest released term by
    default), exactly as the pupil reads it: ``released``, ``withheld`` or
    ``not_published``.

    docstring-name: My child's results
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import term_result

        return success_response(
            data=term_result(self.child, request.query_params.get("term") or None),
        )


class SessionResultsView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/session/?session=<id>

    The child's results across a school year (the latest with a published
    result by default), as the pupil reads them.

    docstring-name: My child's results for the year
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import session_results

        return success_response(
            data=session_results(self.child, request.query_params.get("session") or None),
        )


class ReportCardView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/report-card/?term=<id>

    The released report card for a term, exactly as the gradebook prints it.
    A held or unpublished term answers 404.

    docstring-name: My child's report card
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import report_card

        return success_response(
            data=report_card(
                self.child, request.query_params.get("term") or None, user=request.user,
            ),
        )


def _read_ids(user, notices) -> set:
    from schools.vs_notices.models import NoticeRead

    return set(
        NoticeRead.objects.filter(user=user, notice__in=notices)
        .values_list("notice_id", flat=True)
    )


def _children_for(notices, children, placements) -> dict:
    """For each notice, the first names of the children it is addressed to."""
    from schools.vs_notices.services.notices import children_addressed

    return {
        n.pk: [c.first_name for c in children_addressed(n, children, placements)]
        for n in notices
    }


class _Feed:
    """The notices addressed to any of the parent's children."""

    def feed(self):
        from schools.vs_notices.services.notices import guardian_notices, pupil_placements

        placements = pupil_placements(self.children)
        return guardian_notices(self.children, placements=placements), placements

    def context(self, notices, placements):
        return {
            "read_ids": _read_ids(self.request.user, notices),
            "children_for": _children_for(notices, self.children, placements),
        }


class NoticeFeedView(_Feed, PortalView):
    """GET /v1/portal/parent/notices/

    The published notices addressed to any of the parent's children, newest
    first and each once, marked unread until the parent opens it, with the
    unread count for the badge and, on each notice, the first names of the
    children it is for. Read marks belong to the parent's account.

    docstring-name: My notices as a parent
    """

    def get(self, request):
        from core.pagination import XVSPagination

        from .serializers import ParentNoticeSerializer

        notices, placements = self.feed()
        unread = notices.exclude(reads__user=request.user).count()
        paginator = XVSPagination()
        page = paginator.paginate_queryset(notices, request, view=self)
        data = ParentNoticeSerializer(
            page, many=True, context=self.context(page, placements),
        ).data
        response = paginator.get_paginated_response(data)
        response.data["unread_count"] = unread
        return response


class _OneNotice(_Feed):
    """Finds one notice in the parent's own feed, or answers 404."""

    def notice(self, pk):
        notices, placements = self.feed()
        row = notices.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such notice for you.")
        return row, placements


class NoticeDetailView(_OneNotice, PortalView):
    """GET /v1/portal/parent/notices/<id>/

    One notice addressed to any of the parent's children. Opening it does not
    mark it read. A notice not addressed to them, or withdrawn, answers 404.

    docstring-name: One of my notices as a parent
    """

    def get(self, request, pk):
        from .serializers import ParentNoticeSerializer

        notice, placements = self.notice(pk)
        return success_response(
            data=ParentNoticeSerializer(
                notice, context=self.context([notice], placements),
            ).data,
        )


class NoticeReadView(_OneNotice, PortalView):
    """POST /v1/portal/parent/notices/<id>/read/

    Marks one notice read for the parent, and clears its bell entry.

    docstring-name: Mark a notice read as a parent
    """

    def post(self, request, pk):
        from schools.vs_notices.services.notices import mark_read

        from .serializers import ParentNoticeSerializer

        notice, placements = self.notice(pk)
        mark_read(notice, request.user)
        context = self.context([notice], placements)
        context["read_ids"] = {notice.pk}
        return success_response(
            "Marked as read.", data=ParentNoticeSerializer(notice, context=context).data,
        )


class NoticeReadAllView(_Feed, PortalView):
    """POST /v1/portal/parent/notices/read-all/

    Marks every notice in the parent's feed read.

    docstring-name: Mark all my notices read as a parent
    """

    def post(self, request):
        from schools.vs_notices.services.notices import mark_all_read

        notices, _ = self.feed()
        marked = mark_all_read(notices, request.user)
        return success_response(
            "All notices marked as read.", data={"marked": marked, "unread_count": 0},
        )
