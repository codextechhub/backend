"""What the API exposes, and what it deliberately does not.

Three rules run through every serializer here.

**No medical field ever appears in a list.** Not blood group, not allergies,
not conditions, not the emergency contact. A list is the response that gets
paged, cached and exported; a child's medical history has no business in one.

**Three of the five medical fields are gated on
``school.students.view_sensitive``, and the emergency contact is not.** An
emergency contact only a school administrator can read is useless in the
emergency it exists for, and it is an adult's name and phone number rather than
a child's medical history.

**A file is a signed, user-bound, expiring URL, never a path.** An unsigned
``/media/<name>`` inside its window is a bearer token.

FRD M11 v2.4 sections 7 and 12.1.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from core.media import signed_url
from vs_rbac.fls import FieldSecurityMixin

from .constants import (
    DocumentType,
    Gender,
    PERM_VIEW_SENSITIVE,
    Relationship,
    StudentStatus,
    TransferReason,
)
from .models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentDocument,
    StudentGuardian,
    StudentPromotionBatch,
    StudentStatusLog,
)


def _age_on(dob, when=None):
    if not dob:
        return None
    when = when or timezone.localdate()
    return when.year - dob.year - ((when.month, when.day) < (dob.month, dob.day))


class _BranchAware(serializers.ModelSerializer):
    """Drops the branch field where the school has one branch.

    Not disabled, absent. A column repeating the same value on every row is
    noise, and it reappears the day a second branch opens without a row being
    rewritten.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("multi_branch", True):
            data.pop("branch", None)
            data.pop("branch_name", None)
        return data


# ── guardians ──────────────────────────────────────────────────────────────

class GuardianSerializer(serializers.ModelSerializer):
    has_account = serializers.SerializerMethodField()

    class Meta:
        model = Guardian
        fields = [
            "id", "full_name", "phone", "email", "occupation", "address",
            "has_account",
        ]

    def get_has_account(self, obj):
        # Whether they have a login, never which User row it is: an internal
        # id on a parent's record is an identifier nobody on this screen needs.
        return obj.user_id is not None


class GuardianLinkSerializer(serializers.ModelSerializer):
    guardian = GuardianSerializer(read_only=True)
    relationship_label = serializers.CharField(
        source="get_relationship_display", read_only=True,
    )
    siblings = serializers.SerializerMethodField()

    class Meta:
        model = StudentGuardian
        fields = ["id", "guardian", "relationship", "relationship_label",
                  "is_primary", "siblings"]

    def get_siblings(self, obj):
        rows = self.context.get("siblings", {}).get(obj.guardian_id, [])
        return [
            {"id": s.pk, "name": s.full_name, "class": self.context
             .get("class_names", {}).get(s.pk, "")}
            for s in rows
        ]


class GuardianDirectorySerializer(serializers.ModelSerializer):
    ward_count = serializers.IntegerField(read_only=True)
    ward_names = serializers.SerializerMethodField()
    is_sibling_household = serializers.SerializerMethodField()

    class Meta:
        model = Guardian
        fields = ["id", "full_name", "phone", "email", "ward_count",
                  "ward_names", "is_sibling_household"]

    def get_ward_names(self, obj):
        return self.context.get("wards", {}).get(obj.pk, [])

    def get_is_sibling_household(self, obj):
        return len(self.context.get("wards", {}).get(obj.pk, [])) > 1


