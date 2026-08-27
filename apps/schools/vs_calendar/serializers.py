"""What each surface returns, and what it accepts.

Two rules run through every serializer here and are worth stating once.

**The branch dimension recedes.** Where a school has one branch, no response
carries a branch field and no form asks the question - absent, not blank and not
disabled, because a control with a single option is noise. Nothing about the
stored data changes with it: the column is there, and the fields appear when a
second branch opens without a row being rewritten. ``multi_branch`` in the
serializer context is the switch.

**A person is an id and a display name, never an email address.** A class
timetable is the most widely read document a school produces, and an email
address on it is a disclosure the school never chose. The same rule applies to
``created_by``.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    CalendarEvent,
    ClassTimetable,
    DayOfWeek,
    Exam,
    ExamSlot,
    Period,
    PublishState,
    Room,
    TimetableSlot,
)
from .services.calendar import audience_labels
from .services.teachers import display_name


class _Scoped(serializers.ModelSerializer):
    """Drops the branch fields where the school has only one branch."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("multi_branch", True):
            for key in ("branch", "branch_name", "scope_label"):
                data.pop(key, None)
        return data


class PersonSerializer(serializers.Serializer):
    """A teacher or an invigilator, as everything here renders one."""

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()

    def get_name(self, obj):
        return display_name(obj)


# ── Calendar events ────────────────────────────────────────────────────────

class CalendarEventSerializer(_Scoped):
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    scope_label = serializers.SerializerMethodField()
    type_label = serializers.CharField(source="get_event_type_display", read_only=True)
    term = serializers.SerializerMethodField()
    audience = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "name", "event_type", "type_label",
            "start_date", "end_date", "closes_school", "description",
            "branch", "branch_name", "scope_label",
            "term", "audience",
        ]

    def get_scope_label(self, obj):
        return obj.branch.name if obj.branch_id else "School-wide"

    def get_term(self, obj):
        """The term the event falls in, or None meaning outside every term.

        None is a real answer the screen renders as "Outside every term" and the
        overview raises an alert about. It is never the same as an archived
        term, which is still a term and still reported.
        """
        term = (self.context.get("terms_by_event") or {}).get(obj.pk, "__unset__")
        if term != "__unset__":
            return term
        from .services.calendar import term_of

        found = term_of(obj.session, obj.start_date)
        return {"id": found.pk, "name": found.name} if found else None

    def get_audience(self, obj):
        """Who the event covers. Absent when it covers everybody.

        An empty list would render as "Applies to: none" on a screen, which is
        the opposite of what no rows mean.
        """
        rows = audience_labels(obj)
        return rows or None


class CalendarEventWriteSerializer(serializers.ModelSerializer):
    """Create or edit an event, with its audience, in one call.

    ``audience`` is a list of ``{"type": "level"|"class", "id": n}``. An empty
    list or an absent field means the event applies to everybody in its branch
    scope, which is the default and the common case.
    """

    audience = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True,
    )
    branch = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "name", "event_type", "start_date", "end_date",
            "closes_school", "description", "branch", "audience",
        ]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            from .exceptions import InvalidDateRange

            raise InvalidDateRange()
        return attrs


# ── Rooms ──────────────────────────────────────────────────────────────────

class RoomSerializer(_Scoped):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    type_label = serializers.CharField(source="get_room_type_display", read_only=True)
    usage = serializers.SerializerMethodField()

    class Meta:
        model = Room
        fields = [
            "id", "name", "code", "room_type", "type_label",
            "branch", "branch_name", "capacity", "is_active", "usage",
        ]

    def get_usage(self, obj):
        """What is scheduled here, in words the delete refusal reuses.

        Not in the FRD's FR-011; the design's room card shows it and the room
        delete refusal is worded from it, so it is one annotation rather than a
        second endpoint.
        """
        lessons = getattr(obj, "lesson_count", None)
        papers = getattr(obj, "paper_count", None)
        if lessons is None:
            lessons = obj.slots.count()
        if papers is None:
            papers = obj.exam_slots.count()
        return {
            "lessons": lessons,
            "exam_papers": papers,
            "label": _usage_label(lessons, papers),
        }


