"""The pupil portal's routes.

Every view here answers for the signed-in pupil and nobody else: no route
carries a pupil's id, so there is nothing to change in an address to reach
another child. ``pending_tenant_surface`` is left at its closed default,
because a school that has not gone live has no pupils using the portal.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from .permissions import PORTAL_PERMISSIONS, linked_student


class PortalView(APIView):
    """What every portal view shares: the permissions, and the pupil."""

    permission_classes = PORTAL_PERMISSIONS

    @property
    def student(self):
        return linked_student(self.request)


def _initials(student) -> str:
    return "".join(
        part[0] for part in (student.first_name, student.last_name) if part
    ).upper()


class MeView(PortalView):
    """GET /v1/portal/student/me/

    Who the signed-in pupil is, for the portal's shell: their name and face,
    class and admission number, their school, their branch where the school
    has more than one, and the year and term the school is in. A school app
    asks this after sign-in: 200 means a pupil, 404 means anybody else.

    docstring-name: The signed-in pupil
    """

    def get(self, request):
        from schools.vs_gradebook.services.resolve import current_term
        from schools.vs_students.services.documents import face_url
        from schools.vs_students.services.scoping import branch_dimension_applies
        from vs_config.clock import branch_today
        from vs_user.serializers import school_public_info

        student = self.student
        tenant = student.tenant
        enrolment = (
            student.enrolments.filter(is_active=True, session__status="ACTIVE")
            .select_related("school_class__level", "session")
            .first()
        )
        term = current_term(tenant, branch_today(tenant, student.branch_id))
        show_branch = branch_dimension_applies(tenant)
        school = school_public_info(
            getattr(tenant, "school_profile", None), request,
        ) or {}

        data = {
            "student": {
                "id": student.pk,
                "first_name": student.first_name,
                "full_name": student.full_name,
                "initials": _initials(student),
                "photo_url": face_url(student, request=request) or "",
                "admission_number": student.student_number or "",
                "class_name": enrolment.school_class.name if enrolment else "",
                "level_name": (
                    enrolment.school_class.level.name
                    if enrolment and enrolment.school_class.level_id else ""
                ),
            },
            "school": {"name": school.get("name", ""), "logo_url": school.get("logo") or ""},
            "show_branch": show_branch,
            "session": (
                {"id": enrolment.session.pk, "name": enrolment.session.name}
                if enrolment else None
            ),
            "term": {"id": term.pk, "name": term.name} if term is not None else None,
        }
        if show_branch:
            data["branch"] = {"id": student.branch_id, "name": student.branch.name}
        return success_response(data=data)


def _read_ids(user, notices) -> set:
    from schools.vs_notices.models import NoticeRead

    return set(
        NoticeRead.objects.filter(user=user, notice__in=notices)
        .values_list("notice_id", flat=True)
    )


class NoticeFeedView(PortalView):
    """GET /v1/portal/student/notices/

    The published notices addressed to the pupil, newest first, each marked
    unread until they open it, with the unread count for the badge. A
    withdrawn notice has left the feed.

    docstring-name: My notices
    """

    def get(self, request):
        from core.pagination import XVSPagination
        from schools.vs_notices.serializers import PortalNoticeSerializer
        from schools.vs_notices.services.notices import pupil_notices

        notices = pupil_notices(self.student)
        unread = notices.exclude(reads__user=request.user).count()
        paginator = XVSPagination()
        page = paginator.paginate_queryset(notices, request, view=self)
        data = PortalNoticeSerializer(
            page, many=True, context={"read_ids": _read_ids(request.user, page)},
        ).data
        response = paginator.get_paginated_response(data)
        response.data["unread_count"] = unread
        return response


class _OneNotice:
    """Finds one notice in the pupil's own feed, or answers 404."""

    def notice(self, pk):
        from rest_framework.exceptions import NotFound
        from schools.vs_notices.services.notices import pupil_notices

        row = pupil_notices(self.student).filter(pk=pk).first()
        if row is None:
            raise NotFound("No such notice for you.")
        return row


class NoticeDetailView(_OneNotice, PortalView):
    """GET /v1/portal/student/notices/<id>/

    One notice addressed to the pupil. Opening it does not mark it read; the
    pupil marks it, as the design has them do. A notice not addressed to them,
    or withdrawn, answers 404.

    docstring-name: One of my notices
    """

    def get(self, request, pk):
        from schools.vs_notices.serializers import PortalNoticeSerializer

        notice = self.notice(pk)
        return success_response(
            data=PortalNoticeSerializer(
                notice, context={"read_ids": _read_ids(request.user, [notice])},
            ).data,
        )


class NoticeReadView(_OneNotice, PortalView):
    """POST /v1/portal/student/notices/<id>/read/

    Marks one notice read, and clears its bell entry.

    docstring-name: Mark a notice read
    """

    def post(self, request, pk):
        from schools.vs_notices.serializers import PortalNoticeSerializer
        from schools.vs_notices.services.notices import mark_read

        notice = self.notice(pk)
        mark_read(notice, request.user)
        return success_response(
            "Marked as read.",
            data=PortalNoticeSerializer(notice, context={"read_ids": {notice.pk}}).data,
        )


class NoticeReadAllView(PortalView):
    """POST /v1/portal/student/notices/read-all/

    Marks every notice in the pupil's feed read.

    docstring-name: Mark all my notices read
    """

    def post(self, request):
        from schools.vs_notices.services.notices import mark_all_read, pupil_notices

        marked = mark_all_read(pupil_notices(self.student), request.user)
        return success_response(
            "All notices marked as read.", data={"marked": marked, "unread_count": 0},
        )
