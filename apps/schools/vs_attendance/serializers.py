"""The shapes the attendance screens are read and written in.

Every serializer carries the Field Access mixin for the resource whose fields it
emits, so a field a role may not read is absent from what it is sent and a field
it may not write is refused. Three resources are in play: ``attendance.register``
for everything a register says about a child's day (the registers, the hubs and
the student's tab), ``attendance.correction`` for a request to change a mark
after its day has closed, and ``attendance.lesson`` for the lesson registers and
for the lesson blocks the hubs, the student's tab and the report carry. A
history that mixes resources renders each entry with the serializer of the
resource it came from.

**Lesson blocks inside a register-resource payload.** The teacher hub's
``lessons`` are rendered by a lesson serializer, so each key is held to the
lesson switches. The blocks that are made of lesson absences (the hub's
``missed_lessons`` and ``kpis.missed_a_lesson``, the student's ``lesson_absences``,
the report's ``lessons_missed``) are left out whole unless the caller may read
the lesson mark, and the figures also need the lesson counts: each says that a
lesson mark was Absent. ``morning_mark`` is the class register's mark under
another name and is dropped for a caller who may not read it there
(:class:`_MorningFollowsRegisterMark`).

They are plain serializers over the dictionaries the services build, not over
models, because these are documents assembled from rosters, marks and history
rather than rows. Those that only send are declared read-only throughout, so a
field named like a writable one (``note``, ``reason_code``) is never mistaken
for a way to write it.
"""
from __future__ import annotations

from rest_framework import serializers

from schools.vs_students.serializers import GuardianSerializer
from vs_rbac.field_enforcement import FieldAccessMixin, can_read

from .constants import (
    CHRONIC_THRESHOLD_MAX,
    CHRONIC_THRESHOLD_MIN,
    CORRECTION_FIELD_RESOURCE,
    EDIT_WINDOW_MAX,
    EDIT_WINDOW_MIN,
    FIELD_RESOURCE,
    LESSON_FIELD_RESOURCE,
    LESSON_MARK_CHOICES,
    REASON_NAME_MAX,
    STAFF_FIELD_RESOURCE,
    STAFF_MARK_CHOICES,
    Mark,
)

#: What a method field returns for a block that is left out of the response
#: rather than sent empty. Removed by the serializer that holds the field.
_OMIT = object()

#: The registry keys the lesson blocks are held to.
_REGISTER_MARK = f"{FIELD_RESOURCE}.mark"
_LESSON_MARK = f"{LESSON_FIELD_RESOURCE}.mark"
_LESSON_COUNTS = f"{LESSON_FIELD_RESOURCE}.counts"

#: The Staff Management switches a staff register row is held to for what it
#: borrows from a staff record: the job title in the role line, and the exit
#: block (the status and last working day of somebody about to leave).
_STAFF_JOB_TITLE = "school.teachers.job_title"
_STAFF_EXIT_DATE = "school.teachers.exit_date"
_STAFF_EMPLOYMENT_STATUS = "school.teachers.employment_status"


def _readable(serializer, *keys) -> bool:
    """Whether the caller of *serializer*'s request may read every registry key in *keys*."""
    request = serializer.context.get("request")
    return all(can_read(request, key) for key in keys)


