"""The shapes the gradebook screens are read and written in.

Every serializer that carries a registered field has the Field Access mixin for
the resource it belongs to, so a field a role may not read is absent from what
it is sent and a field it may not write is refused. Five resources are in play
(:mod:`.field_access`): ``gradebook.setup`` for assessment setup,
``gradebook.sheet`` for the score sheet, ``gradebook.result`` for a student's
compiled result and its release, ``gradebook.hold`` for fee holds and
``gradebook.amendment`` for the reason and decision note of a correction to a
published result.

They are plain serializers over the dictionaries the services build, not over
models, because these are documents assembled from sheets, rosters and settings
rather than rows. Those that only send are declared read-only throughout, so a
field named like a writable one is never mistaken for a way to write it.

**Setup** (structures, scales, remark templates) is served by ``views/setup.py``.
**The sheet, result and hold serializers** declare the shape of the score
sheet, the compiled result, the release list and the hold list. They are the
surfaces their fields are registered against, and every endpoint that sends one
of those documents renders it through them.

**Derived verdicts follow the field they describe.** A structure's ``total`` and
``total_check`` follow the switch for component maximums, and a scale's ``check``
follows the switch for its bands, because each states the numbers behind it: a
message such as "the components add up to 99" would hand a role the maximums its
switch withholds.
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework import serializers

from schools.vs_academics.class_teacher_field import ClassTeacherReadGate
from schools.vs_students.serializers import GuardianSerializer
from vs_rbac.field_enforcement import FieldAccessMixin, can_read

from .constants import (
    AMENDMENT_FIELD_RESOURCE,
    AMENDMENT_REASON_MAX,
    CARD_GOVERNED_BY,
    DECISION_NOTE_MAX,
    BAND_GRADE_MAX,
    BAND_MIN_SCORE_MAX,
    BAND_MIN_SCORE_MIN,
    BAND_REMARK_MAX,
    BANDS_MAX,
    CELLS_MAX,
    COMPONENT_MAX_SCORE_MAX,
    COMPONENT_MAX_SCORE_MIN,
    COMPONENT_NAME_MAX,
    COMPONENT_SHORT_NAME_MAX,
    COMPONENTS_MAX,
    ATTENDANCE_COUNTS_FIELD,
    ATTENDANCE_STATUS_FIELD,
    HOLD_FIELD_RESOURCE,
    OVERALL_AVERAGE_FIELD,
    POSITION_FIELD,
    RATINGS_FIELD,
    HOLD_NOTE_MAX,
    HOLD_REASON_MAX,
    IMPORT_MAX_ROWS,
    PRINCIPAL_COMMENT_MAX,
    PUBLISH_CLASSES_MAX,
    REMARK_TEMPLATES_FIELD,
    TEACHER_COMMENT_MAX,
    RATING_TRAIT_NAME_MAX,
    RATING_TRAITS_MAX,
    RATING_TRAITS_MIN,
    REMARK_TEXT_MAX,
    RETURN_NOTE_MAX,
    REMARK_TEMPLATES_MAX,
    RESULT_FIELD_RESOURCE,
    SETUP_FIELD_RESOURCE,
    SETUP_NAME_MAX,
    SHEET_FIELD_RESOURCE,
)

# ── Assessment setup ───────────────────────────────────────────────────────


class CheckSerializer(serializers.Serializer):
    """The verdict on a structure or a scale: whether it can be put in use, and why not."""

    ok = serializers.BooleanField(read_only=True)
    message = serializers.CharField(read_only=True)


class ComponentSerializer(FieldAccessMixin, serializers.Serializer):
    """One column of an assessment structure."""

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    short_name = serializers.CharField(read_only=True)
    max_score = serializers.IntegerField(read_only=True)
    is_exam = serializers.BooleanField(read_only=True)
    order = serializers.IntegerField(read_only=True)


class StructureSerializer(FieldAccessMixin, serializers.Serializer):
    """An assessment structure with its components, whether it is locked and why.

    ``branch`` is null for a structure that applies to every branch.
    ``scope_label`` and ``is_shared`` are present only at a school with several
    branches, where the branch is worth showing; at a school with one they are
    absent and nothing on the screen names a branch.
    """

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    program = serializers.IntegerField(read_only=True)
    program_name = serializers.CharField(read_only=True)
    branch = serializers.IntegerField(read_only=True, allow_null=True)
    scope_label = serializers.CharField(read_only=True)
    is_shared = serializers.BooleanField(read_only=True)
    status = serializers.CharField(read_only=True)
    copied_from = serializers.IntegerField(read_only=True, allow_null=True)
    components = ComponentSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True)
    total_check = CheckSerializer(read_only=True)
    locked = serializers.BooleanField(read_only=True)
    lock_reason = serializers.CharField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)


class ComponentWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """One component in the body of a create or an edit.

    ``id`` names an existing component being kept (and possibly changed); a
    component without one is new. Components of the structure that the list
    leaves out are removed.
    """

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(required=False)
    name = serializers.CharField(max_length=COMPONENT_NAME_MAX, trim_whitespace=True)
    short_name = serializers.CharField(
        max_length=COMPONENT_SHORT_NAME_MAX, trim_whitespace=True,
        error_messages={
            "max_length": f"Keep the short name to {COMPONENT_SHORT_NAME_MAX} characters.",
        },
    )
    max_score = serializers.IntegerField(
        min_value=COMPONENT_MAX_SCORE_MIN, max_value=COMPONENT_MAX_SCORE_MAX,
        error_messages={
            "invalid": "Give the maximum as a whole number of marks.",
            "min_value": "A component must be worth at least 1 mark.",
            "max_value": "A component cannot be worth more than 100 marks.",
        },
    )
    is_exam = serializers.BooleanField(required=False, default=False)


def _distinct(rows, key, message, field):
    seen = set()
    for row in rows:
        value = str(row[key]).strip().lower()
        if value in seen:
            raise serializers.ValidationError({field: [message]})
        seen.add(value)


class _SetupWriteBody:
    """What a structure's and a scale's write bodies share.

    Every top-level field is optional so one serializer serves a create and an
    edit; a create (``context["creating"]``) must name all of ``required_on_create``.
    An edit sends only what it changes, and cannot change the programme.
    """

    required_on_create: tuple = ()
    required_messages = {
        "name": "Give it a name.",
        "program": "Choose the programme it is for.",
        "components": "Add at least one component.",
        "bands": "Add at least one band.",
    }

    def validate(self, attrs):
        if self.context.get("creating"):
            missing = {
                name: [self.required_messages[name]]
                for name in self.required_on_create if name not in attrs
            }
            if missing:
                raise serializers.ValidationError(missing)
        elif "program" in attrs:
            raise serializers.ValidationError(
                {"program": ["The programme cannot be changed. Create a new one instead."]},
            )
        return attrs


class StructureWriteSerializer(_SetupWriteBody, FieldAccessMixin, serializers.Serializer):
    """The body of a new structure, or of an edit (any of name, branch, components).

    ``program`` is chosen when the structure is created and cannot be changed
    afterwards. Whether the program and branch exist at this school is answered
    by the service.
    """

    field_resource = SETUP_FIELD_RESOURCE
    required_on_create = ("name", "program", "components")

    name = serializers.CharField(max_length=SETUP_NAME_MAX, trim_whitespace=True, required=False)
    program = serializers.IntegerField(required=False)
    branch = serializers.IntegerField(allow_null=True, required=False)
    components = ComponentWriteSerializer(
        many=True, allow_empty=False, max_length=COMPONENTS_MAX, required=False,
    )

    def validate_components(self, rows):
        _distinct(rows, "name", "Two components have the same name. Give each its own.", "components")
        _distinct(
            rows, "short_name",
            "Two components have the same short name. Give each its own.", "components",
        )
        return rows


# ── Grading scales ─────────────────────────────────────────────────────────


class BandSerializer(FieldAccessMixin, serializers.Serializer):
    """One band of a grading scale."""

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    min_score = serializers.IntegerField(read_only=True)
    grade = serializers.CharField(read_only=True)
    remark = serializers.CharField(read_only=True)
    is_fail = serializers.BooleanField(read_only=True)
    order = serializers.IntegerField(read_only=True)


class ScaleSerializer(FieldAccessMixin, serializers.Serializer):
    """A grading scale with its bands and the verdict the editor shows beside them."""

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    program = serializers.IntegerField(read_only=True)
    program_name = serializers.CharField(read_only=True)
    branch = serializers.IntegerField(read_only=True, allow_null=True)
    scope_label = serializers.CharField(read_only=True)
    is_shared = serializers.BooleanField(read_only=True)
    status = serializers.CharField(read_only=True)
    copied_from = serializers.IntegerField(read_only=True, allow_null=True)
    bands = BandSerializer(many=True, read_only=True)
    check = CheckSerializer(read_only=True)
    locked = serializers.BooleanField(read_only=True)
    lock_reason = serializers.CharField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)


class BandWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """One band in the body of a create or an edit; see :class:`ComponentWriteSerializer` for ``id``."""

    field_resource = SETUP_FIELD_RESOURCE

    id = serializers.IntegerField(required=False)
    min_score = serializers.IntegerField(
        min_value=BAND_MIN_SCORE_MIN, max_value=BAND_MIN_SCORE_MAX,
        error_messages={
            "invalid": "Give the lowest mark as a whole number.",
            "min_value": "The lowest mark cannot be below 0.",
            "max_value": "The lowest mark cannot be above 100.",
        },
    )
    grade = serializers.CharField(max_length=BAND_GRADE_MAX, trim_whitespace=True)
    remark = serializers.CharField(
        max_length=BAND_REMARK_MAX, trim_whitespace=True, allow_blank=True, required=False,
        default="",
    )
    is_fail = serializers.BooleanField(required=False, default=False)


class ScaleWriteSerializer(_SetupWriteBody, FieldAccessMixin, serializers.Serializer):
    """The body of a new scale, or of an edit (any of name, branch, bands).

    A draft may hold an incomplete scale, so the bands are checked as a whole
    only when the scale is put in use.
    """

    field_resource = SETUP_FIELD_RESOURCE
    required_on_create = ("name", "program", "bands")

    name = serializers.CharField(max_length=SETUP_NAME_MAX, trim_whitespace=True, required=False)
    program = serializers.IntegerField(required=False)
    branch = serializers.IntegerField(allow_null=True, required=False)
    bands = BandWriteSerializer(
        many=True, allow_empty=False, max_length=BANDS_MAX, required=False,
    )


# ── Remark templates ───────────────────────────────────────────────────────


class RemarkTemplateSerializer(serializers.Serializer):
    """One principal's remark template; it travels inside :class:`RemarkTemplatesSerializer`."""

    band_min = serializers.IntegerField(read_only=True)
    text = serializers.CharField(read_only=True)


