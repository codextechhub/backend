"""Class timetables, the teacher's derived view of them, and publication.

The grid read is one document, not a page: at most five days by a dozen periods
however large the school is, so paginating it would make a client reassemble a
grid it asked for whole.

**A clash never refuses a write.** It is recorded, persisted and returned in
``warnings`` beside the row that was written. The refusal happens once, at
publication - see ``services/publishing.py`` for why that is the right place for
it.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_rbac.scoping import WHOLE_TENANT

from ..constants import (
    PERM_TIMETABLE_CREATE,
    PERM_TIMETABLE_MANAGE,
    PERM_TIMETABLE_PUBLISH,
    PERM_TIMETABLE_UPDATE,
    PERM_TIMETABLE_VIEW,
)
from ..models import (
    ClassTimetable,
    DayOfWeek,
    Period,
    PeriodType,
    PublishState,
    Room,
    TimetableSlot,
)
from ..serializers import TimetableSlotSerializer, TimetableSlotWriteSerializer
from ..services.bells import periods_in_force
from ..services.clashes import grid_clashes, slot_warnings
from ..services.publishing import publish_class_timetable
from ..services.scoping import scope_to_visible_branches
from ..services.teachers import display_name, teaching_users
from ..services.timetable import (
    duplicate_grid,
    require_bell_schedule,
    timetable_for,
    touch_timetable,
    validate_slot,
)
from .base import CalendarViewMixin

#: Monday to Friday. A school teaching Saturdays stores day 6 happily - the
#: column accepts 1 to 7 - and this is only which days a grid renders by
#: default, which is a presentation choice rather than a data one.
GRID_DAYS = [
    DayOfWeek.MONDAY, DayOfWeek.TUESDAY, DayOfWeek.WEDNESDAY,
    DayOfWeek.THURSDAY, DayOfWeek.FRIDAY,
]


def _visible_classes(view):
    """The classes this caller may see, through the branch they are looking at.

    Two filters and they are not the same one. `scope_to_visible_branches` is
    security and always applies; the lens is the switcher at the top of the
    screen and applies when it is set. Leaving the second one off is what had a
    Lekki administrator picking Ikeja's classes out of a list headed Lekki.
    """
    from schools.vs_academics.models import SchoolClass
    from ..services.scoping import lens_branch, narrow_to_lens

    return narrow_to_lens(
        scope_to_visible_branches(
            SchoolClass.objects.filter(
                tenant=view.tenant, session=view.session, is_active=True,
            ).select_related("branch"),
            view.request.user, view.tenant,
        ),
        lens_branch(view),
    )


def _status_of(record):
    """Three states, and the third one is an absent row.

    "Not started" is not a stored value: a class whose grid has never been
    touched has no ClassTimetable row at all, which is what the design's third
    chip means and what a DRAFT default would quietly destroy.
    """
    if record is None:
        return {"status": None, "status_label": "Not started", "published_at": None}
    return {
        "status": record.status,
        "status_label": record.get_status_display(),
        "published_at": record.published_at,
    }


class ClassTimetableListView(CalendarViewMixin, APIView):
    """GET /v1/academics/timetable/classes/

    The class picker: every class the caller can see, with the three things the
    design shows beside each name - how many lessons it holds, what state its
    grid is in, and whether it has a clash in it.

    Not in FRD v3.0.1, which serves one grid at a time and leaves the picker
    with nothing to render. M13's class list gives names only.

    docstring-name: Class timetables
    """

    rbac_permission = PERM_TIMETABLE_VIEW
    pagination_class = None

    def get(self, request):
        if self.session is None:
            return success_response(data=[])

        classes = list(
            _visible_classes(self).annotate(
                slot_count=Count("timetable_slots", filter=Q(
                    timetable_slots__session=self.session,
                ), distinct=True),
            ),
        )
        records = {
            row.school_class_id: row
            for row in ClassTimetable.objects.filter(
                tenant=self.tenant, session=self.session,
            )
        }
        # One clash pass over the whole year rather than one per class: the
        # picker shows every class at once and N grid queries would make it the
        # most expensive read in the module.
        clashed = _classes_with_clashes(self.tenant, self.session)

        out = []
        for row in classes:
            entry = {
                "id": row.pk,
                "name": row.name,
                "branch": row.branch_id,
                "lesson_count": row.slot_count,
                "has_clash": row.pk in clashed,
            }
            if self.multi_branch:
                entry["branch_name"] = row.branch.name if row.branch_id else None
                entry["scope_label"] = (
                    row.branch.name if row.branch_id else "School-wide"
                )
            else:
                entry.pop("branch", None)
            entry.update(_status_of(records.get(row.pk)))
            out.append(entry)
        return success_response(data=out)


def _classes_with_clashes(tenant, session):
    """Which classes hold at least one clash. One query, not one per class.

    Tenant-wide, like every clash query here: a class whose only clash is with
    another branch still has a clash, and the picker has to say so or a branch
    admin publishes into a refusal they were never warned about.
    """
    rows = list(
        TimetableSlot.objects.filter(tenant=tenant, session=session)
        .values("pk", "school_class_id", "day_of_week", "period_id",
                "teacher_id", "room_id"),
    )
    by_slot = {}
    for row in rows:
        by_slot.setdefault((row["day_of_week"], row["period_id"]), []).append(row)

    out = set()
    for group in by_slot.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                same_teacher = a["teacher_id"] and a["teacher_id"] == b["teacher_id"]
                same_room = a["room_id"] and a["room_id"] == b["room_id"]
                if same_teacher or same_room:
                    out.add(a["school_class_id"])
                    out.add(b["school_class_id"])
    return out


class ClassTimetableDetailView(CalendarViewMixin, APIView):
    """GET, PUT /v1/academics/timetable/classes/<class_id>/

    GET returns the whole grid. PUT replaces it in one transaction and writes
    one audit event, not one per cell: replacing a grid is one change a school
    made.

    docstring-name: Class timetable
    """

    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_UPDATE if self.request.method == "PUT"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def _class(self, class_id):
        row = _visible_classes(self).filter(pk=class_id).first()
        if row is None:
            # 404 rather than 403: a class of another tenant, or of a branch
            # this caller cannot see, must not be distinguishable from one that
            # does not exist.
            raise NotFound("No such class at this school.")
        return row

    def get(self, request, class_id):
        session = self.session_required
        school_class = self._class(class_id)

        branch = school_class.branch
        slots = {
            (s.day_of_week, s.period_id): s
            for s in TimetableSlot.objects.filter(
                session=session, school_class=school_class,
            ).select_related("period", "subject", "teacher", "room")
        }
        period_rows = list(
            Period.objects.filter(
                tenant=self.tenant, session=session, is_active=True,
            ).select_related("branch"),
        )

        days = []
        filled = total = 0
        for day in GRID_DAYS:
            in_force = periods_in_force(
                self.tenant, session, day_of_week=day, branch=branch,
                queryset=period_rows,
            )
            cells = []
            for period in in_force:
                if period.period_type != PeriodType.LESSON:
                    cells.append({
                        "period": period.pk,
                        "period_label": period.label,
                        "start_time": period.start_time,
                        "end_time": period.end_time,
                        "kind": period.period_type,
                        "label": period.get_period_type_display(),
                    })
                    continue
                total += 1
                slot = slots.get((day, period.pk))
                if slot is not None:
                    filled += 1
                cells.append({
                    "period": period.pk,
                    "period_label": period.label,
                    "start_time": period.start_time,
                    "end_time": period.end_time,
                    "kind": PeriodType.LESSON,
                    "slot": TimetableSlotSerializer(
                        slot, context=self.get_serializer_context(),
                    ).data if slot else None,
                })
            days.append({
                "day_of_week": int(day),
                "day_label": DayOfWeek(day).label,
                "cells": cells,
            })

        warnings = grid_clashes(
            self.tenant, session, school_class, visible=self.visible,
        )
        record = timetable_for(self.tenant, session, school_class)
        data = {
            "school_class": {"id": school_class.pk, "name": school_class.name},
            "session": {"id": session.pk, "name": session.name},
            "has_bell_schedule": bool(period_rows),
            "days": days,
            # A count of what exists, carrying no expectation. Nothing knows how
            # many periods a subject should get a week, so there is no
            # percentage here, no progress figure and no "complete" flag.
            "filled": filled,
            "lesson_periods": total,
            "warnings": [w.as_dict() for w in warnings],
        }
        data.update(_status_of(record))
        return success_response(data=data)

    @transaction.atomic
    def put(self, request, class_id):
        session = self.session_required
        school_class = self._class(class_id)
        require_bell_schedule(self.tenant, session)

        rows = request.data.get("slots")
        if rows is None:
            rows = request.data if isinstance(request.data, list) else []

        writer = TimetableSlotWriteSerializer(data=rows, many=True)
        writer.is_valid(raise_exception=True)

        TimetableSlot.objects.filter(session=session, school_class=school_class).delete()
        created = []
        for entry in writer.validated_data:
            period = self._period(session, entry["period"])
            room = entry.get("room")
            validate_slot(
                self.tenant, session, school_class=school_class,
                day_of_week=entry["day_of_week"], period=period,
                subject=entry["subject"], teacher=entry.get("teacher"), room=room,
            )
            created.append(TimetableSlot(
                tenant=self.tenant, session=session, school_class=school_class,
                day_of_week=entry["day_of_week"], period=period,
                subject=entry["subject"], teacher=entry.get("teacher"), room=room,
                created_by=request.user,
            ))
        TimetableSlot.objects.bulk_create(created)
        touch_timetable(self.tenant, session, school_class, actor=request.user)

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="SchoolClass", entity_id=str(school_class.pk),
            entity_label=school_class.name,
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{school_class.name}'s timetable saved with {len(created)} "
                f"lesson{'' if len(created) == 1 else 's'}."
            ),
        )
        warnings = grid_clashes(
            self.tenant, session, school_class, visible=self.visible,
        )
        return success_response(
            f"{school_class.name}'s timetable saved.",
            {
                "saved": len(created),
                "warnings": [w.as_dict() for w in warnings],
            },
        )

    def _period(self, session, period):
        if period.tenant_id != self.tenant.id or period.session_id != session.pk:
            raise NotFound("No such period in this year.")
        return period


class SlotListCreateView(CalendarViewMixin, generics.ListCreateAPIView):
    """GET, POST /v1/academics/timetable/slots/

    One cell at a time, which is how an administrator filling a grid works.

    docstring-name: Timetable slots
    """

    serializer_class = TimetableSlotSerializer

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_CREATE if self.request.method == "POST"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        if self.session is None:
            return TimetableSlot.objects.none()
        qs = TimetableSlot.objects.filter(
            tenant=self.tenant, session=self.session,
        ).select_related("period", "subject", "teacher", "room", "school_class")
        class_id = (self.request.query_params.get("school_class") or "").strip()
        if class_id:
            qs = qs.filter(school_class_id=class_id)
        return qs

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        session = self.session_required
        writer = TimetableSlotWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        school_class = _visible_classes(self).filter(
            pk=data["school_class"].pk,
        ).first()
        if school_class is None:
            raise NotFound("No such class at this school.")

        period = data["period"]
        if period.tenant_id != self.tenant.id or period.session_id != session.pk:
            raise NotFound("No such period in this year.")

        validate_slot(
            self.tenant, session, school_class=school_class,
            day_of_week=data["day_of_week"], period=period,
            subject=data["subject"], teacher=data.get("teacher"),
            room=data.get("room"),
        )
        row = TimetableSlot.objects.create(
            tenant=self.tenant, session=session, school_class=school_class,
            day_of_week=data["day_of_week"], period=period,
            subject=data["subject"], teacher=data.get("teacher"),
            room=data.get("room"), created_by=request.user,
        )
        touch_timetable(self.tenant, session, school_class, actor=request.user)

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="TimetableSlot", entity_id=str(row.pk),
            entity_label=f"{school_class.name} {data['subject'].name}",
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{data['subject'].name} added to {school_class.name}'s "
                f"{DayOfWeek(row.day_of_week).label}."
            ),
        )
        row = TimetableSlot.objects.select_related(
            "period", "subject", "teacher", "room", "school_class",
        ).get(pk=row.pk)
        payload = TimetableSlotSerializer(
            row, context=self.get_serializer_context(),
        ).data
        # The clash is announced and the write stands. Both cells go red and
        # publishing is what refuses.
        payload["warnings"] = [
            w.as_dict() for w in slot_warnings(row, visible=self.visible)
        ]
        return success_response(
            f"{data['subject'].name} saved.", payload, status=201,
        )


class SlotPreviewView(CalendarViewMixin, APIView):
    """POST /v1/academics/timetable/slots/preview/

    The clashes a slot WOULD have, without writing it.

    **Why this exists rather than the client working it out.** The lesson form
    asks for a teacher and a room, and the moment both are chosen the school can
    already be told that Mr Eze is teaching JSS2 A at that hour. Answering that
    on the client means a second implementation of the clash rules - and the
    rules are not simple. They span the whole tenant on purpose (a person cannot
    be at two branches at once), and they redact the other side of a clash the
    caller may not see, naming neither the class nor the room. A client copy
    would get the width wrong, the redaction wrong, or both, and it would drift
    from the real engine the first time either changed.

    So this is not a second implementation. It builds the same unsaved row the
    create path builds and hands it to the same `slot_warnings`, which reads
    only the fields it is given and excludes nothing for a row with no primary
    key. What the school is shown before saving and what it is told after cannot
    disagree, because they are one function.

    Nothing is written and nothing is audited: it answers a question.

    docstring-name: Timetable slot preview
    """

    rbac_permission = PERM_TIMETABLE_CREATE
    pagination_class = None

    def post(self, request):
        session = self.session_required
        writer = TimetableSlotWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        school_class = _visible_classes(self).filter(
            pk=data["school_class"].pk,
        ).first()
        if school_class is None:
            raise NotFound("No such class at this school.")

        period = data["period"]
        if period.tenant_id != self.tenant.id or period.session_id != session.pk:
            raise NotFound("No such period in this year.")

        # Unsaved, and never saved. `pk` is None, which is exactly what makes
        # the sibling query exclude nothing - a new cell clashes with every
        # other row at that hour, including the one it would replace.
        draft = TimetableSlot(
            tenant=self.tenant, session=session, school_class=school_class,
            day_of_week=data["day_of_week"], period=period,
            subject=data["subject"], teacher=data.get("teacher"),
            room=data.get("room"),
        )
        # Editing a cell must not be told it clashes with itself.
        exclude = str(request.data.get("exclude") or "").strip()
        queryset = None
        if exclude.isdigit():
            queryset = [
                row for row in TimetableSlot.objects.filter(
                    tenant=self.tenant, session=session,
                    day_of_week=draft.day_of_week, period=period,
                ).select_related("school_class", "room", "teacher")
                if row.pk != int(exclude)
            ]

        warnings = slot_warnings(draft, visible=self.visible, queryset=queryset)
        return success_response(data={
            "warnings": [w.as_dict() for w in warnings],
        })


class SlotDetailView(CalendarViewMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/timetable/slots/<id>/

    docstring-name: Timetable slot
    """

    serializer_class = TimetableSlotSerializer

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_TIMETABLE_UPDATE,
            "PUT": PERM_TIMETABLE_UPDATE,
            "DELETE": PERM_TIMETABLE_MANAGE,
        }.get(self.request.method, PERM_TIMETABLE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return TimetableSlot.objects.filter(tenant=self.tenant).select_related(
            "period", "subject", "teacher", "room", "school_class",
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        row = self.get_object()
        writer = TimetableSlotWriteSerializer(row, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        period = data.get("period", row.period)
        teacher = data["teacher"] if "teacher" in data else row.teacher
        room = data["room"] if "room" in data else row.room
        subject = data.get("subject", row.subject)
        day = data.get("day_of_week", row.day_of_week)

        validate_slot(
            self.tenant, row.session, school_class=row.school_class,
            day_of_week=day, period=period, subject=subject,
            teacher=teacher, room=room, exclude_pk=row.pk,
        )
        row.day_of_week, row.period = day, period
        row.subject, row.teacher, row.room = subject, teacher, room
        row.save()
        touch_timetable(
            self.tenant, row.session, row.school_class, actor=request.user,
        )

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="TimetableSlot", entity_id=str(row.pk),
            entity_label=f"{row.school_class.name} {subject.name}",
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.school_class.name}'s lesson updated.",
        )
        payload = TimetableSlotSerializer(
            row, context=self.get_serializer_context(),
        ).data
        payload["warnings"] = [
            w.as_dict() for w in slot_warnings(row, visible=self.visible)
        ]
        return success_response(f"{subject.name} saved.", payload)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        row = self.get_object()
        label = f"{row.school_class.name} {row.subject.name}"
        school_class, session = row.school_class, row.session
        row.delete()
        touch_timetable(self.tenant, session, school_class, actor=request.user)
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="TimetableSlot", entity_id=str(kwargs.get("pk")),
            entity_label=label,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{label} cleared.",
        )
        return success_response("Slot cleared.")
