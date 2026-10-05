"""What the API exposes, and what it deliberately does not.

Three rules run through every serializer here.

**No medical field ever appears in a list.** Not blood group, not allergies,
not conditions, not the emergency contact. A list is the response that gets
paged, cached and exported; a child's medical history has no business in one.

**Every medical field carries a Field Access switch.** Blood group, allergies,
conditions and the emergency contact's name and phone are registered fields of
``school.students`` (see field_access.py), so a school decides per role who
reads and corrects them. The emergency contact's switches start open: a
contact only a school administrator can read is useless in the emergency it
exists for.

**A file is a signed, user-bound, expiring URL, never a path.** An unsigned
``/media/<name>`` inside its window is a bearer token.

**Every serializer that can write a registered field names its resource.**
The guard binds only the serializer declaring it, so enrolling and editing
each declare ``school.students``, and a serializer that is only ever read
marks its fields read-only rather than leaving another door.

FRD M11 v2.4 sections 7 and 12.1.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin, can_read

from .constants import (
    AGE_RULE_CEILING,
    AGE_RULE_FLOOR,
    DEFAULT_CAPACITY_MAX,
    DEFAULT_CAPACITY_MIN,
    EXTRA_RELATIONSHIP_MAX_LENGTH,
    EXTRA_RELATIONSHIPS_MAX,
    GUARDIAN_MINIMUM_CEILING,
    GUARDIAN_MINIMUM_FLOOR,
    REQUIRABLE_FIELDS,
    CapacityMode,
    DocumentType,
    Gender,
    GuardianMatching,
    PromotionArms,
    PromotionCapacityMode,
    PromotionNotPlaced,
    PromotionSuspended,
    StudentStatus,
    TransferReason,
)
from .field_access import STATUS_REASON_FIELD
from .models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentDocument,
    StudentGuardian,
    StudentPromotionBatch,
    StudentStatusLog,
)
from .services import documents as document_service


def _age_on(dob, when=None, *, tenant=None, branch=None):
    """Age in whole years on *when*, or on today at the student's branch."""
    from vs_config.clock import branch_today

    if not dob:
        return None
    when = when or branch_today(tenant, branch)
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


#: A person's name parts, each mapped to where its value is read. The order is
#: the order a one-line name is written in.
PERSON_NAME_PARTS = {
    "first_name": "first_name", "middle_name": "middle_name", "last_name": "last_name",
}
GUARDIAN_NAME_PARTS = PERSON_NAME_PARTS


# ── guardians ──────────────────────────────────────────────────────────────

class GuardianSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A guardian's own details, as a student's record carries them.

    The phone number, email address, home address and occupation are the
    registered fields of ``school.guardians``, so a school decides per role who
    reads them. This shape travels nested inside a student's guardian list as
    well as on its own, and a nested serializer is filtered as its own resource.
    """

    field_resource = "school.guardians"
    field_composites = {"full_name": GUARDIAN_NAME_PARTS}

    has_account = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Guardian
        fields = [
            "id", "full_name", "first_name", "middle_name", "last_name",
            "name_needs_review", "phone", "email", "occupation", "address",
            "has_account", "photo_url",
        ]

    def get_photo_url(self, obj):
        return guardian_photo_url(obj, request=self.context.get("request"))

    def get_has_account(self, obj):
        # Whether they have a login, never which User row it is: an internal
        # id on a parent's record is an identifier nobody on this screen needs.
        return obj.user_id is not None


class GuardianUpdateSerializer(FieldAccessMixin, serializers.Serializer):
    """A guardian's OWN details. Not their link to any one student.

    Every field optional, because this is a correction: a registrar fixing a
    mistyped phone number should not have to resend the address to keep it.
    Relationship and primary-contact are absent on purpose - those belong to a
    LINK, one per student, and a guardian standing for three children has three
    of them.

    A correction is an update, so every registered field it carries asks the
    Write switch, and a form that echoes back a value it did not change is
    accepted rather than refused.

    The name is corrected in its parts, never as one line: a one-line name
    would change all three parts under whichever single switch it answered
    to. Sending the parts is also what confirms a name split from one line
    (``Guardian.name_needs_review``).
    """

    field_resource = "school.guardians"

    first_name = serializers.CharField(max_length=100, required=False)
    middle_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    occupation = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    address = serializers.CharField(required=False, allow_blank=True)

    def validate_first_name(self, value):
        # Blank is refused rather than allowed through: a guardian with no name
        # is a row nobody can identify on the directory or in a ward list.
        if not (value or "").strip():
            raise serializers.ValidationError("A guardian needs a first name.")
        return value

    def validate_last_name(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("A guardian needs a last name.")
        return value

    def validate(self, attrs):
        # Refused rather than ignored: a client still sending the one-line name
        # would otherwise be told the save succeeded while nothing changed.
        if "full_name" in self.initial_data:
            raise serializers.ValidationError({
                "full_name": (
                    "Correct a guardian's name in its parts: first_name, "
                    "middle_name and last_name."
                ),
            })
        return attrs


class GuardianLinkSerializer(serializers.ModelSerializer):
    """One of a student's guardians, as the profile lists them.

    ``relationship`` is the fixed code, OTHER for a relationship the school
    added for itself; ``relationship_label`` is what to show, which is the
    school's own label where one is stored.
    """

    guardian = GuardianSerializer(read_only=True)
    relationship_label = serializers.CharField(read_only=True)
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


def guardian_photo_url(guardian, *, request=None):
    """A guardian's face, absolute so the browser asks the API for it.

    Absolute for the same reason every other media url in the platform is: a
    bare ``/media/`` path resolves against the FRONTEND's origin, which serves
    the single-page app and not the file.
    """
    from core.media import signed_url

    return (
        signed_url(guardian.photo.name, absolute_for=request)
        if guardian.photo else ""
    )


class GuardianDirectorySerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One row of the guardian directory, with the contact details switched.

    A list row, so it names no read-only fields: the guardian's own record is
    where a form edits them.
    """

    field_resource = "school.guardians"
    field_composites = {"full_name": GUARDIAN_NAME_PARTS}

    photo_url = serializers.SerializerMethodField()
    ward_count = serializers.IntegerField(read_only=True)
    ward_names = serializers.SerializerMethodField()
    is_sibling_household = serializers.SerializerMethodField()

    class Meta:
        model = Guardian
        fields = ["id", "full_name", "first_name", "middle_name", "last_name",
                  "name_needs_review", "phone", "email", "ward_count",
                  "ward_names", "is_sibling_household", "photo_url"]

    def get_photo_url(self, obj):
        return guardian_photo_url(obj, request=self.context.get("request"))

    def get_ward_names(self, obj):
        return self.context.get("wards", {}).get(obj.pk, [])

    def get_is_sibling_household(self, obj):
        return len(self.context.get("wards", {}).get(obj.pk, [])) > 1


class GuardianWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """One guardian on an enrolment or a link.

    Either an existing guardian by id, or a new one by name and phone. Never a
    branch: a guardian is school-level and a request supplying one is refused
    as a field that does not exist rather than accepted and ignored.

    A new guardian's name arrives in parts. A one-line ``full_name`` is still
    accepted for a client that has no parts to send; it is split and flagged
    for review exactly as the spreadsheet imports' one-line column is.

    Creating a guardian rather than editing one, so a blank value for a field
    the caller may not write is dropped instead of refused: the enrol form
    posts every input whether it was touched or not. The phone number is
    declared open on create, because a new guardian cannot be added without
    one; any value is accepted here and only the correction form asks its
    Write switch. A row naming an existing guardian writes none of these
    details: the existing record is linked as it stands.

    The school's guardian rules apply (``services/guardian_rules.py``). The
    relationship is a fixed code or label, or one of the school's own, and
    validates to ``relationship`` plus ``relationship_detail``. At a school
    that requires a guardian email, a new guardian needs one; a row that names
    an existing guardian, by id or by a phone the school matches on, does not,
    so a record held from before the rule can still be linked.
    """

    field_resource = "school.guardians"

    guardian_id = serializers.IntegerField(required=False, allow_null=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    middle_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    occupation = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    address = serializers.CharField(required=False, allow_blank=True)
    relationship = serializers.CharField(
        max_length=64,
        error_messages={
            "blank": "Say how this guardian is related to the child.",
            "required": "Say how this guardian is related to the child.",
            "max_length": "That is not a relationship this school records.",
        },
    )
    is_primary = serializers.BooleanField(default=False)

    def validate(self, attrs):
        from .services.guardians import EMAIL_REQUIRED_MESSAGE, match_existing

        rules = _guardian_rules(self)
        resolved = rules.resolve_relationship(attrs.get("relationship"))
        if resolved is None:
            from .services.guardian_rules import unknown_relationship_message

            raise serializers.ValidationError({
                "relationship": unknown_relationship_message(attrs.get("relationship")),
            })
        attrs["relationship"], attrs["relationship_detail"] = resolved

        if not attrs.get("guardian_id"):
            has_parts = any(
                (attrs.get(part) or "").strip()
                for part in ("first_name", "middle_name", "last_name")
            )
            if has_parts:
                for part, label in (("first_name", "first"), ("last_name", "last")):
                    if not (attrs.get(part) or "").strip():
                        raise serializers.ValidationError({
                            part: f"Give the guardian's {label} name.",
                        })
            elif not (attrs.get("full_name") or "").strip():
                raise serializers.ValidationError({
                    "first_name": "Give the guardian's name, or pick one already at the school.",
                })
            if not (attrs.get("phone") or "").strip():
                raise serializers.ValidationError({
                    "phone": "A guardian needs a phone number the school can reach.",
                })
            if rules.email_required and not (attrs.get("email") or "").strip():
                held = match_existing(
                    _context_tenant(self), phone=attrs.get("phone", ""),
                    matching=rules.matching,
                )
                if held is None:
                    raise serializers.ValidationError({"email": EMAIL_REQUIRED_MESSAGE})
        return attrs


def _guardian_rules(serializer):
    """The school's guardian rules, read once per request however many rows ask.

    Cached in the root serializer's context, which is one dict per request, so
    an enrolment naming three guardians reads the settings once.
    """
    from .services.guardian_rules import read_guardian_rules

    context = serializer.context
    rules = context.get("_guardian_rules")
    if rules is None:
        rules = read_guardian_rules(_context_tenant(serializer))
        context["_guardian_rules"] = rules
    return rules


# ── students ───────────────────────────────────────────────────────────────

class _AdmissionStageFields(serializers.Serializer):
    """Where an application stands, on every row and profile of a student.

    ``admission_stage`` is the stage's id, ``admission_stage_name`` its name
    (blank with no stage). ``offer_expired`` is worked out on every read: an
    applicant whose offer's last day is before the school's today, or before
    the day a past view is read at. Nothing is stored when an offer runs out,
    and a confirmed or rejected record is never expired.

    The school's today is read once per response and kept in the context, so a
    page of fifty rows asks for it once.
    """

    admission_stage = serializers.IntegerField(
        source="admission_stage_id", read_only=True, allow_null=True,
    )
    admission_stage_name = serializers.SerializerMethodField()
    offer_expired = serializers.SerializerMethodField()

    def get_admission_stage_name(self, obj):
        from django.core.exceptions import ObjectDoesNotExist

        if obj.admission_stage_id is None:
            return ""
        try:
            return obj.admission_stage.name
        except ObjectDoesNotExist:
            # A past view naming a stage the school has since removed.
            return ""

    def get_offer_expired(self, obj):
        from .services.admission import offer_expired

        if obj.offer_expires_on is None or obj.status != StudentStatus.APPLICANT:
            return False
        return offer_expired(obj, self._today(obj))

    def _today(self, obj):
        """Today at the applicant's branch, read once per branch for a page."""
        from vs_config.clock import branch_today

        as_at = self.context.get("as_at")
        if as_at:
            return as_at.date
        days = self.context.setdefault("_branch_today", {})
        if obj.branch_id not in days:
            tenant = getattr(self.context.get("request"), "tenant", None) or obj.tenant
            days[obj.branch_id] = branch_today(tenant, obj.branch_id)
        return days[obj.branch_id]


#: The stage fields every student payload carries, in the order they are listed.
ADMISSION_STAGE_FIELDS = [
    "admission_stage", "admission_stage_name", "stage_entered_on",
    "offer_expires_on", "offer_expired",
]


class StudentListSerializer(FieldAccessMixin, _AdmissionStageFields, _BranchAware):
    """The directory row. No medical field, no guardian contact details.

    Every field is read-only: a row is only ever read, and a writable
    ``enrolment_date`` here would be a write path with no field guard on it.

    The enrolment date is a registered field of ``school.students``, so the row
    enforces its Read switch as the profile does: a school that hides when a
    pupil joined from a role hides it from the directory too. A list row names
    no read-only fields.
    """

    field_resource = "school.students"
    field_composites = {"full_name": PERSON_NAME_PARTS}

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
            *ADMISSION_STAGE_FIELDS,
        ]
        read_only_fields = fields

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
        return document_service.face_url(obj, request=self.context.get("request"))