class _MorningFollowsRegisterMark:
    """Drops ``morning_mark`` for a caller who may not read the mark on the class register.

    The morning mark beside a lesson is the class register's mark under another
    name, and it has a switch of its own in the lesson resource. Hiding the mark
    on the register has to hide it here as well, or a lesson row would hand a
    role the one value its register switch withholds. Mixed in ahead of
    :class:`FieldAccessMixin` and not a subclass of it, so it is not a surface.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "morning_mark" in data and not _readable(self, _REGISTER_MARK):
            data.pop("morning_mark")
        return data


class _LessonBlocks:
    """Drops whole lesson blocks the caller may not read from a register-resource payload.

    ``lesson_blocks`` maps a block's name to the registry keys that must all be
    readable for the block to be sent. A block absent from the payload (lesson
    marking is off) is left absent.
    """

    lesson_blocks: dict = {}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for name, keys in self.lesson_blocks.items():
            if name in data and not _readable(self, *keys):
                data.pop(name)
        return data


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


# ── Lessons ────────────────────────────────────────────────────────────────

class LessonRowSerializer(_MorningFollowsRegisterMark, FieldAccessMixin, serializers.Serializer):
    """One student's line on a lesson register, with their morning mark beside it."""

    field_resource = LESSON_FIELD_RESOURCE

    student_id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    student_number = serializers.CharField(read_only=True)
    locked = serializers.BooleanField(read_only=True)
    mark = serializers.CharField(read_only=True, allow_null=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    note = serializers.CharField(read_only=True)
    morning_mark = serializers.CharField(read_only=True, allow_null=True)


class LessonChangeSerializer(FieldAccessMixin, serializers.Serializer):
    """One entry in the history of a submitted lesson register."""

    field_resource = LESSON_FIELD_RESOURCE

    student_id = serializers.IntegerField(read_only=True)
    student_name = serializers.CharField(read_only=True)
    from_mark = serializers.CharField(read_only=True, allow_null=True)
    to_mark = serializers.CharField(read_only=True)
    changed_by_name = serializers.CharField(read_only=True)
    changed_at = serializers.DateTimeField(read_only=True)
    note = serializers.CharField(read_only=True)


class LessonHeaderSerializer(serializers.Serializer):
    """Which lesson a register is: its period, subject and teacher as they were taken."""

    slot_id = serializers.IntegerField(read_only=True)
    period_label = serializers.CharField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format="%H:%M")
    end_time = serializers.TimeField(read_only=True, format="%H:%M")
    subject = serializers.CharField(read_only=True)
    teacher_name = serializers.CharField(read_only=True, allow_null=True)


class LessonRegisterSerializer(FieldAccessMixin, serializers.Serializer):
    """A lesson's register for one day, as the lesson register screen reads it."""

    field_resource = LESSON_FIELD_RESOURCE

    lesson = LessonHeaderSerializer(read_only=True)
    school_class = serializers.DictField(read_only=True)
    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    roster_empty = serializers.BooleanField(read_only=True)
    counts = serializers.DictField(read_only=True)
    taken_by = serializers.DictField(read_only=True, allow_null=True)
    edit_window_open = serializers.BooleanField(read_only=True, allow_null=True)
    rows = LessonRowSerializer(many=True, read_only=True)
    history = LessonChangeSerializer(many=True, read_only=True)


class LessonMarkWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a lesson mark: Present, Late or Absent, with a time for Late.

    Whether a Late mark carries its time is a rule of the register and is
    answered by the service.
    """

    field_resource = LESSON_FIELD_RESOURCE

    mark = serializers.ChoiceField(
        choices=LESSON_MARK_CHOICES,
        error_messages={
            "invalid_choice": "Choose Present, Late or Absent. A lesson register has no Excused.",
        },
    )
    arrived_at = serializers.TimeField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)


class LessonListRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One lesson on the administrator's list of the day."""

    field_resource = LESSON_FIELD_RESOURCE

    slot_id = serializers.IntegerField(read_only=True)
    period_label = serializers.CharField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format="%H:%M")
    end_time = serializers.TimeField(read_only=True, format="%H:%M")
    school_class = serializers.DictField(read_only=True)
    branch = serializers.DictField(read_only=True, allow_null=True)
    subject = serializers.CharField(read_only=True)
    teacher_name = serializers.CharField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    submitted_by_name = serializers.CharField(read_only=True, allow_null=True)


class LessonListSerializer(serializers.Serializer):
    """The lessons of one day. Each row is held to the lesson switches."""

    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    lesson_marking_on = serializers.BooleanField(read_only=True)
    branch_dimension = serializers.BooleanField(read_only=True)
    lessons = LessonListRowSerializer(many=True, read_only=True)


class LessonCardSerializer(FieldAccessMixin, serializers.Serializer):
    """One lesson a teacher holds on the day and where its register stands."""

    field_resource = LESSON_FIELD_RESOURCE

    slot_id = serializers.IntegerField(read_only=True)
    period_label = serializers.CharField(read_only=True)
    start_time = serializers.TimeField(read_only=True, format="%H:%M")
    subject = serializers.CharField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True, allow_null=True)


class LessonMissedRowSerializer(_MorningFollowsRegisterMark, FieldAccessMixin, serializers.Serializer):
    """A student who was in school in the morning and absent from a lesson."""

    field_resource = LESSON_FIELD_RESOURCE

    student = serializers.DictField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    period_label = serializers.CharField(read_only=True)
    subject = serializers.CharField(read_only=True)
    lesson_submitted_at = serializers.DateTimeField(read_only=True)
    morning_mark = serializers.CharField(read_only=True, allow_null=True)