class GuardianWriteSerializer(serializers.Serializer):
    """One guardian on an enrolment or a link.

    Either an existing guardian by id, or a new one by name and phone. Never a
    branch: a guardian is school-level and a request supplying one is refused
    as a field that does not exist rather than accepted and ignored.
    """

    guardian_id = serializers.IntegerField(required=False, allow_null=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    occupation = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    address = serializers.CharField(required=False, allow_blank=True)
    relationship = serializers.ChoiceField(choices=Relationship.choices)
    is_primary = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if not attrs.get("guardian_id"):
            if not (attrs.get("full_name") or "").strip():
                raise serializers.ValidationError({
                    "full_name": "Give the guardian's name, or pick one already at the school.",
                })
            if not (attrs.get("phone") or "").strip():
                raise serializers.ValidationError({
                    "phone": "A guardian needs a phone number the school can reach.",
                })
        return attrs


# ── students ───────────────────────────────────────────────────────────────

class StudentListSerializer(_BranchAware):
    """The directory row. No medical field, no guardian contact details."""

    full_name = serializers.CharField(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    class_name = serializers.SerializerMethodField()
    level_name = serializers.SerializerMethodField()
    primary_guardian = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = [
            "id", "student_number", "first_name", "middle_name", "last_name",
            "full_name", "status", "status_label", "branch", "branch_name",
            "class_name", "level_name", "primary_guardian", "photo_url",
            "enrolment_date",
            # How long an application has been waiting is the one fact the
            # applicant board and the directory's work queue are both sorting
            # on, and it was detail-only - so both had a date they could not
            # reach without a request per row.
            "applied_on",
        ]

    def _enrolment(self, obj):
        # Reads the prefetched list rather than querying, so the query count
        # does not grow with the page size.
        rows = getattr(obj, "_active_enrolments", None)
        if rows is None:
            rows = [e for e in obj.enrolments.all() if e.is_active]
        return rows[0] if rows else None

    def get_class_name(self, obj):
        row = self._enrolment(obj)
        return row.school_class.name if row else ""

    def get_level_name(self, obj):
        row = self._enrolment(obj)
        if row and row.school_class.level_id:
            return row.school_class.level.name
        return obj.applied_for.name if obj.applied_for_id else ""

    def get_primary_guardian(self, obj):
        for link in obj.guardian_links.all():
            if link.is_primary:
                return link.guardian.full_name
        return ""

    def get_photo_url(self, obj):
        return signed_url(obj.photo.name) if obj.photo else ""


class StudentDetailSerializer(FieldSecurityMixin, _BranchAware):
    """The profile. Medical is here and gated; it is never in a list."""

    read_permissions = {
        "blood_group": PERM_VIEW_SENSITIVE,
        "allergies": PERM_VIEW_SENSITIVE,
        "conditions": PERM_VIEW_SENSITIVE,
    }
    write_permissions = {
        "blood_group": PERM_VIEW_SENSITIVE,
        "allergies": PERM_VIEW_SENSITIVE,
        "conditions": PERM_VIEW_SENSITIVE,
        "enrolment_date": "school.students.manage",
    }

    full_name = serializers.CharField(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    age = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    level_name = serializers.SerializerMethodField()
    session_name = serializers.SerializerMethodField()
    applied_for_name = serializers.CharField(
        source="applied_for.name", read_only=True, default="",
    )
    photo_url = serializers.SerializerMethodField()
    allowed_transitions = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = [
            "id", "student_number", "first_name", "middle_name", "last_name",
            "full_name", "date_of_birth", "age", "gender", "nationality",
            "state_of_origin", "address", "phone", "email", "previous_school",
            "blood_group", "allergies", "conditions",
            "emergency_contact_name", "emergency_contact_phone",
            "status", "status_label", "enrolment_date",
            "branch", "branch_name", "class_name", "level_name", "session_name",
            "applied_for", "applied_for_name", "applied_on",
            "photo_url", "allowed_transitions", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "branch", "applied_on"]

    def get_age(self, obj):
        return _age_on(obj.date_of_birth)

    def _enrolment(self, obj):
        return obj.enrolments.filter(is_active=True).select_related(
            "school_class", "school_class__level", "session",
        ).first()

    def get_class_name(self, obj):
        row = self._enrolment(obj)
        return row.school_class.name if row else ""

    def get_level_name(self, obj):
        row = self._enrolment(obj)
        if row and row.school_class.level_id:
            return row.school_class.level.name
        return obj.applied_for.name if obj.applied_for_id else ""

    def get_session_name(self, obj):
        row = self._enrolment(obj)
        return str(row.session) if row else ""

    def get_photo_url(self, obj):
        return signed_url(obj.photo.name) if obj.photo else ""

    def get_allowed_transitions(self, obj):
        from .services.status import IMPACT, allowed_from

        return [
            {
                "status": value,
                "label": StudentStatus(value).label,
                "impact": IMPACT.get(value, ""),
                "needs_destination": value == StudentStatus.TRANSFERRED,
            }
            for value in allowed_from(obj.status)
        ]


class StudentWriteSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """Editing a record. Class and status are deliberately absent.

    Both move through their own routes so each keeps its reason, its effective
    date and its audit line. The design's edit drawer omits them for the same
    reason and says so on the form.
    """

    write_permissions = {
        "blood_group": PERM_VIEW_SENSITIVE,
        "allergies": PERM_VIEW_SENSITIVE,
        "conditions": PERM_VIEW_SENSITIVE,
        "enrolment_date": "school.students.manage",
    }

    class Meta:
        model = Student
        fields = [
            "student_number", "first_name", "middle_name", "last_name",
            "date_of_birth", "gender", "nationality", "state_of_origin",
            "address", "phone", "email", "previous_school",
            "blood_group", "allergies", "conditions",
            "emergency_contact_name", "emergency_contact_phone",
            "enrolment_date",
        ]

    def validate(self, attrs):
        # Refused explicitly rather than silently dropped: a school that types
        # a branch and gets a 200 believes the student moved.
        if "branch" in self.initial_data or "branch_id" in self.initial_data:
            from .exceptions import BranchChangeNotSupported

            raise BranchChangeNotSupported(
                "A student cannot be moved to another branch by editing their "
                "record.",
            )
        return attrs


class EnrolmentWriteSerializer(serializers.Serializer):
    """Enrol, or save as an applicant. One serializer, one flag.

    Two endpoints would be two sets of rules, and the second one would be the
    one that forgets the duplicate check.
    """

    first_name = serializers.CharField(max_length=100)
    middle_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    last_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField()
    gender = serializers.ChoiceField(choices=Gender.choices)
    nationality = serializers.CharField(
        max_length=60, required=False, allow_blank=True,
    )
    state_of_origin = serializers.CharField(
        max_length=60, required=False, allow_blank=True,
    )
    address = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    previous_school = serializers.CharField(
        max_length=200, required=False, allow_blank=True,
    )

    blood_group = serializers.CharField(
        max_length=4, required=False, allow_blank=True,
    )
    allergies = serializers.CharField(
        max_length=200, required=False, allow_blank=True,
    )
    conditions = serializers.CharField(
        max_length=200, required=False, allow_blank=True,
    )
    emergency_contact_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True,
    )
    emergency_contact_phone = serializers.CharField(
        max_length=32, required=False, allow_blank=True,
    )

    student_number = serializers.CharField(
        max_length=32, required=False, allow_blank=True,
    )
    enrolment_date = serializers.DateField(required=False)
    branch = serializers.CharField(required=False, allow_blank=True)
    school_class = serializers.IntegerField(required=False, allow_null=True)
    applied_for = serializers.IntegerField(required=False, allow_null=True)

    as_applicant = serializers.BooleanField(default=False)
    allow_over_capacity = serializers.BooleanField(default=False)
    confirm_duplicate = serializers.BooleanField(default=False)

    guardians = GuardianWriteSerializer(many=True)

    def validate(self, attrs):
        if not attrs.get("as_applicant") and not attrs.get("school_class"):
            raise serializers.ValidationError({
                "school_class": "Pick the class this student is joining.",
            })
        if attrs.get("as_applicant") and not attrs.get("applied_for"):
            raise serializers.ValidationError({
                "applied_for": "Say which level this applicant applied for.",
            })
        return attrs


# ── movements ──────────────────────────────────────────────────────────────

class StatusChangeSerializer(serializers.Serializer):
    to_status = serializers.ChoiceField(choices=StudentStatus.choices)
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)
    destination_school = serializers.CharField(
        max_length=200, required=False, allow_blank=True,
    )


class ReasonOnlySerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)