class RemarkTemplatesSerializer(FieldAccessMixin, serializers.Serializer):
    field_resource = SETUP_FIELD_RESOURCE

    templates = RemarkTemplateSerializer(many=True, read_only=True)


class RemarkTemplateWriteSerializer(serializers.Serializer):
    band_min = serializers.IntegerField(
        min_value=0, max_value=100,
        error_messages={
            "invalid": "Give the lowest average as a whole number.",
            "min_value": "The lowest average cannot be below 0.",
            "max_value": "The lowest average cannot be above 100.",
        },
    )
    text = serializers.CharField(max_length=REMARK_TEXT_MAX, trim_whitespace=True)


class RemarkTemplatesWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The whole list of remark templates; it replaces what the school has."""

    field_resource = SETUP_FIELD_RESOURCE

    templates = RemarkTemplateWriteSerializer(
        many=True, allow_empty=False, max_length=REMARK_TEMPLATES_MAX,
    )


# ── Settings ───────────────────────────────────────────────────────────────


class SettingsProgramSerializer(serializers.Serializer):
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    moderated = serializers.BooleanField(read_only=True)


class SettingsSerializer(FieldAccessMixin, serializers.Serializer):
    """The school's gradebook settings, with its programmes beside them.

    ``tell_every_parent`` is a registered field of the setup resource: a role
    that may not read it is sent the settings without it, and
    ``_read_only_fields`` names it for a role that may read but not change it.
    The report card options and moderation have no switch of their own.
    """

    field_resource = SETUP_FIELD_RESOURCE
    field_access_detail = True

    show_class_stats = serializers.BooleanField(read_only=True)
    rank_in_class = serializers.BooleanField(read_only=True)
    conduct_ratings = serializers.BooleanField(read_only=True)
    rating_key = serializers.BooleanField(read_only=True)
    rating_traits = serializers.ListField(child=serializers.CharField(), read_only=True)
    moderated_programs = serializers.ListField(child=serializers.CharField(), read_only=True)
    programs = SettingsProgramSerializer(many=True, read_only=True)
    tell_every_parent = serializers.BooleanField(read_only=True)


class SettingsWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a change to the school's gradebook settings: any of the seven.

    Whether a programme code exists at this school is answered by the service,
    which knows the school's programmes. A role that may not change
    ``tell_every_parent`` is refused a body that sends it.
    """

    field_resource = SETUP_FIELD_RESOURCE

    tell_every_parent = serializers.BooleanField(required=False)
    show_class_stats = serializers.BooleanField(required=False)
    rank_in_class = serializers.BooleanField(required=False)
    conduct_ratings = serializers.BooleanField(required=False)
    rating_key = serializers.BooleanField(required=False)
    rating_traits = serializers.ListField(
        required=False,
        child=serializers.CharField(
            max_length=RATING_TRAIT_NAME_MAX, trim_whitespace=True,
            error_messages={
                "blank": "A trait needs a name.",
                "max_length": f"Keep each trait to {RATING_TRAIT_NAME_MAX} characters.",
            },
        ),
        error_messages={"not_a_list": "Send the traits as a list."},
    )
    moderated_programs = serializers.ListField(
        required=False, allow_empty=True,
        child=serializers.CharField(max_length=20, trim_whitespace=True),
        error_messages={"not_a_list": "Send the programme codes as a list."},
    )

    def validate_rating_traits(self, traits):
        if not RATING_TRAITS_MIN <= len(traits) <= RATING_TRAITS_MAX:
            raise serializers.ValidationError(
                f"Give between {RATING_TRAITS_MIN} and {RATING_TRAITS_MAX} traits.",
            )
        if len({trait.lower() for trait in traits}) != len(traits):
            raise serializers.ValidationError("Each trait must have a different name.")
        return traits


# ── The score sheet ────────────────────────────────────────────────────────


class RefSerializer(serializers.Serializer):
    """A record named beside something else: its id and its name."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)


class _WithClass:
    """Adds the ``class`` entry a payload carries, which cannot be declared as an attribute."""

    def get_fields(self):
        fields = super().get_fields()
        fields["class"] = RefSerializer(read_only=True, allow_null=True)
        return fields


class StudentRefSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    admission_number = serializers.CharField(read_only=True)


class ExamPaperSerializer(serializers.Serializer):
    """The published paper a sheet's subject is sat on: the day and the sitting."""

    date = serializers.DateField(read_only=True)
    sitting = serializers.CharField(read_only=True)
    sitting_label = serializers.CharField(read_only=True)


class SheetCellSerializer(FieldAccessMixin, serializers.Serializer):
    """One cell of a sheet: a mark, or a marker (ABS or EXC), or blank."""

    field_resource = SHEET_FIELD_RESOURCE

    component = serializers.IntegerField(read_only=True)
    mark = serializers.IntegerField(read_only=True, allow_null=True)
    marker = serializers.CharField(read_only=True, allow_null=True)


class SheetRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One student's line on a sheet: their cells and what is worked out from them.

    ``percentage`` is a string to two decimal places. ``excused_note`` is present
    when a component was excused and the row is complete.
    """

    field_resource = SHEET_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    cells = SheetCellSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    obtainable = serializers.IntegerField(read_only=True, allow_null=True)
    percentage = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    grade = serializers.CharField(read_only=True, allow_null=True)
    remark = serializers.CharField(read_only=True, allow_null=True)
    is_fail = serializers.BooleanField(read_only=True, allow_null=True)
    complete = serializers.BooleanField(read_only=True)
    excused_note = serializers.CharField(read_only=True, allow_null=True)


class SheetStatsSerializer(FieldAccessMixin, serializers.Serializer):
    """A subject's figures across the class: the mean percentage, the extreme totals and the pass rate."""

    field_resource = SHEET_FIELD_RESOURCE

    class_average = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    highest = serializers.IntegerField(read_only=True, allow_null=True)
    lowest = serializers.IntegerField(read_only=True, allow_null=True)
    pass_rate = serializers.IntegerField(read_only=True, allow_null=True)


class SheetReturnedSerializer(FieldAccessMixin, serializers.Serializer):
    """Who returned a sheet, when, and the note they gave."""

    field_resource = SHEET_FIELD_RESOURCE

    by = serializers.CharField(read_only=True, allow_null=True)
    at = serializers.DateTimeField(read_only=True)
    note = serializers.CharField(read_only=True, allow_blank=True)


class SheetProgressSerializer(FieldAccessMixin, serializers.Serializer):
    """How many of the sheet's students have a complete row, of how many."""

    field_resource = SHEET_FIELD_RESOURCE

    complete = serializers.IntegerField(read_only=True)
    total = serializers.IntegerField(read_only=True)


class ComponentColumnSerializer(serializers.Serializer):
    """A column of the sheet's grid."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    short_name = serializers.CharField(read_only=True)
    max_score = serializers.IntegerField(read_only=True)
    is_exam = serializers.BooleanField(read_only=True)


class SheetSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """A sheet: its header, the grid's columns, the students' lines and the class figures.

    ``takers`` and ``roll`` are present for an optional subject (the students who
    take it, and the students on the class roll) and null for a core one.
    ``returned`` is null unless the sheet was returned and not yet resubmitted.
    """

    field_resource = SHEET_FIELD_RESOURCE

    subject = RefSerializer(read_only=True)
    term = RefSerializer(read_only=True)
    main_teacher = RefSerializer(read_only=True, allow_null=True)
    assistants = serializers.ListField(child=serializers.CharField(), read_only=True)
    exam_paper = ExamPaperSerializer(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    needs_moderation = serializers.BooleanField(read_only=True)
    is_elective = serializers.BooleanField(read_only=True)
    takers = serializers.IntegerField(read_only=True, allow_null=True)
    roll = serializers.IntegerField(read_only=True, allow_null=True)
    returned = SheetReturnedSerializer(read_only=True, allow_null=True)
    saved_at = serializers.DateTimeField(read_only=True, allow_null=True)
    progress = SheetProgressSerializer(read_only=True)
    can_enter = serializers.BooleanField(read_only=True)
    can_submit = serializers.BooleanField(read_only=True)
    blank_count = serializers.IntegerField(read_only=True)
    components = ComponentColumnSerializer(many=True, read_only=True)
    rows = SheetRowSerializer(many=True, read_only=True)
    stats = SheetStatsSerializer(read_only=True)


class SubmitResultSerializer(serializers.Serializer):
    """The answer to a submit: where the sheet went next, and the sheet as it now stands."""

    next = serializers.CharField(read_only=True)
    sheet = SheetSerializer(read_only=True)


class SheetImportIssueSerializer(serializers.Serializer):
    """A problem found in an uploaded file or one of its rows.

    ``component`` names the column the problem is in, and is absent for a
    problem of the row or of the file as a whole.
    """

    code = serializers.CharField(read_only=True)
    message = serializers.CharField(read_only=True)
    component = serializers.IntegerField(read_only=True)


class SheetImportCellSerializer(FieldAccessMixin, serializers.Serializer):
    """What one uploaded cell would do: the value in the file, the value on the sheet and the verdict.

    ``value`` and ``existing`` are a whole mark, ``ABS`` or ``EXC``, or null for
    an empty cell, and ``value`` is the text as typed when it could not be read.
    ``outcome`` is NEW, SAME, REPLACES or BLANK, and null where the cell has an
    issue. All three state a mark, so they follow the mark switch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    component = serializers.IntegerField(read_only=True)
    value = serializers.JSONField(read_only=True, allow_null=True)
    outcome = serializers.CharField(read_only=True, allow_null=True)
    existing = serializers.JSONField(read_only=True, allow_null=True)


class SheetImportRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One row of an uploaded file: the student it named, its cells and its issues.

    ``row_number`` is the row in the spreadsheet. ``student`` is null when the
    admission number named nobody the caller can see.
    """

    field_resource = SHEET_FIELD_RESOURCE

    row_number = serializers.IntegerField(read_only=True)
    admission_number = serializers.CharField(read_only=True, allow_blank=True)
    student = RefSerializer(read_only=True, allow_null=True)
    cells = SheetImportCellSerializer(many=True, read_only=True)
    issues = SheetImportIssueSerializer(many=True, read_only=True)


class SheetImportSummarySerializer(FieldAccessMixin, serializers.Serializer):
    """How an uploaded file checked out: rows read, rows ready, rows with an issue, scores that would be replaced."""

    field_resource = SHEET_FIELD_RESOURCE

    rows = serializers.IntegerField(read_only=True)
    ready = serializers.IntegerField(read_only=True)
    with_issues = serializers.IntegerField(read_only=True)
    replaces = serializers.IntegerField(read_only=True)


class SheetImportPreviewSerializer(FieldAccessMixin, serializers.Serializer):
    """The checked upload: a token to confirm it with, when it lapses, the summary and every row.

    A file whose headings do not fit the sheet has ``file_issues``, no token
    and no rows.
    """

    field_resource = SHEET_FIELD_RESOURCE

    token = serializers.CharField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    summary = SheetImportSummarySerializer(read_only=True)
    file_issues = SheetImportIssueSerializer(many=True, read_only=True)
    rows = SheetImportRowSerializer(many=True, read_only=True)


class SheetImportFileSerializer(serializers.Serializer):
    """The body of an upload: the file."""

    file = serializers.FileField(allow_empty_file=False)


class SheetImportConfirmSerializer(serializers.Serializer):
    """The body of a confirm: the numbers of the rows to save."""

    rows = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2**31),
        allow_empty=True, max_length=IMPORT_MAX_ROWS,
    )


class CellWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """One cell in the body of a batch save: a mark, or ABS or EXC, or neither (blank).

    ``mark`` is taken as sent, not coerced, because the service decides what a
    whole mark is and says which cell it refused.
    """

    field_resource = SHEET_FIELD_RESOURCE

    student = serializers.IntegerField()
    component = serializers.IntegerField()
    mark = serializers.JSONField(required=False, allow_null=True)
    marker = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class ScoresWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a batch save: the cells to write."""

    field_resource = SHEET_FIELD_RESOURCE

    cells = CellWriteSerializer(many=True, allow_empty=False, max_length=CELLS_MAX)


class ReturnBodySerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a return: the note saying what to correct. Required, which the service enforces."""

    field_resource = SHEET_FIELD_RESOURCE

    note = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=RETURN_NOTE_MAX,
    )


# ── Moderation ─────────────────────────────────────────────────────────────


class ModerationRowSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """One moderated sheet in the queue, with the figures the head decides on.

    ``class_average`` is the mean percentage of the students whose row is graded,
    ``highest`` and ``lowest`` are totals and ``pass_rate`` a whole percentage.
    ``branch`` is null at a school with one branch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    subject = RefSerializer(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)
    main_teacher = RefSerializer(read_only=True, allow_null=True)
    marked = serializers.IntegerField(read_only=True)
    roll = serializers.IntegerField(read_only=True)
    class_average = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    highest = serializers.IntegerField(read_only=True, allow_null=True)
    lowest = serializers.IntegerField(read_only=True, allow_null=True)
    pass_rate = serializers.IntegerField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    compare = serializers.ListField(child=serializers.CharField(), read_only=True)
    can_decide = serializers.BooleanField(read_only=True)


class ModerationQueueSerializer(serializers.Serializer):
    branch_dimension = serializers.BooleanField(read_only=True)
    sheets = ModerationRowSerializer(many=True, read_only=True)