def _usage_label(lessons, papers) -> str:
    if not lessons and not papers:
        return "Nothing scheduled here yet"
    parts = []
    if lessons:
        parts.append(f"{lessons} lesson" + ("" if lessons == 1 else "s"))
    if papers:
        parts.append(f"{papers} exam paper" + ("" if papers == 1 else "s"))
    return " · ".join(parts)


class RoomWriteSerializer(serializers.ModelSerializer):
    branch = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Room
        fields = ["id", "name", "code", "room_type", "branch", "capacity", "is_active"]


# ── Periods ────────────────────────────────────────────────────────────────

class PeriodSerializer(_Scoped):
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    scope_label = serializers.SerializerMethodField()
    type_label = serializers.CharField(source="get_period_type_display", read_only=True)
    day_label = serializers.SerializerMethodField()

    class Meta:
        model = Period
        fields = [
            "id", "label", "order_index", "start_time", "end_time",
            "period_type", "type_label", "day_of_week", "day_label",
            "branch", "branch_name", "scope_label", "is_active",
        ]

    def get_scope_label(self, obj):
        return obj.branch.name if obj.branch_id else "School-wide"

    def get_day_label(self, obj):
        return DayOfWeek(obj.day_of_week).label if obj.day_of_week else "Every day"


class PeriodWriteSerializer(serializers.ModelSerializer):
    branch = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Period
        # order_index is deliberately absent: it is computed from the times.
        # The design's form has no order field, so there is nothing to accept.
        fields = [
            "id", "label", "start_time", "end_time", "period_type",
            "day_of_week", "branch", "is_active",
        ]


# ── Timetable ──────────────────────────────────────────────────────────────

class TimetableSlotSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    teacher = serializers.SerializerMethodField()
    room_name = serializers.CharField(source="room.name", read_only=True, default=None)
    period_label = serializers.CharField(source="period.label", read_only=True)
    class_name = serializers.CharField(source="school_class.name", read_only=True)

    class Meta:
        model = TimetableSlot
        fields = [
            "id", "school_class", "class_name", "day_of_week", "period",
            "period_label", "subject", "subject_name",
            "teacher", "room", "room_name",
        ]

    def get_teacher(self, obj):
        if obj.teacher_id is None:
            return None
        return {"id": obj.teacher_id, "name": display_name(obj.teacher)}


class TimetableSlotWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimetableSlot
        fields = [
            "id", "school_class", "day_of_week", "period", "subject",
            "teacher", "room",
        ]


class ClassTimetableStatusSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = ClassTimetable
        fields = ["status", "status_label", "published_at"]


# ── Exams ──────────────────────────────────────────────────────────────────

class ExamSlotSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source="school_class.name", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    room_name = serializers.CharField(source="room.name", read_only=True, default=None)
    sitting_label = serializers.CharField(source="get_sitting_display", read_only=True)
    invigilator = serializers.SerializerMethodField()

    class Meta:
        model = ExamSlot
        fields = [
            "id", "school_class", "class_name", "subject", "subject_name",
            "exam_date", "sitting", "sitting_label", "start_time", "end_time",
            "room", "room_name", "invigilator",
        ]

    def get_invigilator(self, obj):
        if obj.invigilator_id is None:
            return None
        return {"id": obj.invigilator_id, "name": display_name(obj.invigilator)}


class ExamSlotWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamSlot
        fields = [
            "id", "school_class", "subject", "exam_date", "sitting",
            "start_time", "end_time", "room", "invigilator",
        ]


class ExamSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    event_name = serializers.CharField(source="calendar_event.name", read_only=True)
    start_date = serializers.DateField(source="calendar_event.start_date", read_only=True)
    end_date = serializers.DateField(source="calendar_event.end_date", read_only=True)

    class Meta:
        model = Exam
        fields = [
            "id", "name", "calendar_event", "event_name",
            "start_date", "end_date", "status", "status_label", "published_at",
        ]
