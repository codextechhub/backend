"""The shapes the attendance screens are read and written in.

Every serializer carries the Field Access mixin for the resource whose fields it
emits, so a field a role may not read is absent from what it is sent and a field
it may not write is refused. Two resources are in play: ``attendance.register``
for everything a register says about a child's day (the registers, the hubs and
the student's tab), and ``attendance.correction`` for a request to change a mark
after its day has closed. A history that mixes the two renders each entry with
the serializer of the resource it came from.

They are plain serializers over the dictionaries the services build, not over
models, because these are documents assembled from rosters, marks and history
rather than rows. Those that only send are declared read-only throughout, so a
field named like a writable one (``note``, ``reason_code``) is never mistaken
for a way to write it.
"""
from __future__ import annotations

from rest_framework import serializers

from schools.vs_students.serializers import GuardianSerializer
from vs_rbac.field_enforcement import FieldAccessMixin

from .constants import (
    CHRONIC_THRESHOLD_MAX,
    CHRONIC_THRESHOLD_MIN,
    CORRECTION_FIELD_RESOURCE,
    EDIT_WINDOW_MAX,
    EDIT_WINDOW_MIN,
    FIELD_RESOURCE,
    REASON_NAME_MAX,
    Mark,
)

#: What a method field returns for a block that is left out of the response
#: rather than sent empty. Removed by the serializer that holds the field.
_OMIT = object()


# ── The register ───────────────────────────────────────────────────────────

class RegisterRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One student's line on a register."""

    field_resource = FIELD_RESOURCE

    student_id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    student_number = serializers.CharField(read_only=True)
    locked = serializers.BooleanField(read_only=True)
    mark = serializers.CharField(read_only=True, allow_null=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    reason_code = serializers.IntegerField(read_only=True, allow_null=True)
    reason_label = serializers.CharField(read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True)
    notice_state = serializers.CharField(read_only=True)
    notice_sent_at = serializers.DateTimeField(read_only=True, allow_null=True)


class MarkChangeSerializer(FieldAccessMixin, serializers.Serializer):
    """One entry in the history of a submitted register: who changed what, and when."""

    field_resource = FIELD_RESOURCE

    student_id = serializers.IntegerField(read_only=True)
    student_name = serializers.CharField(read_only=True)
    from_mark = serializers.CharField(read_only=True, allow_null=True)
    to_mark = serializers.CharField(read_only=True)
    changed_by_name = serializers.CharField(read_only=True)
    changed_at = serializers.DateTimeField(read_only=True)
    via = serializers.CharField(read_only=True)
    note = serializers.CharField(read_only=True)


class RegisterSerializer(FieldAccessMixin, serializers.Serializer):
    """A class's register for one day, as the register screen reads it."""

    field_resource = FIELD_RESOURCE

    school_class = serializers.DictField(read_only=True)
    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    roster_empty = serializers.BooleanField(read_only=True)
    counts = serializers.DictField(read_only=True)
    taken_by = serializers.DictField(read_only=True, allow_null=True)
    edit_window_open = serializers.BooleanField(read_only=True, allow_null=True)
    rows = RegisterRowSerializer(many=True, read_only=True)
    history = MarkChangeSerializer(many=True, read_only=True)
    submit_preview = serializers.DictField(read_only=True, allow_null=True)


class MarkWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a mark: the whole of what one row holds.

    Whether the row's details fit its mark (a late mark needs a time, an
    excused one a live reason) is a rule of the register and is answered by the
    service, which knows the school's reasons.
    """

    field_resource = FIELD_RESOURCE

    mark = serializers.ChoiceField(choices=Mark.choices)
    arrived_at = serializers.TimeField(required=False, allow_null=True)
    reason_code = serializers.IntegerField(
        source="reason_code_id", required=False, allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)


# ── Corrections ────────────────────────────────────────────────────────────

class CorrectionSerializer(FieldAccessMixin, serializers.Serializer):
    """One correction request, in the queue, on the hub and after it is decided."""

    field_resource = CORRECTION_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    student = serializers.DictField(read_only=True)
    class_id = serializers.IntegerField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    branch = serializers.DictField(read_only=True, allow_null=True)
    date = serializers.DateField(read_only=True)
    from_mark = serializers.CharField(read_only=True)
    to_mark = serializers.CharField(read_only=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    reason_code = serializers.IntegerField(read_only=True, allow_null=True)
    reason_label = serializers.CharField(read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True)
    requested_by_name = serializers.CharField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)
    status = serializers.CharField(read_only=True)
    decided_by_name = serializers.CharField(read_only=True)
    decided_at = serializers.DateTimeField(read_only=True, allow_null=True)
    decision_note = serializers.CharField(read_only=True)
    is_mine = serializers.BooleanField(read_only=True)


class CorrectionCreateSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a request for a correction.

    Which register and which student travel beside the new mark, because a
    correction is asked for from a screen showing a closed day, not from a
    resource of its own. Whether the day has really closed, whether the reason
    is live and whether it needs a note are rules of the correction and are
    answered by the service.
    """

    field_resource = CORRECTION_FIELD_RESOURCE

    register_class_id = serializers.IntegerField()
    date = serializers.DateField()
    student_id = serializers.IntegerField()
    to_mark = serializers.ChoiceField(choices=Mark.choices)
    reason_code = serializers.IntegerField(
        source="reason_code_id", required=False, allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)
    arrived_at = serializers.TimeField(required=False, allow_null=True)