# ── The hubs ───────────────────────────────────────────────────────────────


class HubTermSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    session = serializers.CharField(read_only=True)


class HubKpisSerializer(serializers.Serializer):
    sheets_in = serializers.IntegerField(read_only=True)
    sheets_due = serializers.IntegerField(read_only=True)
    classes_compiled = serializers.IntegerField(read_only=True)
    awaiting_approval = serializers.IntegerField(read_only=True)
    published = serializers.IntegerField(read_only=True)
    held = serializers.IntegerField(read_only=True)


class HubClassSerializer(ClassTeacherReadGate, serializers.Serializer):
    """One class on the results hub. ``branch`` is null at a school with one branch."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)
    program = RefSerializer(read_only=True)
    subjects_in = serializers.IntegerField(read_only=True)
    subjects_due = serializers.IntegerField(read_only=True)
    stage = serializers.CharField(read_only=True)
    returned = serializers.BooleanField(read_only=True)
    class_teacher = RefSerializer(read_only=True, allow_null=True)


class LateSheetSerializer(_WithClass, serializers.Serializer):
    """A due sheet that is not in, with the one reason the platform knows.

    ``staff_link`` is the staff record to open when the reason is that the
    teacher cannot sign in, and null otherwise.
    """

    subject = RefSerializer(read_only=True)
    main_teacher = RefSerializer(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    reason_kind = serializers.CharField(read_only=True)
    reason = serializers.CharField(read_only=True)
    entered = serializers.IntegerField(read_only=True)
    roll = serializers.IntegerField(read_only=True)
    reminded_today = serializers.BooleanField(read_only=True)
    staff_link = serializers.IntegerField(read_only=True, allow_null=True)


class AdminHubSerializer(serializers.Serializer):
    branch_dimension = serializers.BooleanField(read_only=True)
    term = HubTermSerializer(read_only=True)
    results_day = serializers.DateField(read_only=True, allow_null=True)
    term_ends = serializers.DateField(read_only=True)
    kpis = HubKpisSerializer(read_only=True)
    classes = HubClassSerializer(many=True, read_only=True)
    late = LateSheetSerializer(many=True, read_only=True)
    classes_without_branch = RefSerializer(many=True, read_only=True)


class TeacherSheetSerializer(_WithClass, serializers.Serializer):
    """One sheet the caller is the main teacher of."""

    subject = RefSerializer(read_only=True)
    entered = serializers.IntegerField(read_only=True)
    roll = serializers.IntegerField(read_only=True)
    exam_paper = ExamPaperSerializer(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    cta = serializers.CharField(read_only=True)


class DutySerializer(_WithClass, serializers.Serializer):
    """A duty beyond the caller's own sheets.

    A ``CLASS_TEACHER`` duty carries the class, how many of its sheets are in and
    its stage; a ``HEAD_OF_DEPARTMENT`` duty carries the department and how many
    of its moderated sheets are waiting. The fields of the other kind are null.
    """

    kind = serializers.CharField(read_only=True)
    department = RefSerializer(read_only=True, allow_null=True)
    waiting = serializers.IntegerField(read_only=True, allow_null=True)
    subjects_in = serializers.IntegerField(read_only=True, allow_null=True)
    subjects_due = serializers.IntegerField(read_only=True, allow_null=True)
    stage = serializers.CharField(read_only=True, allow_null=True)


class TeacherHubSerializer(serializers.Serializer):
    term = HubTermSerializer(read_only=True)
    sheets = TeacherSheetSerializer(many=True, read_only=True)
    duties = DutySerializer(many=True, read_only=True)


# ── A student's compiled result ────────────────────────────────────────────


class ResultRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One student's compiled result: their average, position, ratings and comments."""

    field_resource = RESULT_FIELD_RESOURCE

    student_id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    overall_average = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True, coerce_to_string=False,
    )
    position = serializers.IntegerField(read_only=True, allow_null=True)
    ratings = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    teacher_comment = serializers.CharField(read_only=True, allow_blank=True)
    principal_comment = serializers.CharField(read_only=True, allow_null=True)


#: Stands for a guardian block every part of which the caller may not read.
_OMIT = object()


def guardian_block(reach, context):
    """A primary guardian as ``{name, email}``, cut down to what the caller may read.

    The guardian is read through the student module's own serializer, so the
    switches on a guardian's name and email that govern a student's record
    govern this block as well: a hidden part is left out alone, never sent
    blank, and a block with no part left is ``_OMIT``. The email is the address
    the school writes to, which is the guardian's account address where they
    have an account. ``None`` when the student has no primary guardian.
    """
    if reach is None:
        return None
    data = GuardianSerializer(reach.guardian, context=context).data
    out = {}
    name = (data.get("full_name") or "").strip()
    if name:
        out["name"] = name
    if "email" in data:
        out["email"] = reach.email or None
    return out or _OMIT


class _GuardianBlock:
    """Adds the ``guardian`` entry of a release row or a Results tab.

    Both Field Access rules apply and the stricter wins: the gradebook's own
    guardian-contact switch removes the entry whole (it is registered under the
    name ``guardian``), and the Student Management guardian switches cut it down
    to the parts they leave readable.
    """

    def get_guardian(self, obj):
        return guardian_block(obj.get("guardian"), self.context)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("guardian") is _OMIT:
            data.pop("guardian")
        return data


class ReleaseRowSerializer(_GuardianBlock, FieldAccessMixin, serializers.Serializer):
    """One student on the release list: their primary guardian and what became of the email.

    ``result_state`` is NOT_PUBLISHED, HELD or RELEASED. ``email_state`` is null
    before publication, then SENT, NO_EMAIL, HELD, or NOT_SENT for a released
    result whose email has no recorded outcome.
    """

    field_resource = RESULT_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    guardian = serializers.SerializerMethodField()
    result_state = serializers.CharField(read_only=True)
    email_state = serializers.CharField(read_only=True, allow_null=True)
    emailed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    released_at = serializers.DateTimeField(read_only=True, allow_null=True)


