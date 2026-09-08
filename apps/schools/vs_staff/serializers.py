"""What the staff screens read and write.

Two rules run through this file. **The account is a labelled object, never
merged into the record**: employment and account status answer different
questions, and a payload that flattened them would let a screen render a
security lockout as a suspension. And **a file is emitted as a media path**,
never as a signed or guessable direct link, because a file URL looks like a URL
rather than like a record and is the payload most likely to leak.

FRD M12 v2.1, sections 9 and 12.
"""
from __future__ import annotations

from rest_framework import serializers

from .constants import (
    DocumentType,
    EmploymentStatus,
    EmploymentType,
    LeaveType,
    TeachingPart,
)
from .models import (
    LeaveRequest,
    StaffDocument,
    StaffEmploymentEvent,
    StaffProfile,
    StaffQualification,
    TeachingAssignment,
)


def _full_name(user) -> str:
    if user is None:
        return ""
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip()


def _actor(user):
    """An id and a display name, and never an email address.

    A history is the most widely read part of a profile, and the platform
    already applies this rule to ``created_by`` everywhere else.
    """
    if user is None:
        return None
    return {"id": user.pk, "name": _full_name(user)}


class AccountStateSerializer(serializers.Serializer):
    """The account half, kept as its own object on purpose.

    ``can_sign_in`` and ``can_hold_password`` are read from the model's own
    allow-lists rather than recomputed, so a status added later cannot quietly
    acquire a meaning here that the auth layer does not give it.
    """

    status = serializers.CharField()
    label = serializers.SerializerMethodField()
    can_sign_in = serializers.BooleanField(source="may_sign_in")
    can_hold_password = serializers.BooleanField(source="may_hold_password")
    email = serializers.EmailField()

    def get_label(self, obj) -> str:
        return obj.get_status_display()


#: The relations :class:`StaffListSerializer` reads off every row.
#:
#: Named once because two places build a queryset for this serializer - the
#: directory's base queryset and the branch roster - and the roster forgot the
#: invitation, which is one query per person on a screen that lists everybody
#: at a branch. A list of relations kept in two heads drifts; this one is kept
#: beside the serializer that needs it.
STAFF_LIST_PREFETCH = (
    "user__tenant_role_assignments__role",
    "user__invitation",
)