class CorrectionDecisionSerializer(FieldAccessMixin, serializers.Serializer):
    """The optional body of an approval or a rejection."""

    field_resource = CORRECTION_FIELD_RESOURCE

    decision_note = serializers.CharField(required=False, allow_blank=True, max_length=300)


class StudentCorrectionSerializer(FieldAccessMixin, serializers.Serializer):
    """A decided correction as an entry in a student's history."""

    field_resource = CORRECTION_FIELD_RESOURCE

    kind = serializers.CharField(read_only=True)
    at = serializers.DateTimeField(read_only=True)
    date = serializers.DateField(read_only=True)
    from_mark = serializers.CharField(read_only=True)
    to_mark = serializers.CharField(read_only=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    status = serializers.CharField(read_only=True)
    via = serializers.CharField(read_only=True)
    reason_code = serializers.IntegerField(read_only=True, allow_null=True)
    reason_label = serializers.CharField(read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True)
    requested_by_name = serializers.CharField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)
    decided_by_name = serializers.CharField(read_only=True)
    decided_at = serializers.DateTimeField(read_only=True)
    decision_note = serializers.CharField(read_only=True)


# ── Today ──────────────────────────────────────────────────────────────────

class NotTakenRowSerializer(serializers.Serializer):
    """A class whose register is due and has not been submitted."""

    class_id = serializers.IntegerField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    level_name = serializers.CharField(read_only=True)
    class_teacher_name = serializers.CharField(read_only=True, allow_null=True)
    branch = serializers.DictField(read_only=True, allow_null=True)
    reminded = serializers.BooleanField(read_only=True)


class HubAbsentRowSerializer(FieldAccessMixin, serializers.Serializer):
    """A child marked absent or excused on a submitted register."""

    field_resource = FIELD_RESOURCE

    student = serializers.DictField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    mark = serializers.CharField(read_only=True)
    reason_code = serializers.IntegerField(read_only=True, allow_null=True)
    reason_label = serializers.CharField(read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True)
    notice_state = serializers.CharField(read_only=True)
    notice_sent_at = serializers.DateTimeField(read_only=True, allow_null=True)


class HubSerializer(FieldAccessMixin, serializers.Serializer):
    """The administrator's view of one day across the classes in scope."""

    field_resource = FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    kpis = serializers.DictField(read_only=True)
    not_taken = NotTakenRowSerializer(many=True, read_only=True)
    absent = HubAbsentRowSerializer(many=True, read_only=True)
    pending_corrections_preview = CorrectionSerializer(read_only=True, allow_null=True)
    branch_dimension = serializers.BooleanField(read_only=True)
    classes_without_branch = serializers.ListField(
        child=serializers.CharField(), read_only=True,
    )


class TeacherCardSerializer(FieldAccessMixin, serializers.Serializer):
    """One class a teacher leads and where its register stands."""

    field_resource = FIELD_RESOURCE

    class_id = serializers.IntegerField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    student_count = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    submitted_by_name = serializers.CharField(read_only=True, allow_null=True)


class TeacherHubSerializer(FieldAccessMixin, serializers.Serializer):
    """A teacher's own registers for one day."""

    field_resource = FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    classes = TeacherCardSerializer(many=True, read_only=True)


# ── The student's tab ──────────────────────────────────────────────────────

