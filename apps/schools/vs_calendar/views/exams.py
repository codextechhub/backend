"""Exam scheduling: papers placed inside a dated exam period.

**Anchored to the calendar, not floating beside it.** An ``Exam`` names the
EXAM_PERIOD ``CalendarEvent`` it happens inside, and its session, its dates and
its branch scope are read from that event rather than copied. The school says in
the calendar that it is examining in the first week of December, and the
schedule hangs off that statement.

**Two refusals and two warnings, and the split is the opposite way round from
the class timetable.** A class sitting two papers in one sitting is refused by
the unique constraint, because it is physically impossible and a school never
means it. A room used twice and an invigilator in two rooms both warn, because a
school legitimately runs two classes' papers in the Main Hall at once and
legitimately floats one invigilator between adjacent rooms - and nothing records
how many candidates a paper has or how many rooms a person can supervise, so
refusing either would be refusing on a guess.

**No candidate numbers anywhere.** No students sitting, no seats required, no
room utilisation. Each needs a student model that does not exist, and a zero
would read as a real and alarming figure rather than an absent feature.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_TIMETABLE_CREATE,
    PERM_TIMETABLE_MANAGE,
    PERM_TIMETABLE_PUBLISH,
    PERM_TIMETABLE_UPDATE,
    PERM_TIMETABLE_VIEW,
)
from ..exceptions import (
    ExamEventNotExamPeriod,
    ExamOutsideExamPeriod,
    ExamPublishedReadOnly,
    RoomBranchConflict,
)
from ..models import CalendarEvent, Exam, ExamSlot, EventType, PublishState
from ..serializers import ExamSerializer, ExamSlotSerializer, ExamSlotWriteSerializer
from ..services.clashes import SITTING_RANK, exam_clashes, exam_slot_warnings
from ..services.publishing import publish_exam
from ..services.teachers import assert_is_teacher
from .base import CalendarViewMixin
from .timetable import _visible_classes


class ExamListCreateView(CalendarViewMixin, generics.ListCreateAPIView):
    """GET, POST /v1/academics/exams/

    The design never creates an ``Exam`` explicitly - it reads the exam period
    from the calendar and goes straight to "Add paper". So POST is idempotent
    against the event: asked twice for the same exam period it returns the row
    that already exists rather than making a second one. The FRD's model is
    right and the design is right to refuse to make a school name the same thing
    twice.

    docstring-name: Exams
    """

    serializer_class = ExamSerializer
    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_CREATE if self.request.method == "POST"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        if self.session is None:
            return Exam.objects.none()
        return (
            Exam.objects.filter(
                tenant=self.tenant, calendar_event__session=self.session,
            )
            .select_related("calendar_event")
        )

    def list(self, request, *args, **kwargs):
        rows = list(self.get_queryset())
        context = self.get_serializer_context()
        data = []
        for exam in rows:
            entry = ExamSerializer(exam, context=context).data
            slots = list(
                ExamSlot.objects.filter(tenant=self.tenant, exam=exam)
                .select_related("school_class", "subject", "room", "invigilator")
                .order_by("exam_date"),
            )
            slots.sort(key=lambda s: (s.exam_date, SITTING_RANK[s.sitting]))
            entry["slots"] = ExamSlotSerializer(
                slots, many=True, context=context,
            ).data
            entry["warnings"] = [
                w.as_dict() for w in exam_clashes(
                    self.tenant, exam, visible=self.visible,
                )
            ]
            entry["paper_count"] = len(slots)
            data.append(entry)
        return success_response(data=data)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        session = self.session_required
        event_id = request.data.get("calendar_event")
        if not event_id:
            raise ValidationError({
                "calendar_event": "Say which exam period this sits inside.",
            })
        event = CalendarEvent.objects.filter(
            tenant=self.tenant, session=session, pk=event_id,
        ).first()
        if event is None:
            raise NotFound("No such calendar entry in this year.")
        if event.event_type != EventType.EXAM_PERIOD:
            raise ExamEventNotExamPeriod(
                f"{event.name} is a "
                f"{event.get_event_type_display().lower()}, and an exam "
                f"timetable sits inside an exam period.",
                event=event.name,
            )

        existing = Exam.objects.filter(
            tenant=self.tenant, calendar_event=event,
        ).first()
        if existing is not None:
            return success_response(
                f"{existing.name} already exists.",
                ExamSerializer(existing, context=self.get_serializer_context()).data,
            )

        row = Exam.objects.create(
            tenant=self.tenant, calendar_event=event,
            name=(request.data.get("name") or event.name).strip(),
            created_by=request.user,
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="Exam", entity_id=str(row.pk), entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} created.",
        )
        return success_response(
            f"{row.name} created.",
            ExamSerializer(row, context=self.get_serializer_context()).data,
            status=201,
        )


class ExamDetailView(CalendarViewMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/exams/<id>/

    docstring-name: Exam
    """

    serializer_class = ExamSerializer
    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_TIMETABLE_UPDATE,
            "PUT": PERM_TIMETABLE_UPDATE,
            "DELETE": PERM_TIMETABLE_MANAGE,
        }.get(self.request.method, PERM_TIMETABLE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return Exam.objects.filter(tenant=self.tenant).select_related("calendar_event")

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        row = self.get_object()
        name = (request.data.get("name") or "").strip()
        if name:
            row.name = name
            row.save(update_fields=["name", "updated_at"])
        return success_response(
            f"{row.name} updated.",
            ExamSerializer(row, context=self.get_serializer_context()).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        row = self.get_object()
        name = row.name
        row.delete()  # slots CASCADE: a paper has no meaning without its exam.
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Exam", entity_id=str(kwargs.get("pk")), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")


class _ExamScoped(CalendarViewMixin):
    def _exam(self, exam_id, *, for_write=False):
        row = (
            Exam.objects.filter(tenant=self.tenant, pk=exam_id)
            .select_related("calendar_event")
            .first()
        )
        if row is None:
            raise NotFound("No such exam at this school.")
        if for_write and row.status == PublishState.PUBLISHED:
            # One-way in this version. Whether an exam timetable may be
            # unpublished is an open product question, and until it is answered
            # a published schedule is frozen rather than quietly editable.
            raise ExamPublishedReadOnly(
                f"{row.name} has been published, so its papers can no longer "
                f"be changed.",
            )
        return row

    def _validate(self, exam, data, *, exclude_pk=None):
        event = exam.calendar_event
        if not (event.start_date <= data["exam_date"] <= event.end_date):
            raise ExamOutsideExamPeriod(
                f"This date is outside {event.name} "
                f"({event.start_date:%d %b %Y} - {event.end_date:%d %b %Y}).",
                exam_period=event.name,
            )
        school_class = _visible_classes(self).filter(
            pk=data["school_class"].pk,
        ).first()
        if school_class is None:
            raise NotFound("No such class at this school.")
        assert_is_teacher(self.tenant, data.get("invigilator"))

        room = data.get("room")
        if room is not None and event.branch_id and room.branch_id != event.branch_id:
            raise RoomBranchConflict(
                f"{room.name} is at another branch, so it cannot be used for "
                f"{event.name}.",
                room=room.name,
            )
        return school_class


class ExamSlotListCreateView(_ExamScoped, generics.ListCreateAPIView):
    """GET, POST /v1/academics/exams/<exam_id>/slots/

    docstring-name: Exam papers
    """

    serializer_class = ExamSlotSerializer
    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_CREATE if self.request.method == "POST"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        exam = self._exam(self.kwargs["exam_id"])
        return (
            ExamSlot.objects.filter(tenant=self.tenant, exam=exam)
            .select_related("school_class", "subject", "room", "invigilator")
        )

    def list(self, request, *args, **kwargs):
        rows = list(self.get_queryset())
        # Sittings rank by time of day, never by name: "AFTERNOON" sorts before
        # "MORNING" lexically, which would invert every day holding both.
        rows.sort(key=lambda s: (s.exam_date, SITTING_RANK[s.sitting]))
        return success_response(data=ExamSlotSerializer(
            rows, many=True, context=self.get_serializer_context(),
        ).data)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        exam = self._exam(self.kwargs["exam_id"], for_write=True)
        writer = ExamSlotWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        school_class = self._validate(exam, data)

        row = ExamSlot.objects.create(
            tenant=self.tenant, exam=exam, school_class=school_class,
            subject=data["subject"], exam_date=data["exam_date"],
            sitting=data["sitting"],
            start_time=data.get("start_time"), end_time=data.get("end_time"),
            room=data.get("room"), invigilator=data.get("invigilator"),
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="ExamSlot", entity_id=str(row.pk),
            entity_label=f"{school_class.name} {data['subject'].name}",
            tenant=self.tenant, actor_user=request.user,
            summary=f"{school_class.name} {data['subject'].name} scheduled.",
        )
        row = ExamSlot.objects.select_related(
            "school_class", "subject", "room", "invigilator",
        ).get(pk=row.pk)
        payload = ExamSlotSerializer(
            row, context=self.get_serializer_context(),
        ).data
        payload["warnings"] = [
            w.as_dict() for w in exam_slot_warnings(row, visible=self.visible)
        ]
        return success_response(
            f"{school_class.name} {data['subject'].name} scheduled.",
            payload, status=201,
        )


class ExamSlotDetailView(_ExamScoped, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/exams/<exam_id>/slots/<pk>/

    docstring-name: Exam paper
    """

    serializer_class = ExamSlotSerializer
    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_TIMETABLE_UPDATE,
            "PUT": PERM_TIMETABLE_UPDATE,
            "DELETE": PERM_TIMETABLE_MANAGE,
        }.get(self.request.method, PERM_TIMETABLE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return ExamSlot.objects.filter(
            tenant=self.tenant, exam_id=self.kwargs["exam_id"],
        ).select_related("school_class", "subject", "room", "invigilator", "exam")

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        exam = self._exam(self.kwargs["exam_id"], for_write=True)
        row = self.get_object()
        writer = ExamSlotWriteSerializer(row, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        merged = {
            "school_class": data.get("school_class", row.school_class),
            "subject": data.get("subject", row.subject),
            "exam_date": data.get("exam_date", row.exam_date),
            "sitting": data.get("sitting", row.sitting),
            "room": data["room"] if "room" in data else row.room,
            "invigilator": (
                data["invigilator"] if "invigilator" in data else row.invigilator
            ),
        }
        school_class = self._validate(exam, merged, exclude_pk=row.pk)

        row.school_class = school_class
        row.subject = merged["subject"]
        row.exam_date = merged["exam_date"]
        row.sitting = merged["sitting"]
        row.room = merged["room"]
        row.invigilator = merged["invigilator"]
        if "start_time" in data:
            row.start_time = data["start_time"]
        if "end_time" in data:
            row.end_time = data["end_time"]
        row.save()

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="ExamSlot", entity_id=str(row.pk),
            entity_label=f"{school_class.name} {row.subject.name}",
            tenant=self.tenant, actor_user=request.user,
            summary=f"{school_class.name} {row.subject.name} updated.",
        )
        payload = ExamSlotSerializer(
            row, context=self.get_serializer_context(),
        ).data
        payload["warnings"] = [
            w.as_dict() for w in exam_slot_warnings(row, visible=self.visible)
        ]
        return success_response(f"{school_class.name} {row.subject.name} updated.", payload)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        self._exam(self.kwargs["exam_id"], for_write=True)
        row = self.get_object()
        label = f"{row.school_class.name} {row.subject.name}"
        row.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="ExamSlot", entity_id=str(kwargs.get("pk")),
            entity_label=label,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{label} removed.",
        )
        return success_response(f"{label} removed.")


class ExamPublishView(_ExamScoped, APIView):
    """POST /v1/academics/exams/<exam_id>/publish/

    docstring-name: Publish an exam timetable
    """

    rbac_permission = PERM_TIMETABLE_PUBLISH
    pagination_class = None

    def post(self, request, exam_id):
        exam = self._exam(exam_id)
        publish_exam(self.tenant, exam, actor=request.user, visible=self.visible)
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.ACADEMIC_TIMETABLE_PUBLISHED,
            entity_type="Exam", entity_id=str(exam.pk), entity_label=exam.name,
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{exam.name} published with "
                f"{ExamSlot.objects.filter(exam=exam).count()} papers."
            ),
        )
        return success_response(
            f"{exam.name} published.",
            ExamSerializer(exam, context=self.get_serializer_context()).data,
        )
