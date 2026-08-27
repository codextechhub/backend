"""Rooms: the places lessons and examinations happen in.

A short surface, and the timetable depends on it.

Two rules the screens render verbatim and this module therefore has to get
right. **The same room name at two branches is normal**, not an error - "Block A
Room 1" exists at Lekki and could exist at Ikeja - and a repeat within one
branch is refused. And **a room holding anything cannot be deleted**: the
``PROTECT`` foreign keys make the platform answer 409 PROTECTED_REFERENCE, and
this view catches that to say what the school should do instead, because a
generic refusal under a Delete button teaches nobody anything.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from rest_framework import generics

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_TIMETABLE_CREATE,
    PERM_TIMETABLE_MANAGE,
    PERM_TIMETABLE_UPDATE,
    PERM_TIMETABLE_VIEW,
)
from ..exceptions import DuplicateRoomCode, DuplicateRoomName
from ..models import Room
from ..serializers import RoomSerializer, RoomWriteSerializer, _usage_label
from ..services.scoping import UNSET, room_branch, scope_to_visible_branches
from .base import CalendarViewMixin


def _assert_unique(tenant, *, name, code, branch, multi_branch, exclude_pk=None):
    """Refuse a duplicate name or code before writing it.

    The two constraints are scoped differently and the messages have to say so:
    a NAME repeats freely across branches and never within one, while a CODE is
    unique across the whole school. Stating the wrong rule would send a school
    hunting for a clash in the wrong place.

    Checks the name first, because it is the field filled in first and a drawer
    shows one message at a time.

    Not a replacement for the constraints. Two concurrent requests can both pass
    this and the database stops the second, which still answers the generic
    message - the right trade, because that is a race and this is a typo.
    """
    rows = Room.all_objects.filter(tenant=tenant).select_related("branch")
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)

    value = (name or "").strip()
    if value:
        hit = rows.filter(branch=branch).annotate(
            _n=Lower("name"),
        ).filter(_n=value.lower()).first()
        if hit is not None:
            where = (
                f" at {hit.branch.name}"
                if multi_branch and hit.branch_id else ""
            )
            rule = (
                " Room names only have to be unique within a branch, so pick a "
                "different one here."
                if multi_branch else " Pick a different name."
            )
            raise DuplicateRoomName(
                f"{value} already exists{where}.{rule}",
                field="name", conflict=hit.name,
            )

    typed = (code or "").strip()
    if typed:
        hit = rows.annotate(_c=Lower("code")).filter(_c=typed.lower()).first()
        if hit is not None:
            # The branch is named even when the caller cannot see it: the
            # alternative is a refusal they cannot resolve, because their own
            # list does not contain the row. Same reasoning as the catalogue's.
            where = (
                f" at {hit.branch.name}"
                if multi_branch and hit.branch_id else ""
            )
            raise DuplicateRoomCode(
                f"That code belongs to {hit.name}{where}. Room codes are "
                f"unique across the whole school, so pick a different one.",
                field="code", conflict=hit.name,
            )


def _annotated(tenant):
    return (
        Room.objects.filter(tenant=tenant)
        .select_related("branch")
        .annotate(
            lesson_count=Count("slots", distinct=True),
            paper_count=Count("exam_slots", distinct=True),
        )
        # Explicit: Meta.ordering does not survive the annotate chain, and an
        # unordered queryset makes pagination return rows twice.
        .order_by("branch__name", "name")
    )


class _RoomBase(CalendarViewMixin):
    serializer_class = RoomSerializer

    def _filtered(self, qs):
        params = self.request.query_params
        # Rooms are the one thing here with a non-null branch, so the read is
        # exclusive rather than inclusive: there is no shared row to include.
        qs = scope_to_visible_branches(qs, self.request.user, self.tenant)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))

        room_type = (params.get("type") or "").strip()
        if room_type:
            qs = qs.filter(room_type=room_type.upper())

        branch = (params.get("branch") or "").strip()
        if branch and self.multi_branch:
            from vs_tenants.references import resolve_branch_reference

            qs = qs.filter(
                branch=resolve_branch_reference(self.tenant, branch, "branch"),
            )

        active = (params.get("active") or "").strip().lower()
        if active in ("true", "1", "active"):
            qs = qs.filter(is_active=True)
        elif active in ("false", "0", "inactive"):
            qs = qs.filter(is_active=False)
        return qs

    def _write_branch(self, validated):
        requested = validated["branch"] if "branch" in validated else UNSET
        return room_branch(self.request.user, self.tenant, requested)


class RoomListCreateView(_RoomBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/timetable/rooms/

    docstring-name: Rooms
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_CREATE if self.request.method == "POST"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        return self._filtered(_annotated(self.tenant))

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = RoomWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        branch = self._write_branch(data)
        _assert_unique(
            self.tenant,
            name=data["name"], code=data.get("code"), branch=branch,
            multi_branch=self.multi_branch,
        )

        row = Room.objects.create(
            tenant=self.tenant,
            branch=branch,
            name=data["name"].strip(),
            code=(data.get("code") or "").strip().upper(),
            room_type=data["room_type"],
            capacity=data.get("capacity"),
            is_active=data.get("is_active", True),
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="Room", entity_id=str(row.pk), entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} added.",
        )
        return success_response(
            f"{row.name} added.",
            RoomSerializer(row, context=self.get_serializer_context()).data,
            status=201,
        )


class RoomDetailView(_RoomBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/timetable/rooms/<id>/

    docstring-name: Room
    """

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_TIMETABLE_UPDATE,
            "PUT": PERM_TIMETABLE_UPDATE,
            "DELETE": PERM_TIMETABLE_MANAGE,
        }.get(self.request.method, PERM_TIMETABLE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        # Not `_filtered`: a detail route resolves by primary key, and a row of
        # another tenant must answer 404 rather than 403.
        return _annotated(self.tenant)

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        row = self.get_object()
        writer = RoomWriteSerializer(row, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        if "branch" in data:
            row.branch = self._write_branch(data)
        # Against the branch it will END UP at, not the one it is leaving: a
        # room moved to another branch collides with that branch's names.
        _assert_unique(
            self.tenant,
            name=data.get("name", row.name),
            code=data.get("code", row.code),
            branch=row.branch,
            multi_branch=self.multi_branch,
            exclude_pk=row.pk,
        )
        for field in ("name", "room_type", "capacity", "is_active"):
            if field in data:
                setattr(row, field, data[field])
        if "name" in data:
            row.name = row.name.strip()
        if "code" in data:
            row.code = (data.get("code") or "").strip().upper()
        row.save()

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="Room", entity_id=str(row.pk), entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} updated.",
        )
        return success_response(
            f"{row.name} updated.",
            RoomSerializer(
                _annotated(self.tenant).get(pk=row.pk),
                context=self.get_serializer_context(),
            ).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        row = self.get_object()
        lessons = row.slots.count()
        papers = row.exam_slots.count()
        if lessons or papers:
            # Caught here rather than left to the PROTECT handler, because the
            # generic message names a constraint and this one names the way out.
            from ..exceptions import RoomInUse

            raise RoomInUse(
                f"This room already holds {_usage_label(lessons, papers).lower()}. "
                f"Deactivate it instead - it will stop appearing when anyone "
                f"picks a room, and everything already scheduled here stays "
                f"intact.",
                lessons=lessons, exam_papers=papers,
            )

        name = row.name
        row.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Room", entity_id=str(kwargs.get("pk")), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")