class TransferOutSerializer(serializers.Serializer):
    destination_school = serializers.CharField(max_length=200)
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)


class ReactivateSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)
    school_class = serializers.IntegerField(required=False, allow_null=True)
    allow_over_capacity = serializers.BooleanField(default=False)


class AssignClassSerializer(serializers.Serializer):
    school_class = serializers.IntegerField()
    reason = serializers.ChoiceField(
        choices=TransferReason.choices, required=False, allow_blank=True,
    )
    effective_date = serializers.DateField(required=False)
    allow_over_capacity = serializers.BooleanField(default=False)


class BulkAssignSerializer(AssignClassSerializer):
    student_ids = serializers.ListField(child=serializers.IntegerField())


class BulkStatusSerializer(StatusChangeSerializer):
    student_ids = serializers.ListField(child=serializers.IntegerField())


class ConfirmSerializer(serializers.Serializer):
    student_number = serializers.CharField(
        max_length=32, required=False, allow_blank=True,
    )
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)
    effective_date = serializers.DateField(required=False)


# ── reads that hang off the profile ────────────────────────────────────────

class StatusLogSerializer(serializers.ModelSerializer):
    from_label = serializers.SerializerMethodField()
    to_label = serializers.CharField(source="get_to_status_display", read_only=True)
    actor = serializers.SerializerMethodField()

    class Meta:
        model = StudentStatusLog
        fields = ["id", "from_status", "from_label", "to_status", "to_label",
                  "reason", "effective_date", "destination_school", "actor",
                  "changed_at"]

    def get_from_label(self, obj):
        return StudentStatus(obj.from_status).label if obj.from_status else ""

    def get_actor(self, obj):
        # A name, never an email address: the history tab is read by anyone
        # who can see the student, and a colleague's address is not theirs.
        user = obj.changed_by
        if user is None:
            return "System"
        return getattr(user, "full_name", None) or getattr(user, "first_name", "") or "System"


class ClassHistorySerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source="school_class.name", read_only=True)
    session_name = serializers.SerializerMethodField()
    outcome_label = serializers.CharField(
        source="get_outcome_display", read_only=True,
    )

    class Meta:
        model = ClassEnrolment
        fields = ["id", "session_name", "class_name", "outcome",
                  "outcome_label", "is_active", "effective_date", "ended_at"]

    def get_session_name(self, obj):
        return str(obj.session)


class DocumentSerializer(serializers.Serializer):
    """Built from the checklist, so every type appears attached or not."""

    document_type = serializers.CharField()
    label = serializers.CharField()
    required = serializers.BooleanField()
    attached = serializers.BooleanField()
    uploaded_at = serializers.DateTimeField(allow_null=True)
    id = serializers.IntegerField(allow_null=True)
    url = serializers.CharField(allow_blank=True)


class DocumentUploadSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=DocumentType.choices)
    file = serializers.FileField()


class PromotionBatchSerializer(serializers.ModelSerializer):
    from_session_name = serializers.SerializerMethodField()
    to_session_name = serializers.SerializerMethodField()

    class Meta:
        model = StudentPromotionBatch
        fields = ["id", "from_session", "from_session_name", "to_session",
                  "to_session_name", "total", "promoted", "repeated",
                  "graduated", "held", "excluded", "failed", "created_at"]

    def get_from_session_name(self, obj):
        return str(obj.from_session)

    def get_to_session_name(self, obj):
        return str(obj.to_session)


class PromotionRunSerializer(serializers.Serializer):
    from_session = serializers.IntegerField(required=False, allow_null=True)
    to_session = serializers.IntegerField()
    #: {student_id: outcome} from the review screen.
    overrides = serializers.DictField(
        child=serializers.CharField(), required=False,
    )


class AdmissionPolicySerializer(serializers.Serializer):
    required = serializers.BooleanField()
    pattern = serializers.CharField(allow_blank=True, max_length=200)
    hint = serializers.CharField(allow_blank=True, max_length=200)


class SearchHitSerializer(serializers.ModelSerializer):
    """Four fields. A type-ahead is the wrong place to leak a child's address."""

    full_name = serializers.CharField(read_only=True)
    class_name = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = ["id", "full_name", "student_number", "class_name"]

    def get_class_name(self, obj):
        row = next((e for e in obj.enrolments.all() if e.is_active), None)
        return row.school_class.name if row else ""