class LessonAbsenceSerializer(_MorningFollowsRegisterMark, FieldAccessMixin, serializers.Serializer):
    """A lesson a student was absent from, on their Attendance tab."""

    field_resource = LESSON_FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    period_label = serializers.CharField(read_only=True)
    subject = serializers.CharField(read_only=True)
    morning_mark = serializers.CharField(read_only=True, allow_null=True)


class ReportLessonsMissedRowSerializer(serializers.Serializer):
    """One subject, class and teacher in the report's table of lessons missed."""

    subject = serializers.CharField(read_only=True)
    class_name = serializers.CharField(read_only=True)
    teacher_name = serializers.CharField(read_only=True, allow_null=True)
    missed = serializers.IntegerField(read_only=True)
    students = serializers.ListField(child=serializers.CharField(), read_only=True)


# ── The staff register ─────────────────────────────────────────────────────

class StaffRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One member of staff's line on a branch's staff register.

    The role line is made of the job title, which is Staff Management's field and
    is held to its switch, and the classes the person leads, which are not. The
    exit block says what the person's employment status is and when they last
    work, which are Staff Management's employment status and exit date under other
    names: each part is sent only to a caller who may read the field it comes from,
    and the block is left out entirely when they may read neither.
    """

    field_resource = STAFF_FIELD_RESOURCE

    staff = serializers.DictField(read_only=True)
    role = serializers.SerializerMethodField()
    locked = serializers.BooleanField(read_only=True)
    lock_reason = serializers.CharField(read_only=True, allow_null=True)
    lock_note = serializers.CharField(read_only=True, allow_null=True)
    mark = serializers.CharField(read_only=True, allow_null=True)
    arrived_at = serializers.TimeField(read_only=True, format="%H:%M", allow_null=True)
    note = serializers.CharField(read_only=True)
    leave = serializers.DictField(read_only=True, allow_null=True)
    exit = serializers.DictField(read_only=True, allow_null=True)

    def get_role(self, obj):
        parts = []
        if obj.get("job_title") and _readable(self, _STAFF_JOB_TITLE):
            parts.append(obj["job_title"])
        if obj.get("leads"):
            parts.append("Class teacher \u00b7 " + ", ".join(obj["leads"]))
        return " \u00b7 ".join(parts) if parts else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "leave" not in instance:
            data.pop("leave", None)
        if "exit" in data:
            data["exit"] = self._exit_block(data["exit"])
            if data["exit"] is _OMIT:
                data.pop("exit")
        return data

    def _exit_block(self, block):
        """*block* cut down to the parts the caller may read, or ``_OMIT`` for none of it."""
        status = _readable(self, _STAFF_EMPLOYMENT_STATUS)
        day = _readable(self, _STAFF_EXIT_DATE)
        if not (status or day):
            return _OMIT
        if block is None:
            return None
        when = block["last_working_day_label"]
        if status and day:
            text = f"{block['label']} \u00b7 last working day {when}"
        elif day:
            text = f"Last working day {when}"
        else:
            text = block["label"]
        shown = {"text": text}
        if status:
            shown["label"] = block["label"]
        if day:
            shown["last_working_day"] = block["last_working_day"]
            shown["last_working_day_label"] = when
        return shown


class StaffGroupSerializer(FieldAccessMixin, serializers.Serializer):
    """One branch's staff register for the day."""

    field_resource = STAFF_FIELD_RESOURCE

    branch = serializers.DictField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    submitted_by_name = serializers.CharField(read_only=True, allow_null=True)
    edit_window_open = serializers.BooleanField(read_only=True, allow_null=True)
    counts = serializers.DictField(read_only=True)
    rows = StaffRowSerializer(many=True, read_only=True)


class StaffNoHomeBranchSerializer(serializers.Serializer):
    """The people based at no branch, locked until Staff Management gives them one."""

    count = serializers.IntegerField(read_only=True)
    rows = StaffRowSerializer(many=True, read_only=True)