class StaffListSerializer(serializers.ModelSerializer):
    """One row of the directory.

    Carries the teaching load and an account flag, which are the two facts a row
    cannot be read without: a count of assignments says whether somebody teaches
    without anybody having to claim they are a teacher, and the flag is how
    somebody reads as employed and locked at once.
    """

    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="user.email", read_only=True)
    account_status = serializers.CharField(source="user.status", read_only=True)
    account_flag = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    posted_school_wide = serializers.SerializerMethodField()
    teaching_load = serializers.SerializerMethodField()
    employment_status_label = serializers.CharField(
        source="get_employment_status_display", read_only=True,
    )
    #: What the row READS as, which is not always what the column holds.
    #:
    #: Five stored values and one derived, the same split ``LeaveRequest``
    #: already keeps: Completed is what an approved absence reads once its end
    #: date has passed, and On Leave is what an employed person reads while one
    #: is running. Nobody sets either, because a value stored beside the dates
    #: it follows from is a second thing that can be wrong - and here it would
    #: be wrong in a particular direction, since a leave ENDING fires no event
    #: and nothing in this repository runs on a schedule to notice.
    #:
    #: The stored ``employment_status`` is kept beside it rather than replaced.
    #: The history, the lifecycle strip and the transition rules all reason
    #: about what a person DECIDED, and "she was moved to Suspended" is a
    #: different sentence from "her leave was running that week".
    #: Still employed. Read from the model property so the two statuses that
    #: mean "has left" are decided in one place rather than re-derived by every
    #: screen that wants to draw a finished row differently.
    on_roll = serializers.BooleanField(source="is_on_roll", read_only=True)
    display_employment_status = serializers.SerializerMethodField()
    display_employment_status_label = serializers.SerializerMethodField()
    on_leave_today = serializers.SerializerMethodField()
    #: The last day of the leave that is running, or null when none is.
    on_leave_until = serializers.SerializerMethodField()
    #: True exactly when the account is still waiting to be activated, which is
    #: the only state a resend applies to. The screen reads it to decide whether
    #: to offer the control rather than offering one that 422s.
    can_resend = serializers.SerializerMethodField()
    invited_at = serializers.SerializerMethodField()

    class Meta:
        model = StaffProfile
        fields = [
            # ``user_id`` is here because the payroll roster links on it: a
            # finance row points at an account, not at a staff record, because
            # the finance engine is domain-neutral and knows nothing about
            # staff. Without it a bursar cannot tie a salary to a person.
            "id", "user_id", "full_name", "email", "staff_number", "job_title",
            "employment_status", "employment_status_label", "on_roll",
            "display_employment_status", "display_employment_status_label",
            "employment_type",
            "account_status", "account_flag", "roles", "branch_id",
            "branch_name", "posted_school_wide", "teaching_load",
            "on_leave_today", "on_leave_until", "hire_date", "can_resend",
            "invited_at",
        ]

    def get_full_name(self, obj) -> str:
        return _full_name(obj.user)

    def get_roles(self, obj) -> list:
        """Every distinct role name, from the prefetched grants.

        De-duplicated, because one person may hold the same role at two
        branches and that is one role to read; sorted, so the same person reads
        the same way on every page load rather than in whatever order the
        database happened to return.
        """
        return sorted({
            (grant.role.name or grant.role.key)
            for grant in obj.user.tenant_role_assignments.all()
            if grant.assignment_status == "ACTIVE" and grant.role_id
        })

    def get_branch_name(self, obj):
        if not self.context.get("multi_branch"):
            return None
        return obj.branch.name if obj.branch_id else "School-wide"

    def get_posted_school_wide(self, obj):
        if not self.context.get("multi_branch"):
            return None
        return obj.branch_id is None

    def get_teaching_load(self, obj) -> int:
        return getattr(obj, "teaching_load", 0) or 0

    def get_on_leave_today(self, obj) -> bool:
        """Approved leave covering today.

        Reads the queryset annotation where there is one, so a list page
        answers from the same expression it filtered and counted with. The
        context set is the fallback for the few reads that serialize an
        instance rather than a page.
        """
        annotated = getattr(obj, "is_on_leave", None)
        if annotated is not None:
            return bool(annotated)
        return obj.pk in (self.context.get("on_leave_ids") or set())

    def get_on_leave_until(self, obj):
        """When they are back, for the chip that says they are away.

        Null unless the leave is actually running: an end date without the flag
        beside it would let a screen say somebody was away until a date that had
        already passed.
        """
        if not self.get_on_leave_today(obj):
            return None
        return getattr(obj, "on_leave_until", None)

    def get_display_employment_status(self, obj) -> str:
        """On Leave while it is running, and the stored value otherwise.

        Only an ACTIVE person reads as On Leave. Somebody suspended, resigned
        or terminated with a leave request still on the books is not away, they
        are gone or stopped, and the heavier fact is the one the row must show.
        """
        if (
            obj.employment_status == EmploymentStatus.ACTIVE
            and self.get_on_leave_today(obj)
        ):
            return EmploymentStatus.ON_LEAVE
        return obj.employment_status

    def get_display_employment_status_label(self, obj) -> str:
        return dict(EmploymentStatus.choices).get(
            self.get_display_employment_status(obj), "",
        )

    def get_can_resend(self, obj) -> bool:
        from vs_user.models import User

        return obj.user.status == User.Status.PENDING

    def get_invited_at(self, obj):
        invitation = getattr(obj.user, "invitation", None)
        return getattr(invitation, "created_at", None) or obj.created_at

    def get_account_flag(self, obj):
        """A chip only where the account disagrees with the employment record.

        Silent in the ordinary case, so the one row that needs reading stands
        out instead of being lost among fifty that say Active twice.
        """
        from vs_user.models import User

        status = obj.user.status
        if status == User.Status.LOCKED:
            return {
                "code": "LOCKED",
                "label": "Locked",
                "note": (
                    "Locked out after failed sign-in attempts. Their employment "
                    "is unaffected."
                ),
            }
        if status == User.Status.SUSPENDED and (
            obj.employment_status != EmploymentStatus.SUSPENDED
        ):
            return {
                "code": "ACCOUNT_SUSPENDED",
                "label": "Account suspended",
                "note": "They cannot sign in, but they still work here.",
            }
        if status == User.Status.PENDING and (
            obj.employment_status != EmploymentStatus.INVITED
        ):
            return {
                "code": "NOT_ACTIVATED",
                "label": "Invitation not accepted",
                "note": "They have not set a password yet, so they cannot sign in.",
            }
        return None


class QualificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffQualification
        fields = [
            "id", "qualification", "institution", "year_obtained", "note",
            "created_at",
        ]

    def validate_year_obtained(self, value):
        """Plausible, and no further.

        A year is checked for being a year. Whether somebody really graduated in
        1998 is not a question this platform can answer, and a tighter rule
        would refuse a genuine record from a school that keeps older ones.
        """
        if value is None:
            return value
        from django.utils import timezone

        this_year = timezone.localdate().year
        if value < 1900 or value > this_year:
            raise serializers.ValidationError(
                f"Enter a year between 1900 and {this_year}.",
            )
        return value


class DocumentSerializer(serializers.ModelSerializer):
    """A file, emitted as a media path.

    ``file_url`` is the authenticated media route and never the storage's own
    address: the bytes live in the database and ``MediaView`` is what decides
    whether this caller may read them.
    """

    document_type_label = serializers.CharField(
        source="get_document_type_display", read_only=True,
    )
    file_url = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = StaffDocument
        fields = [
            "id", "document_type", "document_type_label", "title", "file_url",
            "uploaded_by", "created_at",
        ]

    def get_file_url(self, obj):
        return obj.file.url if obj.file else None

    def get_uploaded_by(self, obj):
        return _actor(obj.uploaded_by)


class DocumentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffDocument
        fields = ["document_type", "title", "file"]

    def validate_file(self, value):
        """Extension and size, read from the storage layer's own limits.

        ``DatabaseStorage`` already enforces both and would refuse the write,
        but it refuses at save time, which reaches the caller as a 500 rather
        than as something a form can show. Asking the same two questions here
        turns it into a 422 naming which one failed. The lists are the
        storage's, not a second copy: a second set of file rules is a second
        thing to keep in step, and the one that is wrong is always the one
        nobody remembered to update.
        """
        import os

        from django.conf import settings

        from core.storage import ALLOWED_EXTENSIONS, MAX_BYTES_DEFAULT

        extension = os.path.splitext(value.name)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                f"{extension or 'That file type'} is not accepted. Upload a PDF "
                f"or an image.",
            )
        ceiling = getattr(settings, "MEDIA_DB_MAX_BYTES", MAX_BYTES_DEFAULT)
        if value.size > ceiling:
            raise serializers.ValidationError(
                f"That file is {value.size // (1024 * 1024)} MB. The limit is "
                f"{ceiling // (1024 * 1024)} MB.",
            )
        return value


class EmploymentEventSerializer(serializers.ModelSerializer):
    from_status_label = serializers.SerializerMethodField()
    to_status_label = serializers.CharField(
        source="get_to_status_display", read_only=True,
    )
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = StaffEmploymentEvent
        fields = [
            "id", "from_status", "from_status_label", "to_status",
            "to_status_label", "reason", "effective_date", "last_working_day",
            "note", "changed_by", "created_at",
        ]

    def get_from_status_label(self, obj):
        if not obj.from_status:
            return None
        return dict(EmploymentStatus.choices).get(obj.from_status, obj.from_status)

    def get_changed_by(self, obj):
        return _actor(obj.changed_by)