class ReleaseSerializer(_WithClass, serializers.Serializer):
    """A class's release list for a term."""

    term = RefSerializer(read_only=True)
    stage = serializers.CharField(read_only=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    class_size = serializers.IntegerField(read_only=True)
    held_count = serializers.IntegerField(read_only=True)
    rows = ReleaseRowSerializer(many=True, read_only=True)


# ── The report card ────────────────────────────────────────────────────────


class CardLineSerializer(serializers.Serializer):
    """A printed line: a label and the value worded for print."""

    label = serializers.CharField(read_only=True)
    value = serializers.CharField(read_only=True)


class LetterheadSerializer(serializers.Serializer):
    """The school's present identity at the top of a card."""

    school_name = serializers.CharField(read_only=True)
    logo_url = serializers.CharField(read_only=True, allow_null=True)
    branch_name = serializers.CharField(read_only=True, allow_null=True)
    branch_address = serializers.CharField(read_only=True, allow_null=True)
    phone = serializers.CharField(read_only=True, allow_null=True)


class CardComponentSerializer(serializers.Serializer):
    """A column heading of the card's subject table."""

    short_name = serializers.CharField(read_only=True)
    max_score = serializers.IntegerField(read_only=True)


class _AbsentStaysAbsent:
    """Leaves out an optional key the payload does not carry, rather than sending it as null.

    DRF reads a missing key of a nullable field as null; for a key that exists
    only in some payloads (the class figures of a school that shows none, the
    figures of a term nothing was published for) null would read as a value.
    """

    optional_keys: tuple = ()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for name in self.optional_keys:
            if name not in instance:
                data.pop(name, None)
        return data


class CardSubjectSerializer(_AbsentStaysAbsent, FieldAccessMixin, serializers.Serializer):
    """One subject row of a card.

    ``cells`` are the component scores as printed ("18", "ABS", "Exc") and follow
    the sheet's mark switch; the total, grade and remark follow its figures
    switch. ``class_average`` (one place), ``highest`` and ``lowest`` exist only
    when the school showed class figures at publication, and follow the class
    figures switch.
    """

    field_resource = SHEET_FIELD_RESOURCE
    optional_keys = ("class_average", "highest", "lowest")

    name = serializers.CharField(read_only=True)
    cells = serializers.ListField(child=serializers.CharField(), read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    grade = serializers.CharField(read_only=True, allow_null=True)
    remark = serializers.CharField(read_only=True, allow_null=True)
    class_average = serializers.CharField(read_only=True, allow_null=True, required=False)
    highest = serializers.IntegerField(read_only=True, allow_null=True, required=False)
    lowest = serializers.IntegerField(read_only=True, allow_null=True, required=False)


class CardRatingSerializer(serializers.Serializer):
    trait = serializers.CharField(read_only=True)
    value = serializers.IntegerField(read_only=True)


class CumulativeSubjectSerializer(FieldAccessMixin, serializers.Serializer):
    """One subject of the year block: its total in each term and its annual average.

    ``totals`` has one entry for each of the block's terms in order, an empty
    string where the term has none, and follows the sheet's figures switch (the
    sheet's ``totals`` name). ``annual_average`` is the mean of the term
    percentages and follows the overall average switch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    name = serializers.CharField(read_only=True)
    totals = serializers.ListField(
        child=serializers.CharField(allow_blank=True), read_only=True,
    )
    annual_average = serializers.CharField(read_only=True, allow_null=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_read(self.context.get("request"), OVERALL_AVERAGE_FIELD):
            data.pop("annual_average", None)
        return data


class CumulativeSerializer(FieldAccessMixin, serializers.Serializer):
    """The school year on the last term's card and Results-tab entry.

    ``terms`` are the term names in order, ``subjects`` carry a total for each,
    and ``annual_average`` is the overall figure, which follows the overall
    average switch as the subjects' annual averages do. ``annual_position``
    ("7th of 32") exists only where the school ranked and follows the position
    switch, so a hidden position removes its line. ``promotion_line`` ("Promoted
    to JSS2", "To repeat JSS1", "Graduated") is read live from Student
    Management and exists only when an outcome is recorded.
    """

    field_resource = RESULT_FIELD_RESOURCE

    terms = serializers.ListField(child=serializers.CharField(), read_only=True)
    subjects = CumulativeSubjectSerializer(many=True, read_only=True)
    annual_average = serializers.CharField(read_only=True, allow_null=True)
    annual_position = serializers.CharField(read_only=True, required=False)
    promotion_line = serializers.CharField(read_only=True, required=False)


class ReportCardSerializer(FieldAccessMixin, serializers.Serializer):
    """A student's report card, every figure from the snapshot published with it.

    ``totals`` lines are dropped one by one for a caller who may not read the
    figure they state: the subject totals' sum (obtained and obtainable) follows
    the sheet's figures switch, the overall average its own switch and the
    position its own, so a label never outlives the value it announces.
    ``attendance`` follows the register's counts switch. ``ratings`` and
    ``rating_key`` exist only where the school recorded ratings, and the key only
    when it was on; the key goes with the ratings. A comment and the name that signs it are one switch.
    ``next_term_line`` exists only when the school has a next term, and
    ``held`` is true for a student whose result is withheld from the family.
    ``cumulative`` exists only on the last term of a school year with more than
    one term (:class:`CumulativeSerializer`).
    """

    field_resource = RESULT_FIELD_RESOURCE

    letterhead = LetterheadSerializer(read_only=True)
    term_line = serializers.CharField(read_only=True)
    facts = CardLineSerializer(many=True, read_only=True)
    components = CardComponentSerializer(many=True, read_only=True)
    subjects = CardSubjectSerializer(many=True, read_only=True)
    totals = serializers.SerializerMethodField()
    attendance = CardLineSerializer(many=True, read_only=True)
    ratings = CardRatingSerializer(many=True, read_only=True, required=False)
    rating_key = serializers.CharField(read_only=True, required=False)
    teacher_comment = serializers.CharField(read_only=True, allow_blank=True)
    teacher_name = serializers.CharField(read_only=True, allow_null=True)
    principal_comment = serializers.CharField(read_only=True, allow_null=True)
    principal_name = serializers.CharField(read_only=True, allow_null=True)
    grading_key = serializers.CharField(read_only=True)
    cumulative = CumulativeSerializer(read_only=True, required=False)
    next_term_line = serializers.CharField(read_only=True, required=False)
    version = serializers.IntegerField(read_only=True)
    held = serializers.BooleanField(read_only=True)

    def get_totals(self, obj):
        request = self.context.get("request")
        lines = []
        for line in obj["totals"]:
            governing = line.get("governed_by")
            if governing is not None and not can_read(request, CARD_GOVERNED_BY[governing]):
                continue
            lines.append({"label": line["label"], "value": line["value"]})
        return lines

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if not can_read(request, ATTENDANCE_COUNTS_FIELD):
            data.pop("attendance", None)
        if not can_read(request, RATINGS_FIELD):
            data.pop("rating_key", None)
        return data


class LeftOutSerializer(serializers.Serializer):
    """A student whose card is not in the print, and why (HELD or NOT_RELEASED)."""

    student = StudentRefSerializer(read_only=True)
    reason = serializers.CharField(read_only=True)


class ClassCardsSerializer(_WithClass, serializers.Serializer):
    """Every released student's card in roll order, and who was left out."""

    term = RefSerializer(read_only=True)
    cards = ReportCardSerializer(many=True, read_only=True)
    left_out = LeftOutSerializer(many=True, read_only=True)
    left_out_count = serializers.IntegerField(read_only=True)


# ── A student's Results tab ────────────────────────────────────────────────


class ResultSubjectSerializer(FieldAccessMixin, serializers.Serializer):
    """A subject on a published term of the Results tab: its total, grade and remark.

    ``id`` is the subject's, so a correction to it can be asked for from the tab.
    """

    field_resource = SHEET_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    grade = serializers.CharField(read_only=True, allow_null=True)
    remark = serializers.CharField(read_only=True, allow_null=True)


class ResultTermSerializer(_AbsentStaysAbsent, FieldAccessMixin, serializers.Serializer):
    """One term of the Results tab: published (with its figures), or one calm sentence.

    A published term carries ``overall_average``, ``position`` ("3rd of 24",
    only when that term ranked), ``released_at`` (null while unreleased),
    ``release_label``, ``version`` (1 until a correction is approved),
    ``subjects`` and ``class_id``. The last term of a school year with more than
    one term also carries ``cumulative`` (:class:`CumulativeSerializer`). An
    unpublished one carries ``message`` instead.
    """

    field_resource = RESULT_FIELD_RESOURCE
    optional_keys = ("overall_average", "released_at")

    term = RefSerializer(read_only=True)
    session = RefSerializer(read_only=True)
    published = serializers.BooleanField(read_only=True)
    message = serializers.CharField(read_only=True, required=False)
    overall_average = serializers.CharField(read_only=True, allow_null=True, required=False)
    position = serializers.CharField(read_only=True, required=False)
    released_at = serializers.DateTimeField(read_only=True, allow_null=True, required=False)
    release_label = serializers.CharField(read_only=True, required=False)
    version = serializers.IntegerField(read_only=True, required=False)
    subjects = ResultSubjectSerializer(many=True, read_only=True, required=False)
    cumulative = CumulativeSerializer(read_only=True, required=False)
    class_id = serializers.IntegerField(read_only=True, required=False)


class AmendmentHistorySerializer(FieldAccessMixin, serializers.Serializer):
    """An amendment as one entry of a student's history: what was corrected, by whom and how it was decided.

    ``decided_by`` and ``decided_at`` are null while the amendment waits, and
    ``version`` is the report card version an approval produced, null until
    then. The reason and the decision note follow the amendment switches and are
    absent for a caller who may not read them.
    """

    field_resource = AMENDMENT_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    subject = RefSerializer(read_only=True)
    term = RefSerializer(read_only=True)
    amendment_reason = serializers.CharField(read_only=True, allow_blank=True)
    raised_by = serializers.CharField(read_only=True, allow_null=True)
    raised_at = serializers.DateTimeField(read_only=True)
    decided_by = serializers.CharField(read_only=True, allow_null=True)
    decided_at = serializers.DateTimeField(read_only=True, allow_null=True)
    decision_note = serializers.CharField(read_only=True, allow_blank=True)
    version = serializers.IntegerField(read_only=True, allow_null=True)


class ResultHistorySerializer(FieldAccessMixin, serializers.Serializer):
    """One hold placed or lifted, or one amendment.

    ``text`` names the event ("Result held for First Term"). A placed hold
    carries its ``reason`` and a lifted one its ``lift_note``; both follow the
    hold's reason switch and are absent for a caller who may not read it. An
    ``AMENDMENT`` entry carries its details in ``amendment`` and none of the
    hold's fields; a hold entry has no ``amendment``.
    """

    field_resource = HOLD_FIELD_RESOURCE

    kind = serializers.CharField(read_only=True)
    text = serializers.CharField(read_only=True)
    reason = serializers.CharField(read_only=True, allow_blank=True, required=False)
    lift_note = serializers.CharField(read_only=True, allow_blank=True, required=False)
    amendment = AmendmentHistorySerializer(read_only=True, required=False)
    by = serializers.CharField(read_only=True, allow_null=True)
    at = serializers.DateTimeField(read_only=True)


class ResultStudentSerializer(_WithClass, serializers.Serializer):
    """The student the tab is about: who, their current class and (at a school with several) branch."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    admission_number = serializers.CharField(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)


class StudentResultsSerializer(_GuardianBlock, FieldAccessMixin, serializers.Serializer):
    """A student's Results tab: the header, every term and the history of their holds."""

    field_resource = RESULT_FIELD_RESOURCE

    student = ResultStudentSerializer(read_only=True)
    guardian = serializers.SerializerMethodField()
    terms = ResultTermSerializer(many=True, read_only=True)
    history = ResultHistorySerializer(many=True, read_only=True)


# ── Compiling and approving a class ────────────────────────────────────────


class _WithIn:
    """Adds the ``in`` entry a payload carries, which cannot be declared as an attribute."""

    def get_fields(self):
        fields = super().get_fields()
        fields["in"] = serializers.BooleanField(read_only=True)
        return fields


class BroadsheetSubjectSerializer(_WithIn, serializers.Serializer):
    """A column of the broadsheet: the subject and whether its sheet is in."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    short = serializers.CharField(read_only=True)
    main_teacher = RefSerializer(read_only=True, allow_null=True)


class BroadsheetCellSerializer(FieldAccessMixin, serializers.Serializer):
    """One cell of the broadsheet: a subject's total, or why there is none.

    ``state`` is TOTAL, NOT_TAKEN (an optional subject the student does not
    take) or NOT_IN (the subject's sheet is not in yet). ``total`` is the
    sheet's worked-out total and follows the sheet's figures switch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    subject = serializers.IntegerField(read_only=True)
    state = serializers.CharField(read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)


class BroadsheetRowSerializer(FieldAccessMixin, serializers.Serializer):
    """One student's line of the broadsheet.

    ``position`` and ``position_of`` are absent when the school does not rank.
    ``average`` is a two-place string. ``comment_done`` says whether the class
    teacher's comment is written.
    """

    field_resource = RESULT_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    cells = BroadsheetCellSerializer(many=True, read_only=True)
    average = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    position = serializers.IntegerField(read_only=True, required=False)
    position_of = serializers.IntegerField(read_only=True, required=False)
    comment_done = serializers.BooleanField(read_only=True)


class GapSerializer(serializers.Serializer):
    """A subject whose sheet is not in, with the teacher to chase."""

    subject = RefSerializer(read_only=True)
    main_teacher = RefSerializer(read_only=True, allow_null=True)


class AttendanceWarningSerializer(serializers.Serializer):
    days_not_taken = serializers.IntegerField(read_only=True)


class SubmitBlockersSerializer(serializers.Serializer):
    subjects_missing = RefSerializer(many=True, read_only=True)
    comments_missing = serializers.IntegerField(read_only=True)


class BroadsheetSerializer(ClassTeacherReadGate, _WithClass, serializers.Serializer):
    """The broadsheet of a class for a term.

    ``attendance_warning`` is null when no school day is without a register or
    the class is published, and is left out for a caller who may not read the register's status.
    """

    term = RefSerializer(read_only=True)
    class_teacher = RefSerializer(read_only=True, allow_null=True)
    stage = serializers.CharField(read_only=True)
    returned = SheetReturnedSerializer(read_only=True, allow_null=True)
    subjects = BroadsheetSubjectSerializer(many=True, read_only=True)
    rows = BroadsheetRowSerializer(many=True, read_only=True)
    gaps = GapSerializer(many=True, read_only=True)
    attendance_warning = AttendanceWarningSerializer(read_only=True, allow_null=True)
    can_submit = serializers.BooleanField(read_only=True)
    submit_blockers = SubmitBlockersSerializer(read_only=True)
    ranks = serializers.BooleanField(read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_read(self.context.get("request"), ATTENDANCE_STATUS_FIELD):
            data.pop("attendance_warning", None)
        return data


class CompileSubjectSerializer(FieldAccessMixin, serializers.Serializer):
    """One subject on a student's compile drawer: its total and grade, or "not in yet"."""

    field_resource = SHEET_FIELD_RESOURCE

    subject = RefSerializer(read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    grade = serializers.CharField(read_only=True, allow_null=True)
    state = serializers.CharField(read_only=True)


class AttendanceCountsSerializer(serializers.Serializer):
    opened = serializers.IntegerField(read_only=True)
    present = serializers.IntegerField(read_only=True)
    absent = serializers.IntegerField(read_only=True)


class CompileSerializer(FieldAccessMixin, serializers.Serializer):
    """A student's compile drawer: their term so far, and the class teacher's part to write.

    ``ratings`` and ``traits`` are absent when the school does not record conduct
    ratings. ``attendance`` is left out for a caller who may not read the
    register's counts. ``previous`` and ``next`` are the neighbours on the roll.
    """

    field_resource = RESULT_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    index = serializers.IntegerField(read_only=True)
    of = serializers.IntegerField(read_only=True)
    previous = serializers.IntegerField(read_only=True, allow_null=True)
    next = serializers.IntegerField(read_only=True, allow_null=True)
    subjects = CompileSubjectSerializer(many=True, read_only=True)
    average = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    attendance = AttendanceCountsSerializer(read_only=True)
    ratings = serializers.DictField(
        child=serializers.IntegerField(), read_only=True, required=False,
    )
    traits = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    teacher_comment = serializers.CharField(read_only=True, allow_blank=True)
    stage = serializers.CharField(read_only=True)
    can_edit = serializers.BooleanField(read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_read(self.context.get("request"), ATTENDANCE_COUNTS_FIELD):
            data.pop("attendance", None)
        return data


class CompileWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a compile save: the comment, required, and the ratings when they are sent.

    The comment is judged by the service so that a missing and a blank one are
    both ``COMMENT_REQUIRED``. Ratings are judged against the school's traits.
    """

    field_resource = RESULT_FIELD_RESOURCE

    teacher_comment = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=TEACHER_COMMENT_MAX,
    )
    ratings = serializers.JSONField(required=False, allow_null=True)


class ClassReturnBodySerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a class return: the note, required, and the subject when only one sheet goes back."""

    field_resource = SHEET_FIELD_RESOURCE

    note = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=RETURN_NOTE_MAX,
    )
    subject = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class HeldStudentSerializer(serializers.Serializer):
    """A student whose result is held, and who held it. The reason stays on the holds screen."""

    student = RefSerializer(read_only=True)
    held_by = serializers.CharField(read_only=True, allow_null=True)
    placed_at = serializers.DateTimeField(read_only=True)


class ApprovalRowSerializer(ClassTeacherReadGate, _WithClass, serializers.Serializer):
    branch = RefSerializer(read_only=True, allow_null=True)
    class_teacher = RefSerializer(read_only=True, allow_null=True)
    subjects_in = serializers.IntegerField(read_only=True)
    subjects_due = serializers.IntegerField(read_only=True)
    stage = serializers.CharField(read_only=True)
    submitted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    held = HeldStudentSerializer(many=True, read_only=True)
    can_decide = serializers.BooleanField(read_only=True)


class ApprovalsSerializer(serializers.Serializer):
    branch_dimension = serializers.BooleanField(read_only=True)
    classes = ApprovalRowSerializer(many=True, read_only=True)


class PrincipalOverrideSerializer(serializers.Serializer):
    """The principal's own comment for a band of a class, with who wrote it and when."""

    text = serializers.CharField(read_only=True)
    written_by = serializers.CharField(read_only=True, allow_null=True)
    written_at = serializers.DateTimeField(read_only=True)


class PrincipalBandSerializer(FieldAccessMixin, serializers.Serializer):
    """One band of the school's remark templates with the class's own comment, if one is written.

    ``template`` is the school's sentence and follows the remark-template switch
    as well as this screen's; ``override`` is the class's comment and follows the
    principal's comment switch.
    """

    field_resource = RESULT_FIELD_RESOURCE

    band_min = serializers.IntegerField(read_only=True)
    template = serializers.CharField(read_only=True)
    override = PrincipalOverrideSerializer(read_only=True, allow_null=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_read(self.context.get("request"), REMARK_TEMPLATES_FIELD):
            data.pop("template", None)
        return data


class PrincipalCommentsSerializer(_WithClass, serializers.Serializer):
    term = RefSerializer(read_only=True)
    locked = serializers.BooleanField(read_only=True)
    bands = PrincipalBandSerializer(many=True, read_only=True)


class PrincipalCommentWriteSerializer(serializers.Serializer):
    band_min = serializers.IntegerField(
        min_value=0, max_value=100,
        error_messages={"invalid": "Give the band as a whole number."},
    )
    text = serializers.CharField(
        allow_blank=True, allow_null=True, max_length=PRINCIPAL_COMMENT_MAX,
    )


class PrincipalCommentsWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a save of the principal's comments: one text for each band named."""

    field_resource = RESULT_FIELD_RESOURCE

    comments = PrincipalCommentWriteSerializer(many=True, allow_empty=False, max_length=20)

    def validate_comments(self, rows):
        bands = [row["band_min"] for row in rows]
        if len(set(bands)) != len(bands):
            raise serializers.ValidationError("Send each band once.")
        return rows


class PublishPreviewSerializer(_WithClass, serializers.Serializer):
    released = serializers.IntegerField(read_only=True)
    emailed = serializers.IntegerField(read_only=True)
    no_email = serializers.IntegerField(read_only=True)
    held = HeldStudentSerializer(many=True, read_only=True)


class PublishBodySerializer(serializers.Serializer):
    classes = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2**62),
        allow_empty=False, max_length=PUBLISH_CLASSES_MAX,
    )


class PublishResultSerializer(_WithClass, serializers.Serializer):
    """One class's answer to a publish: published, or the reason it was not.

    ``code`` and ``message`` are present only when the class was refused.
    ``held`` is how many results stayed back.
    """

    published = serializers.BooleanField(read_only=True)
    code = serializers.CharField(read_only=True, required=False)
    message = serializers.CharField(read_only=True, required=False)
    released = serializers.IntegerField(read_only=True)
    emailed = serializers.IntegerField(read_only=True)
    no_email = serializers.IntegerField(read_only=True)
    held = serializers.IntegerField(read_only=True)


# ── Fee holds ──────────────────────────────────────────────────────────────


class HoldRecordSerializer(FieldAccessMixin, serializers.Serializer):
    """A hold: why, who placed it and when. The reason follows the hold switch."""

    field_resource = HOLD_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    reason = serializers.CharField(read_only=True)
    placed_by = serializers.CharField(read_only=True, allow_null=True)
    placed_at = serializers.DateTimeField(read_only=True)


class HoldLiftedSerializer(HoldRecordSerializer):
    """A hold after it was lifted: who lifted it, when, and the note."""

    lifted_by = serializers.CharField(read_only=True, allow_null=True)
    lifted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    lift_note = serializers.CharField(read_only=True, allow_blank=True)


class HoldRowSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """One student on the holds list: their class, what Finance says they owe, and their hold.

    It carries no score, grade or average. ``balance`` is kobo and, with
    ``oldest_unpaid_on`` and ``days_owed``, is null when Finance could not
    answer, never zero. ``hold`` is null when the result is not held.
    """

    field_resource = HOLD_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)
    balance = serializers.IntegerField(read_only=True, allow_null=True)
    oldest_unpaid_on = serializers.DateField(read_only=True, allow_null=True)
    days_owed = serializers.IntegerField(read_only=True, allow_null=True)
    hold = HoldRecordSerializer(read_only=True, allow_null=True)
    released = serializers.BooleanField(read_only=True)
    released_on = serializers.DateField(read_only=True, allow_null=True)


class HoldPlaceBodySerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a hold: the student, the term and the reason, which the service requires."""

    field_resource = HOLD_FIELD_RESOURCE

    student = serializers.IntegerField(min_value=1, max_value=2**62)
    term = serializers.IntegerField(min_value=1, max_value=2**62)
    reason = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=HOLD_REASON_MAX,
    )


class HoldLiftBodySerializer(FieldAccessMixin, serializers.Serializer):
    """The body of a lift: an optional note.

    The note is sent as ``note`` and held as ``lift_note``, the name the Field
    Access registry knows it by, so the body is read under that name.
    """

    field_resource = HOLD_FIELD_RESOURCE

    lift_note = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="", max_length=HOLD_NOTE_MAX,
    )

    def to_internal_value(self, data):
        if isinstance(data, Mapping) and "note" in data and "lift_note" not in data:
            data = {**data, "lift_note": data["note"]}
        return super().to_internal_value(data)


class HoldLiftResultSerializer(serializers.Serializer):
    """The answer to a lift: the hold as it now stands, whether the result was released, the student's row."""

    hold = HoldLiftedSerializer(read_only=True)
    released = serializers.BooleanField(read_only=True)
    row = HoldRowSerializer(read_only=True)


# ── Amendments ─────────────────────────────────────────────────────────────


class PendingAmendmentSerializer(serializers.Serializer):
    """The correction already waiting for a student's subject, so the drawer can say so."""

    id = serializers.IntegerField(read_only=True)
    raised_at = serializers.DateTimeField(read_only=True)


class AmendmentLineSerializer(FieldAccessMixin, serializers.Serializer):
    """A subject's cells and what is worked out from them, before or after a correction.

    The cells follow the mark switch and the figures the row-result switch, as
    on the sheet, and the grade and remark are those of the grading key the
    result was published with.
    """

    field_resource = SHEET_FIELD_RESOURCE

    cells = SheetCellSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    obtainable = serializers.IntegerField(read_only=True, allow_null=True)
    percentage = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    grade = serializers.CharField(read_only=True, allow_null=True)
    remark = serializers.CharField(read_only=True, allow_null=True)
    is_fail = serializers.BooleanField(read_only=True, allow_null=True)


class AmendmentFormSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """What the amendment drawer shows: the columns and the subject's marks as published.

    The marks follow the mark switch and the worked-out figures the row-result
    switch, as on the sheet. ``pending`` is the correction already waiting for
    this student's subject, or null; while one waits another cannot be raised.
    """

    field_resource = SHEET_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    subject = RefSerializer(read_only=True)
    term = RefSerializer(read_only=True)
    version = serializers.IntegerField(read_only=True)
    components = ComponentColumnSerializer(many=True, read_only=True)
    cells = SheetCellSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True, allow_null=True)
    obtainable = serializers.IntegerField(read_only=True, allow_null=True)
    percentage = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True, allow_null=True,
    )
    grade = serializers.CharField(read_only=True, allow_null=True)
    remark = serializers.CharField(read_only=True, allow_null=True)
    is_fail = serializers.BooleanField(read_only=True, allow_null=True)
    pending = PendingAmendmentSerializer(read_only=True, allow_null=True)


class AmendmentRowSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """A correction to a published result: who, what, why, and how it was decided.

    ``before`` and ``after`` are the subject's cells with the line worked out
    from them. ``version`` is the report card version an approval produced and is
    null until then. ``branch`` is null at a school with one branch. The reason
    and the decision note follow the amendment switches. ``can_decide`` is true
    for a correction still waiting that the caller did not raise.
    """

    field_resource = AMENDMENT_FIELD_RESOURCE

    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    student = StudentRefSerializer(read_only=True)
    term = RefSerializer(read_only=True)
    subject = RefSerializer(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)
    amendment_reason = serializers.CharField(read_only=True, allow_blank=True)
    raised_by = serializers.CharField(read_only=True, allow_null=True)
    raised_at = serializers.DateTimeField(read_only=True)
    decided_by = serializers.CharField(read_only=True, allow_null=True)
    decided_at = serializers.DateTimeField(read_only=True, allow_null=True)
    decision_note = serializers.CharField(read_only=True, allow_blank=True)
    version = serializers.IntegerField(read_only=True, allow_null=True)
    before = AmendmentLineSerializer(read_only=True)
    after = AmendmentLineSerializer(read_only=True)
    can_decide = serializers.BooleanField(read_only=True)


class AmendmentCellWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """One corrected cell: a column, and a mark or ABS or EXC. The student is the amendment's own.

    ``mark`` is taken as sent, not coerced, because the service decides what a
    whole mark is and says which cell it refused.
    """

    field_resource = SHEET_FIELD_RESOURCE

    component = serializers.IntegerField()
    mark = serializers.JSONField(required=False, allow_null=True)
    marker = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class AmendmentCellsBodySerializer(FieldAccessMixin, serializers.Serializer):
    """The corrected cells of a request to amend: the columns being changed.

    Read apart from the rest of the body because the cells are the sheet's
    marks and are governed by the mark switch, while the reason is the
    amendment's.
    """

    field_resource = SHEET_FIELD_RESOURCE

    cells = AmendmentCellWriteSerializer(many=True, allow_empty=False, max_length=COMPONENTS_MAX)


class _WithClassId:
    """Adds the ``class`` entry a request body carries, which cannot be declared as an attribute."""

    def get_fields(self):
        fields = super().get_fields()
        fields["class"] = serializers.IntegerField(min_value=1, max_value=2**62)
        return fields


class _Aliased:
    """Accepts a body key under the name the screens send it by, for the registered field it is.

    The reason is sent as ``reason`` and the decision note as ``note``, and are
    held as ``amendment_reason`` and ``decision_note``, the names the Field
    Access registry knows them by, so the body is read under the registered
    name. A body that carries both names keeps the registered one.
    """

    aliases: dict = {}

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            for sent, registered in self.aliases.items():
                if sent in data and registered not in data:
                    data = {**data, registered: data[sent]}
        return super().to_internal_value(data)


class AmendmentRaiseBodySerializer(_Aliased, _WithClassId, FieldAccessMixin, serializers.Serializer):
    """The identifying part of a request to amend: the term, class, student, subject and reason.

    The reason is required, which the service enforces so a missing and a blank
    one are the same refusal. The corrected cells travel in the same body and are
    read by :class:`AmendmentCellsBodySerializer`.
    """

    field_resource = AMENDMENT_FIELD_RESOURCE
    aliases = {"reason": "amendment_reason"}

    term = serializers.IntegerField(min_value=1, max_value=2**62)
    student = serializers.IntegerField(min_value=1, max_value=2**62)
    subject = serializers.IntegerField(min_value=1, max_value=2**62)
    amendment_reason = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=AMENDMENT_REASON_MAX,
    )


class AmendmentDecisionBodySerializer(_Aliased, FieldAccessMixin, serializers.Serializer):
    """The body of an approval or a rejection: the note, required to reject and optional to approve."""

    field_resource = AMENDMENT_FIELD_RESOURCE
    aliases = {"note": "decision_note"}

    decision_note = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default="",
        max_length=DECISION_NOTE_MAX,
    )


