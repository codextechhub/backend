"""Payloads for the academic structure screens.

Two rules run through all of them.

**The branch dimension recedes at one branch.** A school with a single branch
gets responses identical to the ones these calls would have returned before the
column existed: no branch field, no scope label, nothing greyed out. A control
with one option is noise, and the rows are unaffected either way.

**Nothing here exposes anything section 6 of the FRD does not name.** No user
email, no permission keys, no raw metadata.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SessionBranch,
    SessionStatus,
)
from .services.scoping import branch_dimension_applies


class _BranchAware(serializers.ModelSerializer):
    """Drops every branch-shaped field when the school has one branch."""

    def _multi_branch(self):
        # Resolved once per request by the view and passed in context, because
        # asking per row would be a query per row on every list.
        return self.context.get("multi_branch", True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._multi_branch():
            for field in ("branch", "branch_name", "scope_label", "branches"):
                data.pop(field, None)
        return data


class TermSerializer(serializers.ModelSerializer):
    is_archived = serializers.SerializerMethodField()

    class Meta:
        model = AcademicTerm
        fields = [
            "id", "name", "order_index", "start_date", "end_date",
            "is_archived",
        ]

    def get_is_archived(self, obj) -> bool:
        return obj.archived_at is not None


class TermWriteSerializer(serializers.Serializer):
    """A term inside a session create or edit.

    Deliberately not a ModelSerializer: these arrive nested in the session
    body, are validated as a set rather than one at a time (three of the four
    rules are about siblings), and are never addressed individually here.
    """

    id = serializers.IntegerField(required=False)
    name = serializers.CharField(max_length=30)
    order_index = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField()
    end_date = serializers.DateField()


class SessionSerializer(_BranchAware):
    """One school year, as the list and the detail screen read it."""

    terms = TermSerializer(many=True, read_only=True)
    term_count = serializers.SerializerMethodField()
    branches = serializers.SerializerMethodField()
    scope_label = serializers.SerializerMethodField()

    class Meta:
        model = AcademicSession
        fields = [
            "id", "name", "start_date", "end_date", "status",
            "activated_at", "archived_at", "is_school_wide",
            "terms", "term_count", "branches", "scope_label",
        ]
        read_only_fields = ["status", "activated_at", "archived_at", "is_school_wide"]

    def get_term_count(self, obj) -> int:
        # Annotated by the view; the fallback is for a single-object read.
        count = getattr(obj, "term_count_annotated", None)
        return count if count is not None else obj.terms.count()

    def get_branches(self, obj):
        return [
            {"id": link.branch_id, "name": link.branch.name}
            for link in obj.branch_links.select_related("branch").all()
        ]

    def get_scope_label(self, obj) -> str:
        """What the session list and the detail chips print.

        "The whole school" is a statement, not a fallback: a session naming no
        branches covers every branch the school has now, including ones opened
        after it was written.
        """
        names = [link.branch.name for link in obj.branch_links.select_related("branch").all()]
        return ", ".join(sorted(names)) if names else "The whole school"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._multi_branch():
            data.pop("is_school_wide", None)
        return data


class SessionWriteSerializer(serializers.ModelSerializer):
    """Create or edit a year, with its terms and its branch set in one call.

    One request because the drawer has one Save button: a session created by
    one call and its terms by three leaves a half-built year on the school's
    screen whenever the second call fails.
    """

    terms = TermWriteSerializer(many=True, required=False)
    branch_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="Omit or send an empty list for a year the whole school runs.",
    )

    class Meta:
        model = AcademicSession
        fields = ["id", "name", "start_date", "end_date", "terms", "branch_ids"]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end <= start:
            raise serializers.ValidationError({
                "end_date": "The session must end after it starts.",
            })
        return attrs


# ── Structure: departments, programmes, levels ─────────────────────────────

class _ScopedSerializer(_BranchAware):
    """A catalogue row that may belong to one branch or to the whole school."""

    branch_name = serializers.SerializerMethodField()
    scope_label = serializers.SerializerMethodField()

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else None

    def get_scope_label(self, obj) -> str:
        return obj.branch.name if obj.branch_id else "School-wide"


class DepartmentSerializer(_ScopedSerializer):
    program_count = serializers.SerializerMethodField()
    subject_count = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "description", "is_active",
            "branch", "branch_name", "scope_label",
            "program_count", "subject_count",
        ]

    def get_program_count(self, obj) -> int:
        return getattr(obj, "program_count_annotated", 0)

    def get_subject_count(self, obj) -> int:
        return getattr(obj, "subject_count_annotated", 0)


class DepartmentWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "code", "description", "branch", "is_active"]
        extra_kwargs = {
            # Generated from the name when omitted, so the drawer's Generate
            # button and an API caller who leaves it out behave alike.
            "code": {"required": False, "allow_blank": True},
            "branch": {"required": False, "allow_null": True},
        }


class LevelSerializer(_ScopedSerializer):
    class_count = serializers.SerializerMethodField()
    program_name = serializers.CharField(source="program.name", read_only=True)

    class Meta:
        model = Level
        fields = [
            "id", "name", "code", "order_index", "is_active",
            "program", "program_name", "next_level",
            "branch", "branch_name", "scope_label", "class_count",
        ]

    def get_class_count(self, obj) -> int:
        return getattr(obj, "class_count_annotated", 0)


class LevelWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Level
        fields = [
            "id", "name", "code", "order_index", "branch", "next_level",
            "is_active",
        ]
        extra_kwargs = {
            "code": {"required": False, "allow_blank": True},
            "order_index": {"required": False},
            "branch": {"required": False, "allow_null": True},
            "next_level": {"required": False, "allow_null": True},
        }


class ProgramSerializer(_ScopedSerializer):
    """A programme with its levels nested.

    Nested rather than a separate call because the screen is an accordion: a
    flat list would mean one request per programme to draw one page.
    """

    levels = serializers.SerializerMethodField()
    level_count = serializers.SerializerMethodField()
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None,
    )

    class Meta:
        model = Program
        fields = [
            "id", "name", "code", "order_index", "is_active",
            "department", "department_name",
            "branch", "branch_name", "scope_label",
            "levels", "level_count",
        ]

    def _levels(self, obj):
        return getattr(obj, "visible_levels", None) or list(obj.levels.all())

    def get_levels(self, obj):
        return LevelSerializer(self._levels(obj), many=True, context=self.context).data

    def get_level_count(self, obj) -> int:
        return len(self._levels(obj))


class ProgramWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Program
        fields = [
            "id", "name", "code", "order_index", "department", "branch",
            "is_active",
        ]
        extra_kwargs = {
            "code": {"required": False, "allow_blank": True},
            "order_index": {"required": False},
            "department": {"required": False, "allow_null": True},
            "branch": {"required": False, "allow_null": True},
        }


class BulkLevelSerializer(serializers.Serializer):
    """A run of levels typed one per line."""

    names = serializers.ListField(
        child=serializers.CharField(max_length=60), allow_empty=False,
    )
    branch = serializers.IntegerField(required=False, allow_null=True)