class StudentDetailSerializer(FieldAccessMixin, _AdmissionStageFields, _BranchAware):
    """The profile. Medical is here and behind its switches; never in a list.

    A detail response, so it names in ``_read_only_fields`` whatever the
    caller may read and not change: a school that lets a class teacher see a
    child's allergies without correcting them gets a greyed field rather than
    a save that is refused.

    Rendered for a past day (``context["as_at"]``, see ``as_at.py``) the age is
    the age on that day, the class is the one installed on the rebuilt record,
    and no status move is offered, because nothing can be done to the past.
    """

    field_resource = "school.students"
    field_access_detail = True
    field_composites = {"full_name": PERSON_NAME_PARTS}

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
    suspension = serializers.SerializerMethodField()

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
            *ADMISSION_STAGE_FIELDS,
            "photo_url", "allowed_transitions", "suspension",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "branch", "applied_on", "stage_entered_on", "offer_expires_on",
        ]

    def get_age(self, obj):
        as_at = self.context.get("as_at")
        # The school is asked for only when no day is given: a record read as
        # at a past day is a history snapshot, which carries no tenant.
        if as_at:
            return _age_on(obj.date_of_birth, as_at.date)
        return _age_on(obj.date_of_birth, tenant=obj.tenant, branch=obj.branch_id)

    def _enrolment(self, obj):
        installed = getattr(obj, "_active_enrolments", None)
        if installed is not None:
            return installed[0] if installed else None
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
        return document_service.face_url(obj, request=self.context.get("request"))

    def get_suspension(self, obj):
        """The suspension the pupil is serving, or ``None``.

        ``return_date`` is the day they are expected back, and a null is a
        suspension that stands until somebody lifts it rather than an unknown
        date. ``due_back`` says that day has arrived at the pupil's own
        branch, which is a question a screen cannot answer for itself: the
        branch keeps its own calendar, so a client comparing the date with the
        reader's clock gets a pupil expected back a day early or a day late.
        It stays true until the return is recorded, so a suspension whose day
        has passed reads as a pupil who should be in school.

        ``reason`` is a registered field of ``school.students`` and this block
        is built by hand, so the key is left out entirely for a caller whose
        roles do not grant Read, rather than sent as a null: a null would say
        the school recorded no reason. The rest of the block answers either
        way, because when a pupil is back is not a confidential fact.

        One query, and only for a pupil who is actually suspended, so every
        other profile read costs nothing. Absent on a record read as at a past
        day, which carries no status move either.
        """
        if self.context.get("as_at") or obj.status != StudentStatus.SUSPENDED:
            return None
        row = (
            obj.status_logs.filter(to_status=StudentStatus.SUSPENDED)
            .order_by("-changed_at", "-id").first()
        )
        if row is None:
            return None
        today = self._today(obj)
        block = {"effective_date": row.effective_date}
        if can_read(self.context.get("request"), STATUS_REASON_FIELD):
            block["reason"] = row.reason
        block["return_date"] = row.return_date
        block["due_back"] = bool(row.return_date and row.return_date <= today)
        return block

    def get_allowed_transitions(self, obj):
        from .services.status import IMPACT, allowed_from

        if self.context.get("as_at"):
            return []

        return [
            {
                "status": value,
                "label": StudentStatus(value).label,
                "impact": IMPACT.get(value, ""),
                "needs_destination": value == StudentStatus.TRANSFERRED,
            }
            for value in allowed_from(obj.status)
        ]


