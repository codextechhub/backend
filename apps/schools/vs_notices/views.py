"""The staff routes for notices, mounted at ``/v1/notices/``.

Reading needs ``school.notices.view``. Drafting, correcting, publishing and
withdrawing need either ``school.notices.post`` or ``school.class_notices.post``,
and the service decides, notice by notice, whether the audience lies inside
what the caller may post to (``services.notices.reach_for``). A notice outside
the caller's sight answers 404. ``pending_tenant_surface`` is left closed: a
school that has not gone live has no pupils to tell.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.field_enforcement import assert_writable
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .constants import PERM_POST, PERM_POST_CLASS, PERM_VIEW, NoticeStatus
from .serializers import NoticeSerializer, NoticeWriteSerializer
from .services import notices as notice_service


class _NoticesView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No school in context.")
        return tenant

    def write_permission(self):
        """Either posting key admits the request; the service judges the audience."""
        self.rbac_permission = [PERM_POST, PERM_POST_CLASS]

    def notice(self, pk):
        row = (
            notice_service.staff_notices(self.tenant, self.request.user)
            .filter(pk=pk)
            .prefetch_related("audiences__branch", "audiences__level", "audiences__school_class")
            .select_related("created_by", "published_by", "withdrawn_by")
            .first()
        )
        if row is None:
            raise NotFound("No such notice at this school.")
        return row

    def context(self, **extra):
        return {
            "request": self.request,
            "reach": notice_service.reach_for(self.tenant, self.request.user),
            **extra,
        }


class NoticeListCreateView(_NoticesView):
    """GET, POST /v1/notices/

    GET lists the notices inside the caller's reach, newest first, filtered by
    ``?status=`` and ``?search=`` (title). POST drafts a notice, and publishes
    it at once when the body says ``"publish": true``.

    docstring-name: Notices
    """

    def get_permissions(self):
        if self.request.method == "GET":
            self.rbac_permission = PERM_VIEW
        else:
            self.write_permission()
        return super().get_permissions()

    def get(self, request):
        qs = (
            notice_service.staff_notices(self.tenant, request.user)
            .prefetch_related("audiences__branch", "audiences__level", "audiences__school_class")
            .select_related("created_by", "published_by", "withdrawn_by")
        )
        status = (request.query_params.get("status") or "").upper()
        if status in NoticeStatus.values:
            qs = qs.filter(status=status)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(title__icontains=search)
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = NoticeSerializer(page, many=True, context=self.context()).data
        return paginator.get_paginated_response(data)

    def post(self, request):
        writer = NoticeWriteSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        notice = notice_service.create(
            self.tenant, actor=request.user,
            title=data.get("title", ""), body=data.get("body", ""),
            audience=data.get("audience", []), publish_now=data.get("publish", False),
        )
        message = "Notice published." if data.get("publish") else "Draft saved."
        return success_response(
            message,
            data=NoticeSerializer(self.notice(notice.pk), context=self.context(with_reads=True)).data,
            status=201,
        )


class NoticeDetailView(_NoticesView):
    """GET, PATCH /v1/notices/<id>/

    GET carries the read count. PATCH corrects a notice: anything on a draft,
    the title and the message on a published one.

    docstring-name: A notice
    """

    def get_permissions(self):
        if self.request.method == "GET":
            self.rbac_permission = PERM_VIEW
        else:
            self.write_permission()
        return super().get_permissions()

    def get(self, request, pk):
        return success_response(
            data=NoticeSerializer(self.notice(pk), context=self.context(with_reads=True)).data,
        )

    def patch(self, request, pk):
        notice = self.notice(pk)
        # A correction is an update of a stored notice, so a field open on
        # create is still judged by its Write switch; an unchanged value passes.
        assert_writable(
            request, "school.notices",
            {k: request.data[k] for k in ("title", "body", "audience") if k in request.data},
            current={"title": notice.title, "body": notice.body},
        )
        writer = NoticeWriteSerializer(
            data=request.data, partial=True, context={"request": request},
        )
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        notice_service.update(
            notice, actor=request.user,
            title=data.get("title"), body=data.get("body"),
            audience=data.get("audience") if "audience" in request.data else None,
        )
        return success_response(
            "Notice saved.",
            data=NoticeSerializer(self.notice(pk), context=self.context(with_reads=True)).data,
        )


class NoticePublishView(_NoticesView):
    """POST /v1/notices/<id>/publish/

    Publishes a draft. Its pupils are told in-app and by email.

    docstring-name: Publish a notice
    """

    def get_permissions(self):
        self.write_permission()
        return super().get_permissions()

    def post(self, request, pk):
        notice_service.publish(self.notice(pk), actor=request.user)
        return success_response(
            "Notice published.",
            data=NoticeSerializer(self.notice(pk), context=self.context(with_reads=True)).data,
        )


class NoticeWithdrawView(_NoticesView):
    """POST /v1/notices/<id>/withdraw/

    Takes a published notice out of every pupil's feed. Staff still see it,
    marked withdrawn.

    docstring-name: Withdraw a notice
    """

    def get_permissions(self):
        self.write_permission()
        return super().get_permissions()

    def post(self, request, pk):
        notice_service.withdraw(self.notice(pk), actor=request.user)
        return success_response(
            "Notice withdrawn.",
            data=NoticeSerializer(self.notice(pk), context=self.context(with_reads=True)).data,
        )


class NoticeAudienceOptionsView(_NoticesView):
    """GET /v1/notices/audiences/

    What the caller may address a notice to: the whole school and levels for a
    whole-school poster, their branches where the school has more than one,
    and the classes inside their reach.

    docstring-name: Who a notice can go to
    """

    def get_permissions(self):
        self.write_permission()
        return super().get_permissions()

    def get(self, request):
        return success_response(
            data=notice_service.audience_options(self.tenant, request.user),
        )