class StaffRegisterSerializer(serializers.Serializer):
    """A day's staff registers. Each group and row is held to the staff switches."""

    date = serializers.DateField(read_only=True)
    date_label = serializers.CharField(read_only=True)
    closed = serializers.DictField(read_only=True, allow_null=True)
    branch_dimension = serializers.BooleanField(read_only=True)
    groups = StaffGroupSerializer(many=True, read_only=True)
    no_home_branch = StaffNoHomeBranchSerializer(read_only=True, allow_null=True)


class StaffMarkWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a staff mark: Present, Late or Absent, with a time for Late.

    Whether a Late mark carries its time is a rule of the register and is
    answered by the service.
    """

    field_resource = STAFF_FIELD_RESOURCE

    mark = serializers.ChoiceField(
        choices=STAFF_MARK_CHOICES,
        error_messages={
            "invalid_choice": "Choose Present, Late or Absent. A member of staff "
                              "away with permission is on leave, which is filled in for you.",
        },
    )
    arrived_at = serializers.TimeField(required=False, allow_null=True)
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


class HubSerializer(_LessonBlocks, FieldAccessMixin, serializers.Serializer):
    """The administrator's view of one day across the classes in scope.

    ``missed_lessons`` and ``kpis.missed_a_lesson`` are present only when a class
    in scope marks lessons. The list needs the lesson mark to be readable and
    the count needs the lesson counts as well.
    """

    field_resource = FIELD_RESOURCE
    lesson_blocks = {"missed_lessons": (_LESSON_MARK,)}

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
    missed_lessons = LessonMissedRowSerializer(many=True, read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        kpis = data.get("kpis")
        if kpis and "missed_a_lesson" in kpis and not _readable(
            self, _LESSON_MARK, _LESSON_COUNTS,
        ):
            kpis.pop("missed_a_lesson")
        return data


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
    lesson_marking_on = serializers.BooleanField(read_only=True)
    lessons = LessonCardSerializer(many=True, read_only=True)


# ── The student's tab ──────────────────────────────────────────────────────

class StudentDaySerializer(FieldAccessMixin, serializers.Serializer):
    """One day of a student's term.

    ``state`` says whether the day was marked, not taken, closed, a day the
    pupil was suspended or a day they were not on the roll; ``mark`` is the mark
    itself and is the field Field Access hides. ``class_id`` is the class the
    pupil was placed in that day, whose register the day belongs to: an
    internal reference, not a field of the child's record.
    """

    field_resource = FIELD_RESOURCE

    date = serializers.DateField(read_only=True)
    state = serializers.CharField(read_only=True)
    class_id = serializers.IntegerField(read_only=True, allow_null=True)
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


class StudentAttendanceSerializer(_LessonBlocks, FieldAccessMixin, serializers.Serializer):
    """A student's Attendance tab for one term.

    The primary guardian is read through the student module's own guardian
    serializer, so the switches on a guardian's name and email address that
    govern the student's record govern this tab as well. A guardian whose name
    and email are both hidden from the caller is left out, and a hidden one of
    the two is left out alone, never sent blank. The history is rendered entry
    by entry with the serializer of the resource each came from.
    """

    field_resource = FIELD_RESOURCE
    lesson_blocks = {"lesson_absences": (_LESSON_MARK,)}

    student = serializers.DictField(read_only=True)
    primary_guardian = serializers.SerializerMethodField()
    term = serializers.DictField(read_only=True, allow_null=True)
    terms = serializers.ListField(child=serializers.DictField(), read_only=True)
    counts = serializers.DictField(read_only=True)
    days = StudentDaySerializer(many=True, read_only=True)
    absences = StudentAbsenceSerializer(many=True, read_only=True)
    lesson_absences = LessonAbsenceSerializer(many=True, read_only=True)
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


class ReportSerializer(_LessonBlocks, FieldAccessMixin, serializers.Serializer):
    """The attendance report for one term.

    The weekly trend, the by-level table and the chronic list are made of
    figures (the chronic list is who falls below a rate), so they follow the
    switch on the register's counts, and the list of registers not taken
    follows the one on the register's status. The table of lessons missed (present
    only when a class in scope marks lessons) is made of figures about lesson
    absences and needs both the lesson mark and the lesson counts.
    """

    field_resource = FIELD_RESOURCE
    lesson_blocks = {"lessons_missed": (_LESSON_MARK, _LESSON_COUNTS)}

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
    lessons_missed = ReportLessonsMissedRowSerializer(many=True, read_only=True)


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