def _context_tenant(serializer):
    """The school a write serializer is judging for: explicit, else the request's."""
    tenant = serializer.context.get("tenant")
    if tenant is None:
        tenant = getattr(serializer.context.get("request"), "tenant", None)
    return tenant


def _plausible_birth_date(value, tenant, branch=None):
    """Refuse a birth date outside the school's age range, in the import's words.

    Judged on today at *branch* where the student's branch is known (an edit),
    else on the school's today (an enrolment, whose branch is resolved later).
    """
    from .ages import date_of_birth_problem

    problem = (
        date_of_birth_problem(value, tenant=tenant, branch=branch) if value else ""
    )
    if problem:
        raise serializers.ValidationError(problem)
    return value


def _required_field_errors(tenant, attrs, *, only_sent):
    """``{field: sentence}`` for each field the school requires that is blank.

    With *only_sent*, a field missing from *attrs* is not checked, which is how
    an edit refuses blanking a required field without refusing a record that
    predates the rule and was never asked for it.
    """
    from .constants import REQUIRABLE_FIELDS
    from .services.rules import required_fields

    if tenant is None:
        return {}
    errors = {}
    for field in required_fields(tenant):
        if only_sent and field not in attrs:
            continue
        if not str(attrs.get(field) or "").strip():
            errors[field] = f"{REQUIRABLE_FIELDS[field]} is required at this school."
    return errors


class StudentWriteSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """Editing a record. Class and status are deliberately absent.

    Both move through their own routes so each keeps its reason, its effective
    date and its audit line. The design's edit drawer omits them for the same
    reason and says so on the form.

    Editing an existing record, so every registered field it carries is
    governed by its Write switch, the enrolment date included: correcting when
    a child joined rewrites their history.
    """

    field_resource = "school.students"

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

    def validate_date_of_birth(self, value):
        return _plausible_birth_date(
            value, _context_tenant(self),
            getattr(self.instance, "branch_id", None),
        )

    def validate(self, attrs):
        # Refused explicitly rather than silently dropped: a school that types
        # a branch and gets a 200 believes the student moved.
        if "branch" in self.initial_data or "branch_id" in self.initial_data:
            from .exceptions import BranchChangeNotSupported

            raise BranchChangeNotSupported(
                "A student cannot be moved to another branch by editing their "
                "record.",
            )
        errors = _required_field_errors(_context_tenant(self), attrs, only_sent=True)
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class EnrolmentWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """Enrol, or save as an applicant. One serializer, one flag.

    Two endpoints would be two sets of rules, and the second one would be the
    one that forgets the duplicate check.

    The medical fields carry the same rule here as on the edit route: a role
    whose Write switch does not reach a child's blood group, allergies or
    conditions is refused, per field, and nothing is created. A blank value is
    dropped rather than refused, because the enrol form posts every input
    whether it was touched or not. The enrolment date is the exception the
    registry marks: whoever enrols the pupil sets the date they enrolled on,
    and the switch decides only who may correct it afterwards. The view must
    pass the request in the context, or the guard has no caller to judge and
    skips itself.
    """

    field_resource = "school.students"

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

    def validate_date_of_birth(self, value):
        return _plausible_birth_date(value, _context_tenant(self))

    def validate(self, attrs):
        errors = {}
        if not attrs.get("as_applicant") and not attrs.get("school_class"):
            errors["school_class"] = "Pick the class this student is joining."
        if attrs.get("as_applicant") and not attrs.get("applied_for"):
            errors["applied_for"] = "Say which level this applicant applied for."
        # The school's own required fields, for an applicant as for an
        # enrolment: an application is where the record's details are taken.
        errors.update(
            _required_field_errors(_context_tenant(self), attrs, only_sent=False),
        )
        if errors:
            raise serializers.ValidationError(errors)
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