# ── Performance analytics ──────────────────────────────────────────────────


class AnalyticsSpreadSerializer(serializers.Serializer):
    """The number of students on one grade of a class's scale."""

    grade = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class AnalyticsSubjectSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """One subject in one published class: its average, grade spread, pass rate and how many were marked.

    The average, the spread and the pass rate are the class figures of the sheet
    and follow its class-figures switch (``average``, ``spread`` and ``pass_rate``
    are names of that switch). ``marked`` counts students and has no switch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    subject = RefSerializer(read_only=True)
    average = serializers.CharField(read_only=True, allow_null=True)
    spread = AnalyticsSpreadSerializer(many=True, read_only=True)
    pass_rate = serializers.IntegerField(read_only=True, allow_null=True)
    marked = serializers.IntegerField(read_only=True)


class AnalyticsArmSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """One class that offers a compared subject, with its average (the class-figures switch)."""

    field_resource = SHEET_FIELD_RESOURCE

    average = serializers.CharField(read_only=True)


class AnalyticsCompareSerializer(FieldAccessMixin, serializers.Serializer):
    """A subject offered by two or more published classes, and the gap between highest and lowest average.

    The gap is a difference of class averages and follows the class-figures switch.
    """

    field_resource = SHEET_FIELD_RESOURCE

    subject = RefSerializer(read_only=True)
    arms = AnalyticsArmSerializer(many=True, read_only=True)
    gap = serializers.CharField(read_only=True)


class _WithFromTo:
    """Adds the ``from`` and ``to`` entries a mover carries, which cannot be declared as attributes."""

    def get_fields(self):
        fields = super().get_fields()
        fields["from"] = serializers.CharField(read_only=True)
        fields["to"] = serializers.CharField(read_only=True)
        return fields


class AnalyticsMoverSerializer(_WithClass, _WithFromTo, FieldAccessMixin, serializers.Serializer):
    """A student's overall average this term against their last earlier published term.

    ``from``, ``to`` and the signed ``change`` are overall averages and follow
    the overall average switch; the student, the class and the term compared
    with are names and stay.
    """

    field_resource = RESULT_FIELD_RESOURCE

    student = StudentRefSerializer(read_only=True)
    from_term = RefSerializer(read_only=True)
    change = serializers.CharField(read_only=True)


class AnalyticsTopSerializer(_WithClass, FieldAccessMixin, serializers.Serializer):
    """The highest overall average of a class and every student on it."""

    field_resource = RESULT_FIELD_RESOURCE

    students = RefSerializer(many=True, read_only=True)
    average = serializers.CharField(read_only=True)


class AnalyticsScopeClassSerializer(serializers.Serializer):
    """A published class in scope; ``branch`` is null at a school with one branch."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    branch = RefSerializer(read_only=True, allow_null=True)