class StudentDaySerializer(FieldAccessMixin, serializers.Serializer):
    """One day of a student's term.

    ``state`` says whether the day was marked, not taken, closed, a day the
    pupil was suspended or a day they were not on the roll; ``mark`` is the mark
    itself and is the field Field Access hides.
    """

    field_resource = FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    state = serializers.CharField(read_only=True)
    mark = serializers.CharField(read_only=True, allow_null=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    closed_reason = serializers.CharField(read_only=True, allow_null=True)


class StudentAbsenceSerializer(FieldAccessMixin, serializers.Serializer):
    """A day the student was absent or excused."""

    field_resource = FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    mark = serializers.CharField(read_only=True)
    reason_code = serializers.IntegerField(read_only=True, allow_null=True)
    reason_label = serializers.CharField(read_only=True, allow_null=True)
    note = serializers.CharField(read_only=True)
    notice_state = serializers.CharField(read_only=True)
    notice_sent_at = serializers.DateTimeField(read_only=True, allow_null=True)


class StudentChangeSerializer(FieldAccessMixin, serializers.Serializer):
    """A change made in place to a student's mark, as an entry in their history."""

    field_resource = FIELD_RESOURCE

    kind = serializers.CharField(read_only=True)
    at = serializers.DateTimeField(read_only=True)
    date = serializers.DateField(read_only=True)
    from_mark = serializers.CharField(read_only=True, allow_null=True)
    to_mark = serializers.CharField(read_only=True)
    changed_by_name = serializers.CharField(read_only=True)
    via = serializers.CharField(read_only=True)
    note = serializers.CharField(read_only=True)


class StudentAttendanceSerializer(FieldAccessMixin, serializers.Serializer):
    """A student's Attendance tab for one term.

    The primary guardian is read through the student module's own guardian
    serializer, so the switches on a guardian's name and email address that
    govern the student's record govern this tab as well. A guardian whose name
    and email are both hidden from the caller is left out, and a hidden one of
    the two is left out alone, never sent blank. The history is rendered entry
    by entry with the serializer of the resource each came from.
    """

    field_resource = FIELD_RESOURCE

    student = serializers.DictField(read_only=True)
    primary_guardian = serializers.SerializerMethodField()
    term = serializers.DictField(read_only=True, allow_null=True)
    terms = serializers.ListField(child=serializers.DictField(), read_only=True)
    counts = serializers.DictField(read_only=True)
    days = StudentDaySerializer(many=True, read_only=True)
    absences = StudentAbsenceSerializer(many=True, read_only=True)
    history = serializers.SerializerMethodField()

    def get_primary_guardian(self, obj):
        guardian = obj.get("guardian")
        if guardian is None:
            return None
        data = GuardianSerializer(guardian, context=self.context).data
        out = {}
        name = (data.get("full_name") or "").strip()
        if name:
            out["name"] = name
        if "email" in data:
            out["email"] = data["email"] or None
        return out or _OMIT

    def get_history(self, obj):
        return [
            (
                StudentChangeSerializer if entry["kind"] == "change"
                else StudentCorrectionSerializer
            )(entry, context=self.context).data
            for entry in obj["history"]
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("primary_guardian") is _OMIT:
            data.pop("primary_guardian")
        return data


# ── Reports ────────────────────────────────────────────────────────────────

class ReportTrendRowSerializer(serializers.Serializer):
    """One week of the term and the rate of the registers taken in it."""

    week_start = serializers.DateField(read_only=True)
    label = serializers.CharField(read_only=True)
    rate = serializers.FloatField(read_only=True, allow_null=True)


class ReportClassRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One class in the report: who it is, and its figures under ``counts``.

    The figures are nested so that Field Access can take them away together and
    leave the class itself, which a role that may read the rows but not the
    totals still needs to see.
    """

    field_resource = FIELD_RESOURCE

    class_id = serializers.IntegerField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    level_name = serializers.CharField(read_only=True)
    branch = serializers.DictField(read_only=True, allow_null=True)
    counts = serializers.DictField(read_only=True)


class ReportLevelRowSerializer(serializers.Serializer):
    """One level, its classes added up."""

    level_name = serializers.CharField(read_only=True)
    present = serializers.IntegerField(read_only=True)
    late = serializers.IntegerField(read_only=True)
    absent = serializers.IntegerField(read_only=True)
    excused = serializers.IntegerField(read_only=True)
    not_taken = serializers.IntegerField(read_only=True)
    rate = serializers.FloatField(read_only=True, allow_null=True)


class ReportChronicRowSerializer(serializers.Serializer):
    """A student whose term rate is below the school's threshold."""

    student = serializers.DictField(read_only=True)
    class_name = serializers.CharField(read_only=True, allow_null=True)
    rate = serializers.FloatField(read_only=True)
    absent = serializers.IntegerField(read_only=True)
    not_taken = serializers.IntegerField(read_only=True)


class ReportNotTakenRowSerializer(serializers.Serializer):
    """A day a class owed a register and none was submitted."""

    date = serializers.DateField(read_only=True)
    class_id = serializers.IntegerField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    level_name = serializers.CharField(read_only=True)
    class_teacher_name = serializers.CharField(read_only=True, allow_null=True)
    branch = serializers.DictField(read_only=True, allow_null=True)


class ReportRangeSerializer(FieldAccessMixin, serializers.Serializer):
    """The days a report covers; the count of school days is a figure."""

    field_resource = FIELD_RESOURCE

    start = serializers.DateField(read_only=True, allow_null=True)
    end = serializers.DateField(read_only=True, allow_null=True)
    school_days = serializers.IntegerField(read_only=True)


class ReportSerializer(FieldAccessMixin, serializers.Serializer):
    """The attendance report for one term.

    The weekly trend, the by-level table and the chronic list are made of
    figures (the chronic list is who falls below a rate), so they follow the
    switch on the register's counts, and the list of registers not taken
    follows the one on the register's status.
    """

    field_resource = FIELD_RESOURCE

    term = serializers.DictField(read_only=True, allow_null=True)
    terms = serializers.ListField(child=serializers.DictField(), read_only=True)
    range = ReportRangeSerializer(read_only=True)
    trend = ReportTrendRowSerializer(many=True, read_only=True)
    by_class = ReportClassRowSerializer(many=True, read_only=True)
    by_level = ReportLevelRowSerializer(many=True, read_only=True)
    chronic = ReportChronicRowSerializer(many=True, read_only=True)
    threshold = serializers.IntegerField(read_only=True)
    not_taken = ReportNotTakenRowSerializer(many=True, read_only=True)
    branch_dimension = serializers.BooleanField(read_only=True)


# ── Settings and reasons ───────────────────────────────────────────────────

class SettingsWriteSerializer(serializers.Serializer):
    """The body of a change to the school's attendance settings: any of the four.

    Whether a level code exists at this school is answered by the service,
    which knows the school's levels.
    """

    guardian_notices = serializers.BooleanField(required=False)
    chronic_threshold = serializers.IntegerField(
        required=False, min_value=CHRONIC_THRESHOLD_MIN, max_value=CHRONIC_THRESHOLD_MAX,
        error_messages={
            "invalid": "Give the threshold as a whole number from 50 to 100.",
            "min_value": "The threshold cannot be below 50 percent.",
            "max_value": "The threshold cannot be above 100 percent.",
        },
    )
    lesson_marking_levels = serializers.ListField(
        required=False, allow_empty=True,
        child=serializers.CharField(max_length=20, trim_whitespace=True),
        error_messages={"not_a_list": "Send the level codes as a list."},
    )
    edit_window_days = serializers.IntegerField(
        required=False, min_value=EDIT_WINDOW_MIN, max_value=EDIT_WINDOW_MAX,
        error_messages={
            "invalid": "Give the number of days as a whole number from 0 to 14.",
            "min_value": "The edit window cannot be negative.",
            "max_value": "A register cannot stay open to changes for more than 14 days.",
        },
    )


class ReasonCodeSerializer(FieldAccessMixin, serializers.Serializer):
    """One of the school's reasons for excusing an absence.

    The name and the two flags are the school's configuration, not a child's
    record, and follow the permission on the settings; how many marks used the
    reason this term is a count of marks and follows the register's counts.
    """

    field_resource = FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    counts_as_authorised = serializers.BooleanField(read_only=True)
    requires_note = serializers.BooleanField(read_only=True)
    retired = serializers.BooleanField(read_only=True)
    used_this_term = serializers.IntegerField(read_only=True)


class ReasonCodeWriteSerializer(serializers.Serializer):
    """The body of a new reason, or of a change to one."""

    name = serializers.CharField(
        max_length=REASON_NAME_MAX, trim_whitespace=True,
        error_messages={
            "blank": "Give the reason a name.",
            "required": "Give the reason a name.",
            "null": "Give the reason a name.",
            "max_length": f"A reason's name can be at most {REASON_NAME_MAX} characters.",
        },
    )
    counts_as_authorised = serializers.BooleanField(required=False)
    requires_note = serializers.BooleanField(required=False)