class SuspendSerializer(serializers.Serializer):
    """The one status route that does not insist on a reason.

    A school suspending a pupil pending an investigation has nothing truthful
    to type yet, and a required field buys a placeholder rather than a record.
    Withdrawal, transfer and graduation still require one: each is a child
    leaving the roll, and none of them is ever recorded before the school
    knows why.

    ``return_date`` is the day the pupil is expected back, and it is optional
    too. With one, the pupil returns on that day without anybody acting; with
    none, the suspension stands until a person lifts it. It is refused on or
    before the day the suspension begins.

    ``send_reason`` decides only whether the guardian's notice repeats the
    reason, never whether the history keeps it, which it always does. It
    defaults to withholding, because an unweighed sentence about a child in an
    email cannot be taken back.
    """

    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)
    send_reason = serializers.BooleanField(default=False)
    effective_date = serializers.DateField(required=False)
    return_date = serializers.DateField(required=False, allow_null=True)


class TransferOutSerializer(serializers.Serializer):
    destination_school = serializers.CharField(max_length=200)
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)


class ReactivateSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)
    effective_date = serializers.DateField(required=False)
    school_class = serializers.IntegerField(required=False, allow_null=True)
    allow_over_capacity = serializers.BooleanField(default=False)


#: The reasons a class transfer may give. A branch move writes its own, and a
#: class move that claimed it would put a branch move in the history that never
#: happened.
CLASS_TRANSFER_REASONS = [
    choice for choice in TransferReason.choices
    if choice[0] != TransferReason.BRANCH_MOVE
]


class AssignClassSerializer(serializers.Serializer):
    school_class = serializers.IntegerField()
    reason = serializers.ChoiceField(
        choices=CLASS_TRANSFER_REASONS, required=False, allow_blank=True,
    )
    effective_date = serializers.DateField(required=False)
    allow_over_capacity = serializers.BooleanField(default=False)


class BulkAssignSerializer(AssignClassSerializer):
    student_ids = serializers.ListField(child=serializers.IntegerField())


class BranchMovePreviewSerializer(serializers.Serializer):
    """Where a pupil would move and on which day. The branch is an id, as everywhere."""

    to_branch = serializers.CharField()
    effective_date = serializers.DateField(required=False)


class BranchMoveSerializer(BranchMovePreviewSerializer):
    """A pupil's move to another branch.

    ``school_class`` is the class they join there; it may be left out only for
    a pupil with no class or in a school-wide class. ``reason`` is required:
    a move changes which branch chases a family's money, and the history may
    not hold one unexplained.
    """

    school_class = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=300, trim_whitespace=True)
    allow_over_capacity = serializers.BooleanField(default=False)


class BulkStatusSerializer(StatusChangeSerializer):
    student_ids = serializers.ListField(child=serializers.IntegerField())


class ConfirmSerializer(serializers.Serializer):
    student_number = serializers.CharField(
        max_length=32, required=False, allow_blank=True,
    )
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)
    effective_date = serializers.DateField(required=False)


# ── reads that hang off the profile ────────────────────────────────────────

class StatusLogSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One move in a pupil's status history.

    The reason is a registered field of ``school.students``, so a role reads
    the words a colleague wrote about a child only where the school has turned
    the switch on. Every other column stays: a reader who may not see why a
    pupil was suspended still sees that they were, when it took effect and who
    recorded it, which is what the history tab is for.

    A list serializer, so it names no ``_read_only_fields``: a history row is
    only ever read, and the reason is written on the status routes.
    """

    field_resource = "school.students"

    from_label = serializers.SerializerMethodField()
    to_label = serializers.CharField(source="get_to_status_display", read_only=True)
    actor = serializers.SerializerMethodField()

    class Meta:
        model = StudentStatusLog
        fields = ["id", "from_status", "from_label", "to_status", "to_label",
                  "reason", "effective_date", "return_date",
                  "destination_school", "actor", "changed_at"]

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
    #: Held on the day asked about, and replaced or removed since, so its file
    #: no longer exists. Only a past view sets it.
    file_retired = serializers.BooleanField(default=False)


#: 5 MB. Comfortably more than a phone photograph of a certificate, and small
#: enough that a directory of fifty faces is not a slow page.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

IMAGE_CONTENT_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
})


def check_upload(upload, *, field, must_be_image, subject):
    """Size, and type when the file will be rendered as a picture.

    One function for both photographs. A file accepted here is drawn in an
    ``<img>`` on a list, so a PDF that slips through is a broken picture beside
    a person's name on the directory, the class register and the guardian's
    list of children - and nothing on any of those screens would say why. The
    refusal names what was sent instead, because "invalid file" sends somebody
    trying the same PDF again.

    Raises ``ValidationError`` keyed on ``field`` so the message lands under
    the input the reader would change.
    """
    size = getattr(upload, "size", 0) or 0
    if size > MAX_UPLOAD_BYTES:
        raise serializers.ValidationError({
            field: (
                f"That file is {size // (1024 * 1024)}MB. The limit is "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB."
            ),
        })
    if not must_be_image:
        return
    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type not in IMAGE_CONTENT_TYPES:
        raise serializers.ValidationError({
            field: (
                f"{subject} must be an image - JPEG, PNG, WebP or HEIC. This "
                f"one is a {content_type or 'file of unknown type'}."
            ),
        })


class DocumentUploadSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=DocumentType.choices)
    file = serializers.FileField()

    def validate(self, attrs):
        check_upload(
            attrs["file"], field="file",
            must_be_image=attrs["document_type"] == DocumentType.PASSPORT_PHOTO,
            subject="A passport photograph",
        )
        return attrs


#: The prefix of a file field that carries a document on the enrol form.
ENROLMENT_DOCUMENT_PREFIX = "document_"


def read_enrolment_form(fields, files):
    """The enrolment body and its documents, from a ``multipart/form-data`` request.

    The enrol form sends documents in the same save as the child, which JSON
    cannot carry, so a multipart request holds the JSON body exactly as a JSON
    request would, as the string in the ``payload`` field, and one file per
    document in a field named ``document_<TYPE>`` (``document_BIRTH_CERTIFICATE``).

    Returns ``(body, documents)``, *documents* being ``[{"document_type",
    "file"}]``. Each file passes the same checks as the document upload route
    (:func:`check_upload`). Everything else is refused with a 400 keyed on the
    field at fault: a missing or malformed payload, a document type the
    module does not record, two files for one document, a document field with
    no file in it, and any other field, because a flat field such as
    ``as_applicant`` sent beside the payload would otherwise be ignored and
    the child enrolled when the form meant to save an applicant.
    """
    import json

    errors: dict[str, list[str]] = {}
    raw = fields.get("payload")
    body = None
    if raw in (None, ""):
        errors["payload"] = ["Send the enrolment as JSON in the payload field."]
    else:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            errors["payload"] = ["The payload field must hold a JSON object."]

    for key in fields:
        if key == "payload":
            continue
        if key.startswith(ENROLMENT_DOCUMENT_PREFIX):
            errors[key] = ["Attach a file here, or leave the field out."]
        else:
            errors[key] = [
                "Only payload and document files are read from this form. "
                "Put this value in the payload.",
            ]

    documents = []
    for key in files:
        document_type = key[len(ENROLMENT_DOCUMENT_PREFIX):]
        if not key.startswith(ENROLMENT_DOCUMENT_PREFIX) or (
            document_type not in DocumentType.values
        ):
            errors[key] = [
                "This is not a document the school records. Send each one as "
                "document_ followed by one of "
                f"{', '.join(DocumentType.values)}.",
            ]
            continue
        uploads = files.getlist(key)
        if len(uploads) != 1:
            errors[key] = ["Attach one file for each document."]
            continue
        try:
            check_upload(
                uploads[0], field=key,
                must_be_image=document_type == DocumentType.PASSPORT_PHOTO,
                subject="A passport photograph",
            )
        except serializers.ValidationError as exc:
            detail = exc.detail[key]
            errors[key] = detail if isinstance(detail, list) else [detail]
            continue
        documents.append({"document_type": document_type, "file": uploads[0]})

    if errors:
        raise serializers.ValidationError(errors)
    return body, documents


class PhotoUploadSerializer(serializers.Serializer):
    """A guardian's photograph. Always optional, never a gate on anything."""

    photo = serializers.FileField()

    def validate(self, attrs):
        check_upload(
            attrs["photo"], field="photo", must_be_image=True,
            subject="A photograph",
        )
        return attrs


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
    #: Go ahead although the preview listed classes over capacity.
    allow_over_capacity = serializers.BooleanField(default=False)


class AdmissionPolicySerializer(serializers.Serializer):
    """The school's admission-number rule, or one branch's.

    ``auto_issue`` may be left out, which keeps the value the school or branch
    reads today, so a client that predates it cannot switch it off by saving.
    """

    required = serializers.BooleanField()
    pattern = serializers.CharField(allow_blank=True, max_length=200)
    hint = serializers.CharField(allow_blank=True, max_length=200)
    auto_issue = serializers.BooleanField(required=False)


