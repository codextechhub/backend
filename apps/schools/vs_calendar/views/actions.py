"""The three POST-only routes on a class grid: duplicate, clear and publish."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_TIMETABLE_MANAGE,
    PERM_TIMETABLE_PUBLISH,
    PERM_TIMETABLE_UPDATE,
)
from ..models import PublishState, TimetableSlot
from ..services.publishing import publish_class_timetable
from ..services.timetable import duplicate_grid, timetable_for
from .base import CalendarViewMixin
from .timetable import _visible_classes


class _ClassScoped(CalendarViewMixin, APIView):
    pagination_class = None

    def _class(self, class_id):
        row = _visible_classes(self).filter(pk=class_id).first()
        if row is None:
            raise NotFound("No such class at this school.")
        return row


class ClassTimetableDuplicateView(_ClassScoped):
    """POST /v1/academics/timetable/classes/<class_id>/duplicate/

    Copy another class's week into this one. ``?preview=1`` reports what would
    happen and writes nothing.

    Not in FRD v3.0.1. The design has a whole drawer for it, and two of its
    rules have to be computed here rather than in a client: which source lessons
    sit in a period the target does not run, and how many rows the copy will
    replace. Two clients would compute the first differently.

    docstring-name: Duplicate a class timetable
    """

    def get_permissions(self):
        self.rbac_permission = PERM_TIMETABLE_UPDATE
        return super().get_permissions()

    def post(self, request, class_id):
        session = self.session_required
        target = self._class(class_id)

        source_id = request.data.get("source_class")
        if not source_id:
            raise ValidationError({"source_class": "Say which class to copy from."})
        if str(source_id) == str(class_id):
            raise ValidationError({
                "source_class": "A class cannot be copied into itself.",
            })
        source = _visible_classes(self).filter(pk=source_id).first()
        if source is None:
            raise NotFound("No such class at this school.")

        preview = str(request.query_params.get("preview") or "").lower() in (
            "1", "true", "yes",
        )
        keep_teachers = bool(request.data.get("keep_teachers", True))
        keep_rooms = bool(request.data.get("keep_rooms", True))

        summary = duplicate_grid(
            self.tenant, session, source_class=source, target_class=target,
            actor=request.user, keep_teachers=keep_teachers,
            keep_rooms=keep_rooms, preview=preview,
        )
        if preview:
            return success_response(data=summary)

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="SchoolClass", entity_id=str(target.pk),
            entity_label=target.name,
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{summary['copied']} lesson"
                f"{'' if summary['copied'] == 1 else 's'} copied from "
                f"{source.name} into {target.name}."
            ),
        )
        count = summary["copied"]
        return success_response(
            f"{count} lesson{'' if count == 1 else 's'} copied from "
            f"{source.name} into {target.name}.",
            summary,
        )


class ClassTimetableClearView(_ClassScoped):
    """POST /v1/academics/timetable/classes/<class_id>/clear/

    docstring-name: Clear a class timetable
    """

    def get_permissions(self):
        # SENSITIVE and school_admin only, matching every other delete here.
        self.rbac_permission = PERM_TIMETABLE_MANAGE
        return super().get_permissions()

    @transaction.atomic
    def post(self, request, class_id):
        session = self.session_required
        school_class = self._class(class_id)

        removed, _ = TimetableSlot.objects.filter(
            session=session, school_class=school_class,
        ).delete()
        record = timetable_for(self.tenant, session, school_class)
        if record is not None and record.status == PublishState.PUBLISHED:
            record.status = PublishState.DRAFT
            record.published_at = None
            record.save(update_fields=["status", "published_at", "updated_at"])

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="SchoolClass", entity_id=str(school_class.pk),
            entity_label=school_class.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{school_class.name}'s timetable cleared.",
        )
        return success_response(f"{school_class.name}'s timetable cleared.")


class ClassTimetablePublishView(_ClassScoped):
    """POST /v1/academics/timetable/classes/<class_id>/publish/

    docstring-name: Publish a class timetable
    """

    def get_permissions(self):
        self.rbac_permission = PERM_TIMETABLE_PUBLISH
        return super().get_permissions()

    def post(self, request, class_id):
        session = self.session_required
        school_class = self._class(class_id)

        record = publish_class_timetable(
            self.tenant, session, school_class,
            actor=request.user, visible=self.visible,
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.ACADEMIC_TIMETABLE_PUBLISHED,
            entity_type="SchoolClass", entity_id=str(school_class.pk),
            entity_label=school_class.name,
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{school_class.name}'s timetable published with "
                f"{TimetableSlot.objects.filter(session=session, school_class=school_class).count()} "
                f"lessons."
            ),
        )
        # Publication sets a state and tells nobody. There is no academics
        # notification event type, no student record and no guardian record, so
        # the version 1.0 promise to notify students and parents is withdrawn
        # rather than quietly unimplemented.
        return success_response(
            f"{school_class.name}'s timetable published.",
            {
                "status": record.status,
                "status_label": record.get_status_display(),
                "published_at": record.published_at,
            },
        )
