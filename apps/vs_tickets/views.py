from __future__ import annotations

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from core.mixins import XVSModelViewSetMixin
from core.response import success_response
from vs_user.models import User

from .constants import (
    CommentVisibility,
    TicketCategory,
    TicketPermission,
    TicketPriority,
    TicketStatus,
)
from .models import Ticket, TicketComment
from .permissions import TICKET_PERMISSIONS
from .serializers import (
    TicketAssignSerializer,
    TicketAttachmentCreateSerializer,
    TicketAttachmentSerializer,
    TicketAuditLogSerializer,
    TicketCommentCreateSerializer,
    TicketCommentSerializer,
    TicketCreateSerializer,
    TicketDashboardSerializer,
    TicketDetailSerializer,
    TicketSerializer,
    TicketTransitionSerializer,
    TicketUpdateSerializer,
)
from .services import tickets as ticket_svc
from .services import visibility


class TicketViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """Ticket CRUD plus assignment, transitions, comments, attachments and audit."""

    permission_classes = TICKET_PERMISSIONS

    # Actions gated by an RBAC key (support staff bypass in the permission
    # class). Absent actions rely on queryset/object scoping: anyone may file
    # a ticket and participants always keep access to their own thread.
    RBAC_ACTION_KEYS = {
        "assign": TicketPermission.ASSIGN,
        "transition": TicketPermission.MANAGE,
        "audit": TicketPermission.AUDIT_VIEW,
    }

    @property
    def rbac_permission(self):
        return self.RBAC_ACTION_KEYS.get(self.action)

    def get_queryset(self):
        # Internal notes (and their attachments) stay out of the counts for
        # users who cannot see them.
        if visibility.sees_internal_notes_by_default(self.request.user):
            comment_filter = Q()
            attachment_filter = Q()
        else:
            comment_filter = Q(comments__visibility=CommentVisibility.PUBLIC)
            attachment_filter = Q(attachments__comment__isnull=True) | Q(
                attachments__comment__visibility=CommentVisibility.PUBLIC
            )
        qs = visibility.visible_tickets_qs(self.request.user).annotate(
            comments_count=Count("comments", filter=comment_filter, distinct=True),
            attachments_count=Count("attachments", filter=attachment_filter, distinct=True),
        )
        params = self.request.query_params
        if value := params.get("status"):
            qs = qs.filter(status=value)
        if value := params.get("priority"):
            qs = qs.filter(priority=value)
        if value := params.get("category"):
            qs = qs.filter(category=value)
        if value := params.get("assignee"):
            qs = qs.filter(assignee_id=value)
        if value := params.get("requester"):
            qs = qs.filter(requester_id=value)
        if value := params.get("school"):
            qs = qs.filter(school_id=value)
        if value := params.get("created_from"):
            qs = qs.filter(created_at__date__gte=value)
        if value := params.get("created_to"):
            qs = qs.filter(created_at__date__lte=value)
        if value := params.get("q"):
            qs = qs.filter(
                Q(title__icontains=value)
                | Q(description__icontains=value)
                | Q(ticket_number__icontains=value)
            )
        return qs.order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return TicketDetailSerializer
        if self.action == "create":
            return TicketCreateSerializer
        if self.action in ("update", "partial_update"):
            return TicketUpdateSerializer
        return TicketSerializer

    def get_object(self):
        ticket = get_object_or_404(
            Ticket.all_objects.select_related("requester", "assignee", "school", "branch"),
            pk=self.kwargs["pk"],
        )
        if not visibility.can_view_ticket(self.request.user, ticket):
            raise NotFound("No such ticket.")
        return ticket

    def create(self, request, *args, **kwargs):
        serializer = TicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = ticket_svc.create_ticket(actor=request.user, **serializer.validated_data)
        return success_response(
            message="Ticket created successfully.",
            data=TicketDetailSerializer(ticket, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        ticket = self.get_object()
        serializer = TicketUpdateSerializer(data=request.data, partial=kwargs.pop("partial", False))
        serializer.is_valid(raise_exception=True)
        ticket = ticket_svc.update_ticket(ticket, actor=request.user, **serializer.validated_data)
        return success_response(
            message="Ticket updated successfully.",
            data=TicketDetailSerializer(ticket, context={"request": request}).data,
        )

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        raise PermissionDenied("Tickets are retained for audit history and cannot be deleted.")

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        ticket = self.get_object()
        serializer = TicketAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignee_id = serializer.validated_data.get("assignee_id")
        assignee = User.objects.get(pk=assignee_id) if assignee_id else None
        ticket = ticket_svc.assign_ticket(ticket, actor=request.user, assignee=assignee)
        return success_response(
            message="Ticket assigned successfully.",
            data=TicketDetailSerializer(ticket, context={"request": request}).data,
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        ticket = self.get_object()
        serializer = TicketTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = ticket_svc.transition_ticket(ticket, actor=request.user, status=serializer.validated_data["status"])
        return success_response(
            message="Ticket status updated successfully.",
            data=TicketDetailSerializer(ticket, context={"request": request}).data,
        )

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        ticket = self.get_object()
        if request.method == "GET":
            comments = ticket.comments.select_related("author").prefetch_related("attachments")
            if not visibility.can_view_internal_notes(request.user, ticket):
                comments = comments.filter(visibility=CommentVisibility.PUBLIC)
            return success_response(
                message="Comments retrieved successfully.",
                data=TicketCommentSerializer(comments, many=True, context={"request": request}).data,
            )

        serializer = TicketCommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = ticket_svc.add_comment(ticket, actor=request.user, **serializer.validated_data)
        return success_response(
            message="Comment added successfully.",
            data=TicketCommentSerializer(comment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def attachments(self, request, pk=None):
        ticket = self.get_object()
        serializer = TicketAttachmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = None
        comment_id = serializer.validated_data.get("comment_id")
        if comment_id:
            # Scoped to this ticket so foreign comment ids 404 instead of
            # leaking their existence through a different error.
            comment = get_object_or_404(TicketComment, pk=comment_id, ticket=ticket)
        attachment = ticket_svc.add_attachment(
            ticket,
            actor=request.user,
            file_obj=serializer.validated_data["file"],
            comment=comment,
        )
        return success_response(
            message="Attachment added successfully.",
            data=TicketAttachmentSerializer(attachment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="audit")
    def audit(self, request, pk=None):
        ticket = self.get_object()
        qs = ticket.audit_logs.select_related("actor").all()
        return success_response(
            message="Ticket audit trail retrieved successfully.",
            data=TicketAuditLogSerializer(qs, many=True).data,
        )


class TicketDashboardView(APIView):
    permission_classes = TICKET_PERMISSIONS

    def get(self, request):
        qs = visibility.visible_tickets_qs(request.user)
        aggregates = {
            "total": Count("id"),
            "assigned_to_me": Count("id", filter=Q(assignee=request.user)),
            "requested_by_me": Count("id", filter=Q(requester=request.user)),
        }
        for key, _ in TicketStatus.choices:
            aggregates[f"status__{key}"] = Count("id", filter=Q(status=key))
        for key, _ in TicketPriority.choices:
            aggregates[f"priority__{key}"] = Count("id", filter=Q(priority=key))
        for key, _ in TicketCategory.choices:
            aggregates[f"category__{key}"] = Count("id", filter=Q(category=key))
        row = qs.aggregate(**aggregates)
        payload = {
            "total": row["total"],
            "by_status": {key: row[f"status__{key}"] for key, _ in TicketStatus.choices},
            "by_priority": {key: row[f"priority__{key}"] for key, _ in TicketPriority.choices},
            "by_category": {key: row[f"category__{key}"] for key, _ in TicketCategory.choices},
            "assigned_to_me": row["assigned_to_me"],
            "requested_by_me": row["requested_by_me"],
        }
        return success_response(
            message="Ticket dashboard retrieved successfully.",
            data=TicketDashboardSerializer(payload).data,
        )