class EnrolmentRulesSerializer(serializers.Serializer):
    """The full set of a school's enrolment rules, as the settings screen saves it.

    Every rule is sent every time, because the screen shows every rule; a
    partial save would leave a rule the admin could see set to something they
    did not choose. Each refusal is keyed on its own field and written as a
    sentence the screen can put under that field.
    """

    min_age_years = serializers.IntegerField(
        min_value=AGE_RULE_FLOOR, max_value=AGE_RULE_CEILING,
        error_messages={
            "min_value": f"The youngest age cannot be below {AGE_RULE_FLOOR}.",
            "max_value": f"The youngest age cannot be above {AGE_RULE_CEILING}.",
            "invalid": "Give the youngest age as a whole number of years.",
        },
    )
    max_age_years = serializers.IntegerField(
        min_value=AGE_RULE_FLOOR, max_value=AGE_RULE_CEILING,
        error_messages={
            "min_value": f"The oldest age cannot be below {AGE_RULE_FLOOR}.",
            "max_value": f"The oldest age cannot be above {AGE_RULE_CEILING}.",
            "invalid": "Give the oldest age as a whole number of years.",
        },
    )
    required_documents = serializers.ListField(
        child=serializers.CharField(), allow_empty=True,
    )
    required_fields = serializers.ListField(
        child=serializers.CharField(), allow_empty=True,
    )
    capacity_mode = serializers.ChoiceField(
        choices=CapacityMode.choices,
        error_messages={
            "invalid_choice": "Choose WARN, HARD or OFF for what a full class does.",
        },
    )
    default_capacity = serializers.IntegerField(
        allow_null=True,
        min_value=DEFAULT_CAPACITY_MIN, max_value=DEFAULT_CAPACITY_MAX,
        error_messages={
            "min_value": f"A class needs at least {DEFAULT_CAPACITY_MIN} seat.",
            "max_value": (
                f"A class of more than {DEFAULT_CAPACITY_MAX} is not a class. "
                f"Leave the default empty for no limit."
            ),
            "invalid": "Give the default class size as a whole number of seats.",
        },
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
    )

    def validate_required_documents(self, value):
        known = dict(DocumentType.choices)
        unknown = [v for v in value if v not in known]
        if unknown:
            raise serializers.ValidationError(
                f"'{unknown[0]}' is not a document this school can ask for.",
            )
        return value

    def validate_required_fields(self, value):
        unknown = [v for v in value if v not in REQUIRABLE_FIELDS]
        if unknown:
            raise serializers.ValidationError(
                f"'{unknown[0]}' is not a field this school can make required.",
            )
        return value

    def validate(self, attrs):
        if attrs["min_age_years"] >= attrs["max_age_years"]:
            raise serializers.ValidationError({
                "max_age_years": "The oldest age must be above the youngest.",
            })
        return attrs


class GuardianRulesSerializer(serializers.Serializer):
    """The full set of a school's guardian rules, as the settings screen saves it.

    Every rule is sent every time, for the reason ``EnrolmentRulesSerializer``
    gives. The added relationships are each trimmed and must be 1 to 30
    characters, distinct ignoring case, and not a relationship every school
    already has, because a second "Mother" would be two answers to one
    question. Each refusal is keyed on its own field, as a sentence.
    """

    min_per_student = serializers.IntegerField(
        min_value=GUARDIAN_MINIMUM_FLOOR, max_value=GUARDIAN_MINIMUM_CEILING,
        error_messages={
            "min_value": "Every child needs at least 1 guardian.",
            "max_value": (
                f"A school can ask for at most {GUARDIAN_MINIMUM_CEILING} "
                f"guardians for every child."
            ),
            "invalid": "Give the number of guardians as a whole number.",
            "required": "Say how many guardians every child needs.",
            "null": "Say how many guardians every child needs.",
        },
    )
    email_required = serializers.BooleanField(
        error_messages={
            "invalid": "Say whether a guardian email is required, true or false.",
            "required": "Say whether a guardian email is required.",
            "null": "Say whether a guardian email is required.",
        },
    )
    matching = serializers.ChoiceField(
        choices=GuardianMatching.choices,
        error_messages={
            "invalid_choice": (
                "Choose EMAIL_THEN_PHONE or EMAIL_ONLY for how guardians are "
                "matched."
            ),
            "required": "Say how guardians are matched.",
            "null": "Say how guardians are matched.",
        },
    )
    extra_relationships = serializers.ListField(
        child=serializers.CharField(allow_blank=True, trim_whitespace=True),
        allow_empty=True,
        error_messages={
            "not_a_list": "Give the school's own relationships as a list.",
            "required": "Give the school's own relationships, or an empty list.",
            "null": "Give the school's own relationships, or an empty list.",
        },
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
    )

    def validate_extra_relationships(self, value):
        from .services.guardian_rules import FIXED_SPELLINGS

        if len(value) > EXTRA_RELATIONSHIPS_MAX:
            raise serializers.ValidationError(
                f"A school can add up to {EXTRA_RELATIONSHIPS_MAX} relationships "
                f"of its own.",
            )
        seen: set[str] = set()
        for label in value:
            if not label:
                raise serializers.ValidationError("A relationship needs a name.")
            if len(label) > EXTRA_RELATIONSHIP_MAX_LENGTH:
                raise serializers.ValidationError(
                    f"'{label}' is longer than {EXTRA_RELATIONSHIP_MAX_LENGTH} "
                    f"characters.",
                )
            folded = label.casefold()
            if folded in FIXED_SPELLINGS:
                raise serializers.ValidationError(
                    f"'{label}' is already a relationship every school has.",
                )
            if folded in seen:
                raise serializers.ValidationError(f"'{label}' is listed twice.")
            seen.add(folded)
        return value


class AdmissionStageItemSerializer(serializers.Serializer):
    """One stage as the Applicants settings screen saves it.

    Checked as a set by ``AdmissionRulesSerializer``, which is where the
    sentences are, so a refusal names the stage it is about.
    """

    id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(allow_blank=True, trim_whitespace=True)
    is_offer = serializers.BooleanField(default=False)
    offer_valid_days = serializers.IntegerField(required=False, allow_null=True)