class LeaveSerializer(serializers.ModelSerializer):
    leave_type_label = serializers.CharField(
        source="get_leave_type_display", read_only=True,
    )
    #: Four stored values and one derived. Completed is what an approved absence
    #: reads once its end date has passed, and it is computed here rather than
    #: stored beside the date it follows from.
    display_status = serializers.SerializerMethodField()
    requested_by = serializers.SerializerMethodField()
    staff_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "staff_id", "staff_name", "leave_type", "leave_type_label",
            "start_date", "end_date", "days", "note", "status",
            "display_status", "decided_at", "requested_by", "created_at",
        ]

    def get_display_status(self, obj) -> str:
        return obj.display_status()

    def get_requested_by(self, obj):
        return _actor(obj.requested_by)

    def get_staff_name(self, obj) -> str:
        return _full_name(obj.staff.user)


class LeaveWriteSerializer(serializers.Serializer):
    """What a form may send. Deliberately without ``status``.

    A status a form can set is a status that disagrees with the instance that
    decided it: an administrator could mark their own leave approved without
    anybody voting, and the profile would show an approval the trail has no
    record of. The workflow handler writes it and nothing else does.
    """

    leave_type = serializers.ChoiceField(choices=LeaveType.choices)
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    days = serializers.IntegerField(required=False, min_value=1, max_value=366)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class LeaveUpdateSerializer(serializers.Serializer):
    leave_type = serializers.ChoiceField(choices=LeaveType.choices, required=False)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    days = serializers.IntegerField(required=False, min_value=1, max_value=366)
    note = serializers.CharField(required=False, allow_blank=True)


class TeachingAssignmentSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    class_name = serializers.CharField(source="school_class.name", read_only=True)
    part_label = serializers.CharField(source="get_part_display", read_only=True)
    staff_name = serializers.SerializerMethodField()

    class Meta:
        model = TeachingAssignment
        fields = [
            "id", "staff_id", "staff_name", "subject_id", "subject_name",
            "school_class_id", "class_name", "session_id", "part", "part_label",
            "created_at",
        ]

    def get_staff_name(self, obj) -> str:
        return _full_name(obj.staff.user)


class TeachingWriteSerializer(serializers.Serializer):
    school_class = serializers.IntegerField()
    subject = serializers.IntegerField()
    session = serializers.IntegerField(required=False)
    part = serializers.ChoiceField(
        choices=TeachingPart.choices, default=TeachingPart.LEAD,
    )