class AnalyticsScopeSerializer(serializers.Serializer):
    branch_dimension = serializers.BooleanField(read_only=True)
    classes = AnalyticsScopeClassSerializer(many=True, read_only=True)
    published_count = serializers.IntegerField(read_only=True)


class AnalyticsSerializer(FieldAccessMixin, serializers.Serializer):
    """Performance analytics for a term, from published results only.

    ``tops`` states who leads each class by overall average and by position, so
    it is sent only to a caller who may read **both** switches: the stricter
    wins, and a role that may not read positions or averages is sent no
    ``tops`` at all. A role that may not read overall averages is also sent the
    movers by name, because their order is an order of averages.
    """

    field_resource = RESULT_FIELD_RESOURCE

    term = HubTermSerializer(read_only=True)
    scope = AnalyticsScopeSerializer(read_only=True)
    ranks = serializers.BooleanField(read_only=True)
    subjects = AnalyticsSubjectSerializer(many=True, read_only=True)
    compare = AnalyticsCompareSerializer(many=True, read_only=True)
    movers = AnalyticsMoverSerializer(many=True, read_only=True)
    tops = AnalyticsTopSerializer(many=True, read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if not can_read(request, OVERALL_AVERAGE_FIELD) or not can_read(request, POSITION_FIELD):
            data.pop("tops", None)
        if not can_read(request, OVERALL_AVERAGE_FIELD) and "movers" in data:
            data["movers"] = sorted(
                data["movers"],
                key=lambda row: (row["student"]["name"].casefold(), row["student"]["id"]),
            )
        return data