class AdmissionRulesSerializer(serializers.Serializer):
    """The school's admission stages and its documents, as one save.

    ``stages`` is the whole ordered list: its order is the position, an item
    with an ``id`` updates that stage, one without creates one, and a stage
    left out is removed (``services/admission.py``, which also refuses an id
    that is not the school's and a removal that would strand applicants).
    Every refusal is keyed on its field, as one sentence naming the stage.
    """

    stages = AdmissionStageItemSerializer(
        many=True, allow_empty=True,
        error_messages={
            "not_a_list": "Give the admission stages as a list.",
            "required": "Give the admission stages, or an empty list.",
            "null": "Give the admission stages, or an empty list.",
        },
    )
    required_documents_to_confirm = serializers.ListField(
        child=serializers.CharField(), allow_empty=True,
        error_messages={
            "not_a_list": "Give the documents as a list.",
            "required": "Give the documents needed to confirm, or an empty list.",
            "null": "Give the documents needed to confirm, or an empty list.",
        },
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_stages(self, value):
        from .constants import (
            ADMISSION_STAGE_NAME_MAX_LENGTH,
            ADMISSION_STAGES_MAX,
            OFFER_DAYS_MAX,
            OFFER_DAYS_MIN,
        )

        if len(value) > ADMISSION_STAGES_MAX:
            raise serializers.ValidationError(
                f"A school can name up to {ADMISSION_STAGES_MAX} admission stages.",
            )
        seen: set[str] = set()
        for position, item in enumerate(value, start=1):
            name = item["name"]
            if not name:
                raise serializers.ValidationError(f"Stage {position} needs a name.")
            if len(name) > ADMISSION_STAGE_NAME_MAX_LENGTH:
                raise serializers.ValidationError(
                    f"'{name}' is longer than {ADMISSION_STAGE_NAME_MAX_LENGTH} "
                    f"characters.",
                )
            folded = name.casefold()
            if folded in seen:
                raise serializers.ValidationError(f"'{name}' is listed twice.")
            seen.add(folded)
            days = item.get("offer_valid_days")
            if days is None:
                continue
            if not item["is_offer"]:
                raise serializers.ValidationError(
                    f"'{name}' is not an offer stage, so it has no number of "
                    f"days to accept.",
                )
            if not OFFER_DAYS_MIN <= days <= OFFER_DAYS_MAX:
                raise serializers.ValidationError(
                    f"An offer at '{name}' can stay open for {OFFER_DAYS_MIN} "
                    f"to {OFFER_DAYS_MAX} days.",
                )
        return value

    def validate_required_documents_to_confirm(self, value):
        known = dict(DocumentType.choices)
        unknown = [v for v in value if v not in known]
        if unknown:
            raise serializers.ValidationError(
                f"'{unknown[0]}' is not a document this school can ask for.",
            )
        return value


def _choice_errors(choices, what: str) -> dict:
    """A choice field's refusals, as sentences naming the values it takes."""
    values = [value for value, _ in choices.choices]
    listed = f"{', '.join(values[:-1])} or {values[-1]}"
    return {
        "invalid_choice": f"Choose {listed} for {what}.",
        "required": f"Say {what}.",
        "null": f"Say {what}.",
    }


class PromotionRulesSerializer(serializers.Serializer):
    """The full set of a school's promotion rules, as the settings screen saves it.

    Every rule is sent every time, for the reason ``EnrolmentRulesSerializer``
    gives. Each refusal is keyed on its own field, as a sentence.
    """

    suspended = serializers.ChoiceField(
        choices=PromotionSuspended.choices,
        error_messages=_choice_errors(
            PromotionSuspended, "what happens to suspended pupils at promotion",
        ),
    )
    not_placed = serializers.ChoiceField(
        choices=PromotionNotPlaced.choices,
        error_messages=_choice_errors(
            PromotionNotPlaced,
            "what happens to pupils who are confirmed but not placed",
        ),
    )
    arms = serializers.ChoiceField(
        choices=PromotionArms.choices,
        error_messages=_choice_errors(
            PromotionArms, "how promoted pupils are placed in next year's classes",
        ),
    )
    capacity_mode = serializers.ChoiceField(
        choices=PromotionCapacityMode.choices,
        error_messages=_choice_errors(
            PromotionCapacityMode, "what the promotion does when a class is full",
        ),
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
    )


class StageMoveSerializer(serializers.Serializer):
    """Where to put an applicant: a stage's id, or null for no stage.

    ``stage`` must be sent, null included, so an empty body is refused rather
    than read as "no stage".
    """

    stage = serializers.IntegerField(
        allow_null=True,
        error_messages={
            "required": "Say which stage to move the applicant to, or null for none.",
            "invalid": "Give the stage as its id, or null for none.",
        },
    )
    offer_expires_on = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)


class SearchHitSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """Four fields. A type-ahead is the wrong place to leak a child's address.

    The admission number and the name parts behind ``full_name`` are registered
    fields of ``school.students``, so the type-ahead follows the same switches
    as the directory it searches.
    """

    field_resource = "school.students"
    field_composites = {"full_name": PERSON_NAME_PARTS}

    full_name = serializers.CharField(read_only=True)
    class_name = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = ["id", "full_name", "student_number", "class_name"]
        read_only_fields = fields

    def get_class_name(self, obj):
        row = next((e for e in obj.enrolments.all() if e.is_active), None)
        return row.school_class.name if row else ""