class StaffDetailSerializer(StaffListSerializer):
    """One person's record, with the account beside it rather than inside it."""

    account = serializers.SerializerMethodField()
    middle_name = serializers.CharField(read_only=True)
    date_of_birth = serializers.DateField(read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    gender = serializers.CharField(source="user.gender", read_only=True)
    photo_url = serializers.SerializerMethodField()
    tenure = serializers.SerializerMethodField()
    lifecycle = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta(StaffListSerializer.Meta):
        fields = StaffListSerializer.Meta.fields + [
            "account", "middle_name", "date_of_birth", "phone", "gender",
            "photo_url", "exit_date", "tenure", "lifecycle", "counts",
            "created_by",
        ]

    def get_account(self, obj):
        return AccountStateSerializer(obj.user).data

    def get_photo_url(self, obj):
        return obj.photo.url if obj.photo else None

    def get_created_by(self, obj):
        return _actor(obj.created_by)

    def get_tenure(self, obj):
        """Derived from the hire date, and absent where there is none.

        Never guessed from ``User.created_at``: when an account was made is not
        when somebody started, and a school reading three years of service off
        an invitation date would be reading a number nobody entered.
        """
        if not obj.hire_date:
            return None
        from django.utils import timezone

        today = timezone.localdate()
        years = today.year - obj.hire_date.year
        months = today.month - obj.hire_date.month
        if today.day < obj.hire_date.day:
            months -= 1
        if months < 0:
            years -= 1
            months += 12
        return {"years": max(years, 0), "months": max(months, 0)}

    def get_lifecycle(self, obj):
        """Where this person sits on the ordinary path, or that they are off it.

        Invited then Active is the whole of the ordinary path. The other four
        statuses are not later stages of it and must not be drawn as though they
        were: a strip that showed Terminated as step three would say a school
        expects everybody to get there.
        """
        path = [EmploymentStatus.INVITED, EmploymentStatus.ACTIVE]
        if obj.employment_status in path:
            return {
                "on_path": True,
                "steps": [
                    {"value": value, "label": dict(EmploymentStatus.choices)[value]}
                    for value in path
                ],
                "current": obj.employment_status,
            }
        return {
            "on_path": False,
            "steps": [],
            "current": obj.employment_status,
            "note": (
                f"{obj.get_employment_status_display()} sits outside the "
                f"ordinary Invited to Active path."
            ),
        }

    def get_counts(self, obj):
        return {
            "qualifications": obj.qualifications.count(),
            "documents": obj.documents.count(),
            "teaching_assignments": obj.teaching_assignments.count(),
            "leave_requests": obj.leave_requests.count(),
        }


class StaffUpdateSerializer(serializers.ModelSerializer):
    """What an administrator may change on a record.

    ``employment_status`` is absent: it moves only through the lifecycle
    service, which is the only place that also does the right thing to the
    account. The email is absent too, because changing an account's address is a
    different key on a different endpoint.
    """

    branch = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    gender = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField(required=False)
    last_name = serializers.CharField(required=False)

    class Meta:
        model = StaffProfile
        fields = [
            "staff_number", "job_title", "employment_type", "hire_date",
            "middle_name", "date_of_birth", "photo", "branch", "phone",
            "gender", "first_name", "last_name",
        ]
        extra_kwargs = {field: {"required": False} for field in fields}


#: What a person may change about themselves, and nothing else.
#:
#: Employment status, employment type, hire date, exit date, staff number, job
#: title and posting are read-only to self at any permission level. A person
#: editing their own hire date is editing their own tenure, and one editing
#: their own job title is giving themselves a promotion the school did not.
SELF_EDITABLE_FIELDS = frozenset({
    "middle_name", "date_of_birth", "photo", "phone",
})


class StaffCreateSerializer(serializers.Serializer):
    """The Add screen, in one payload.

    Wraps ``UserCreateSerializer``'s fields rather than replacing them: the
    account half is validated by the platform's own serializer inside the view,
    and what is declared here is the staff half plus the three child collections
    the form carries.
    """

    # Account half, passed through to UserCreateSerializer.
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    gender = serializers.CharField(required=False, allow_blank=True, default="")
    role = serializers.CharField(max_length=120)
    role_branch = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None,
    )

    # Staff half.
    staff_number = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    job_title = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    employment_type = serializers.ChoiceField(
        choices=EmploymentType.choices, required=False, allow_blank=True, default="",
    )
    hire_date = serializers.DateField(required=False, allow_null=True, default=None)
    branch = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None,
    )
    middle_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    date_of_birth = serializers.DateField(required=False, allow_null=True, default=None)
    photo = serializers.ImageField(required=False, allow_null=True, default=None)

    # The form's own child collections, written in the same transaction.
    qualifications = QualificationSerializer(many=True, required=False, default=list)
    subjects = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )
    classes = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )

    def validate_staff_number(self, value):
        """Unique among this school's non-empty numbers.

        Reported on the field rather than as a code, so the form shows it where
        the reader has to change it. The format is the school's own, so nothing
        here checks its shape.
        """
        value = (value or "").strip()
        if not value:
            return value
        tenant = self.context["tenant"]
        if StaffProfile.objects.filter(tenant=tenant, staff_number=value).exists():
            raise serializers.ValidationError(
                "Somebody at this school already has that staff ID.",
            )
        return value


class BulkPostingSerializer(serializers.Serializer):
    staff_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, max_length=200,
    )
    #: Explicitly nullable: "across the whole school" is a choice a school makes
    #: rather than a field it forgot to fill in.
    branch = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class BulkRoleSerializer(serializers.Serializer):
    staff_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, max_length=200,
    )
    role = serializers.CharField(max_length=120)
    branch = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class StatusChangeSerializer(serializers.Serializer):
    to_status = serializers.ChoiceField(choices=EmploymentStatus.choices)
    effective_date = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    last_working_day = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class RevokeInvitationSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)


class EmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ClassTeacherSerializer(serializers.Serializer):
    school_class = serializers.IntegerField()
    #: Null clears the designation, which is a real thing a school does when
    #: somebody leaves and nobody has taken the class yet.
    staff = serializers.IntegerField(required=False, allow_null=True)
