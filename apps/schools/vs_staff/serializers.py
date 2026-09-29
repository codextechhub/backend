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

from vs_rbac.field_enforcement import FieldAccessMixin

from .constants import (
    DocumentType,
    EmploymentStatus,
    EmploymentType,
    LeaveType,
    OrgUnitKind,
    TeachingPart,
)
from .models import (
    LeaveRequest,
    StaffDocument,
    StaffEmploymentEvent,
    StaffMatrixReport,
    StaffOrgNode,
    StaffPosition,
    StaffPositionAssignment,
    StaffProfile,
    StaffQualification,
    TeachingAssignment,
)


def _full_name(user) -> str:
    if user is None:
        return ""
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip()


def _media_link(field, request):
    """A fetchable link to a stored file, or None when there is none.

    Signed for the reader and absolute to the API, through ``core.media``.
    ``FieldFile.url`` is the storage's bare ``/media/`` path, which the media
    view refuses without a signature and which a browser would resolve against
    the frontend's own origin besides.
    """
    from core.media import signed_url

    if not field:
        return None
    return signed_url(field.name, absolute_for=request) or None


def _actor(user):
    """An id and a display name, and never an email address.

    A history is the most widely read part of a profile, and the platform
    already applies this rule to ``created_by`` everywhere else.
    """
    if user is None:
        return None
    return {"id": user.pk, "name": _full_name(user)}


def _is_own_record(staff, user) -> bool:
    """Field Access owner rule: a person reads their own staff record whole.

    A school switches a field off per role to decide who reads it about other
    people. Switched off for the Teacher role, a teacher's own phone number
    would vanish from her own profile, which protects nobody.
    """
    return staff.user_id == user.pk


def _is_own_account(account, user) -> bool:
    """The same owner rule, for the account block nested in a staff record."""
    return account.pk == user.pk


class AccountStateSerializer(FieldAccessMixin, serializers.Serializer):
    """The account half, kept as its own object on purpose.

    The sign-in address is the staff member's registered ``email`` of
    ``school.teachers``, the same value the record carries at its top level, so
    this block enforces the same switch. It is nested as a serializer rather
    than returned from a method, because Field Access filters a nested
    serializer as its own surface and cannot see into what a method returns: a
    role with Read off would otherwise lose the top-level address and still be
    sent this one.

    ``can_hold_password`` is read from the model's own allow-list rather than
    recomputed, so a status added later cannot quietly acquire a meaning here
    that the auth layer does not give it.

    ``status`` is :attr:`User.account_state`, which is the stored status with a
    running lockout laid over it. A lockout is not a status and is not stored as
    one, but it is what a reader has to see, and it stops being shown the moment
    the window passes rather than when somebody clears a column.

    ``can_sign_in`` therefore asks both halves. The allow-list alone would say
    yes for an ACTIVE account that is locked out this minute, and a screen
    reading Locked beside "can sign in" is a screen nobody believes.
    """

    status = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    can_sign_in = serializers.SerializerMethodField()
    field_resource = "school.teachers"
    owner_rule = staticmethod(_is_own_account)

    can_hold_password = serializers.BooleanField(source="may_hold_password")
    email = serializers.EmailField(read_only=True)

    def get_status(self, obj) -> str:
        return obj.account_state

    def get_can_sign_in(self, obj) -> bool:
        return obj.may_sign_in and not obj.is_locked

    def get_label(self, obj) -> str:
        from vs_user.models import User

        return dict(User.Status.choices).get(obj.account_state, "")


#: The relations :class:`StaffListSerializer` reads off every row.
#:
#: Named once because two places build a queryset for this serializer - the
#: directory's base queryset and the branch roster - and the roster forgot the
#: invitation, which is one query per person on a screen that lists everybody
#: at a branch. A list of relations kept in two heads drifts; this one is kept
#: beside the serializer that needs it.
STAFF_LIST_PREFETCH = (
    "additional_postings",
    "user__tenant_role_assignments__role__additional_branches",
    "user__tenant_role_assignments__role",
    "user__invitation",
    # Whether a lockout is running, which the account status and the chip both
    # read. Without it every row on the page asks for itself.
    "user__lockout",
)


class StaffListSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One row of the directory.

    Carries the teaching load and an account flag, which are the two facts a row
    cannot be read without: a count of assignments says whether somebody teaches
    without anybody having to claim they are a teacher, and the flag is how
    somebody reads as employed and locked at once.

    A staff member's own date of birth, gender, phone number and email are the
    registered fields of ``school.teachers``, so a school decides per role who
    reads them, about everybody but themselves: a person's own row carries
    every one of their own details. A row of a list names no read-only fields;
    the record behind it does.
    """

    field_resource = "school.teachers"
    owner_rule = staticmethod(_is_own_record)
    field_composites = {
        "full_name": {"first_name": "user.first_name", "last_name": "user.last_name"},
    }

    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="user.email", read_only=True)
    #: The stored status with a running lockout laid over it, so the column, the
    #: chip beside it and the ``?account_status=`` filter name the same people.
    account_status = serializers.CharField(source="user.account_state", read_only=True)
    account_flag = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    posting_branch_ids = serializers.SerializerMethodField()
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
    #: Whether the invitation email actually went out, which ``invited_at``
    #: cannot say on its own.
    #:
    #: A person imported with Send Invitation set to No has a real invitation
    #: sitting unsent, and reads identically to an invited one on every other
    #: field here. This is what the screen that offers to invite people later
    #: reads to find them.
    invitation_email_status = serializers.SerializerMethodField()
    #: Whether the viewer may change this record rather than only read it. False
    #: for a branch administrator looking at somebody school-wide or posted to a
    #: branch they do not cover; the server refuses those writes whatever the
    #: screen draws.
    can_manage = serializers.SerializerMethodField()

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
            "branch_name", "posting_branch_ids", "posted_school_wide", "teaching_load",
            "on_leave_today", "on_leave_until", "hire_date", "can_resend",
            "invited_at", "invitation_email_status", "can_manage",
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

    def _branch_dimension(self) -> bool:
        """Whether this render names branches. On a row, per viewer: see ``multi_branch``."""
        return bool(self.context.get("multi_branch"))

    def get_branch_name(self, obj):
        if not self._branch_dimension():
            return None
        names = ([obj.branch.name] if obj.branch_id else []) + [
            branch.name for branch in obj.additional_postings.all()
        ]
        return ", ".join(names) if names else "School-wide"

    def get_can_manage(self, obj) -> bool:
        from .services.scoping import caller_manages

        request = self.context.get("request")
        if request is None:
            return False
        if "viewer_branches" in self.context:
            return caller_manages(
                request.user, None, obj,
                visible=self.context["viewer_branches"], resolved=True,
            )
        return caller_manages(request.user, self.context.get("tenant"), obj)

    def get_posting_branch_ids(self, obj):
        return obj.posting_branch_ids

    def get_posted_school_wide(self, obj):
        if not self._branch_dimension():
            return None
        return not obj.posting_branch_ids

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

    def get_invitation_email_status(self, obj):
        """Read off the prefetched invitation, so a page costs no extra query.

        ``STAFF_LIST_PREFETCH`` already carries ``user__invitation`` for the
        resend flag beside this one. Null means there is no invitation row at
        all, which is a DRAFT rather than an unsent invitation.

        The platform user list has the same field gated behind
        ``platform.team.view``. That gate is not reused: no school role holds a
        platform key, and a school reading its own staff should not need one.
        """
        invitation = getattr(obj.user, "invitation", None)
        return getattr(invitation, "email_status", None)

    def get_account_flag(self, obj):
        """A chip only where the account disagrees with the employment record.

        Silent in the ordinary case, so the one row that needs reading stands
        out instead of being lost among fifty that say Active twice.
        """
        from vs_user.models import User

        # The lockout is read from its own row, which expires by itself. A chip
        # drawn from the status column stayed up all week.
        status = obj.user.account_state
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
        from vs_config.clock import tenant_today

        tenant = self.context.get("tenant") or getattr(self.context.get("request"), "tenant", None)
        this_year = tenant_today(tenant).year
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
    file_retired = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = StaffDocument
        fields = [
            "id", "document_type", "document_type_label", "title", "file_url",
            "file_retired", "uploaded_by", "created_at",
        ]

    def get_file_url(self, obj):
        if obj.pk in self.context.get("retired_document_ids", ()):
            return None
        return _media_link(obj.file, self.context.get("request"))

    def get_file_retired(self, obj) -> bool:
        """Held on the day asked about and replaced since, so its file is gone."""
        return obj.pk in self.context.get("retired_document_ids", ())

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
            "start_date", "end_date", "days", "over_allowance_by", "note", "status",
            "display_status", "decided_at", "requested_by", "created_at",
        ]

    def get_display_status(self, obj) -> str:
        as_at = self.context.get("as_at")
        return obj.display_status(as_at.date if as_at else None)

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
    """One person's record, with the account beside it rather than inside it.

    A detail response, so it names the registered fields the caller may read
    and not change, for the edit drawer to grey.

    Where the person works is part of their contact card, so at a school with
    more than one branch the record always names it (``branch_name``,
    ``posted_school_wide`` and ``posting_branches``), whatever the reader's own
    reach. The directory row recedes it for a reader who works in one branch;
    a card opened from the chart is how that reader finds somebody at another
    branch, and there the branch is the point. At a one-branch school all three
    are null, as on the row.

    Cut down to what the reader's standing to the person allows
    (:func:`.services.visibility.shape_record`), after Field Access has removed
    what the reader's role may not read. The view passes the reader's
    ``profile_access`` in the context; any other caller (a write's response, a
    lifecycle action) has it worked out here from the request. ``counts`` asks
    only for the groups the reader may open, so a restricted read does not pay
    for counts it will not see.
    """

    field_access_detail = True

    account = AccountStateSerializer(source="user", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    middle_name = serializers.CharField(read_only=True)
    date_of_birth = serializers.DateField(read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    gender = serializers.CharField(source="user.gender", read_only=True)
    photo_url = serializers.SerializerMethodField()
    #: Every branch they are posted to, as ``{id, name}``, main posting first.
    posting_branches = serializers.SerializerMethodField()
    tenure = serializers.SerializerMethodField()
    lifecycle = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    #: Their primary post, its unit, their line manager and whether they are
    #: acting. On the record only, never on a directory row: it costs two or
    #: three queries, and a page of twenty-five would pay that twenty-five times.
    organogram = serializers.SerializerMethodField()
    #: The document types the school expects (Settings, Staff) that this
    #: record holds none of, as ``{type, label}``. A flag, never a gate: nothing
    #: is refused for a missing document. Read with the records group.
    missing_documents = serializers.SerializerMethodField()

    class Meta(StaffListSerializer.Meta):
        fields = StaffListSerializer.Meta.fields + [
            "account", "first_name", "middle_name", "last_name",
            "date_of_birth", "phone", "gender",
            "photo_url", "posting_branches", "exit_date", "tenure", "lifecycle", "counts",
            "created_by", "organogram", "missing_documents",
        ]

    def _tenant(self):
        return self.context.get("tenant") or getattr(
            self.context.get("request"), "tenant", None,
        )

    def get_missing_documents(self, obj):
        """Expected types with no document of that type on the record.

        Null for a record read as at an earlier day: the rule is today's, and
        measuring a past record against it answers a question nobody asked.
        """
        if self.context.get("as_at") is not None:
            return None
        required = self.context.get("_required_documents")
        if required is None:
            from .services.rules import required_documents

            required = required_documents(self._tenant())
            self.context["_required_documents"] = required
        if not required:
            return []
        held = set(obj.documents.values_list("document_type", flat=True))
        labels = dict(DocumentType.choices)
        return [
            {"type": code, "label": labels.get(code, code)}
            for code in required if code not in held
        ]

    def get_organogram(self, obj):
        """Where they sit on the chart, as at the day asked about where one was.

        None for somebody with no primary post, which is an ordinary answer: a
        school that has not drawn its chart has nobody on it.
        """
        from .services.organogram import primary_line_for

        as_at = self.context.get("as_at")
        line = primary_line_for(obj, on=as_at.date if as_at else None)
        if line is None:
            return None
        return {
            "position": position_inline(line["position"]),
            "org_node": org_node_inline(line["org_node"]),
            "line_manager": (
                staff_holder(line["line_manager"], self.context)
                if line["line_manager"] is not None else None
            ),
            "is_acting": line["is_acting"],
        }

    def get_photo_url(self, obj):
        return _media_link(obj.photo, self.context.get("request"))

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
        from vs_config.clock import branch_today

        as_at = self.context.get("as_at")
        today = as_at.date if as_at else branch_today(obj.tenant, obj.branch_id)
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

        Invited then Active is the whole of the ordinary path, preceded by
        Awaiting approval for a hire the school has not approved yet, or by
        Invited at go-live for somebody imported while it was set up. The other
        statuses are not later stages of it and must not be drawn as though they
        were: a strip that showed Terminated as step three would say a school
        expects everybody to get there.
        """
        path = [EmploymentStatus.INVITED, EmploymentStatus.ACTIVE]
        if obj.employment_status in (
            EmploymentStatus.PENDING_APPROVAL, EmploymentStatus.AWAITING_GO_LIVE,
        ):
            path = [obj.employment_status, *path]
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

    def _branch_dimension(self) -> bool:
        """Whether the school runs more than one branch, asked once per request."""
        cached = self.context.get("_school_has_branches")
        if cached is None:
            from .services.scoping import branch_dimension_applies

            tenant = self.context.get("tenant") or getattr(
                self.context.get("request"), "tenant", None,
            )
            cached = tenant is not None and branch_dimension_applies(tenant)
            self.context["_school_has_branches"] = cached
        return cached

    def get_posting_branches(self, obj):
        if not self._branch_dimension():
            return None
        branches = ([obj.branch] if obj.branch_id else []) + list(
            obj.additional_postings.all(),
        )
        return [branch_ref(branch) for branch in branches]

    def _profile_access(self, obj):
        """The reader's standing to *obj*, or None for a render acting for nobody."""
        from vs_rbac.field_enforcement import SYSTEM_CONTEXT_KEY

        from .services import visibility

        access = self.context.get("profile_access")
        if access is not None:
            return access
        request = self.context.get("request")
        if request is None or self.context.get(SYSTEM_CONTEXT_KEY):
            return None
        tenant = self.context.get("tenant") or getattr(request, "tenant", None)
        options = {}
        if "viewer_branches" in self.context:
            options["visible"] = self.context["viewer_branches"]
        return visibility.profile_access(request, obj, tenant, **options)

    def get_counts(self, obj):
        from .services.visibility import ALL_GROUPS, COUNT_GROUPS

        access = self._profile_access(obj)
        groups = ALL_GROUPS if access is None else access.groups
        counters = {
            "qualifications": obj.qualifications,
            "documents": obj.documents,
            "teaching_assignments": obj.teaching_assignments,
            "leave_requests": obj.leave_requests,
        }
        return {
            name: related.count()
            for name, related in counters.items()
            if COUNT_GROUPS[name] in groups
        }

    def to_representation(self, instance):
        """The shaped record, plus ``self_editable_fields`` on the reader's own.

        The school's list of what staff may change about themselves, so the
        edit drawer opens those boxes and no others without keeping its own
        copy. Absent on anybody else's record.
        """
        from .services.visibility import shape_record

        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and instance.user_id == getattr(user, "pk", None):
            from .services.rules import self_editable_options, self_editable_fields

            allowed = self_editable_fields(self._tenant())
            data["self_editable_fields"] = [
                option["value"] for option in self_editable_options()
                if option["value"] in allowed
            ]
        return shape_record(data, self._profile_access(instance))


class StaffUpdateSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """What an administrator may change on a record.

    ``employment_status`` is absent: it moves only through the lifecycle
    service, which is the only place that also does the right thing to the
    account. The email is absent too, because changing an account's address is a
    different key on a different endpoint.

    The personal details it does carry are registered fields, so a role whose
    Write switch does not reach one is refused with 403 rather than quietly
    saving it.
    """

    field_resource = "school.teachers"

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

    def _entry_for(self, name, entries):
        """A person's own self-editable details are theirs to write.

        The owner rule of the read serializers, narrowed to the fields the
        school lets staff edit about themselves: a teacher whose role has the
        phone switch off may still correct her own number, and gains nothing
        else by being the subject. Which fields a person may send about
        themselves at all is the view's rule, not this one.
        """
        if name in self._self_editable() and self._edits_own_record():
            return None
        return super()._entry_for(name, entries)

    def _self_editable(self) -> frozenset:
        cached = self.context.get("_self_editable")
        if cached is None:
            from .services.rules import self_editable_fields

            tenant = self.context.get("tenant") or getattr(
                self.context.get("request"), "tenant", None,
            )
            cached = self_editable_fields(tenant)
            self.context["_self_editable"] = cached
        return cached

    def _edits_own_record(self) -> bool:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return (
            self.instance is not None
            and user is not None
            and getattr(user, "is_authenticated", False)
            and self.instance.user_id == user.pk
        )


class StaffCreateSerializer(FieldAccessMixin, serializers.Serializer):
    """The Add screen, in one payload.

    Wraps ``UserCreateSerializer``'s fields rather than replacing them: the
    account half is validated by the platform's own serializer inside the view,
    and what is declared here is the staff half plus the qualifications and
    teaching duties the form carries. There is no documents field: a file is
    uploaded to the record once it exists.

    The address a new account signs in with is declared open on create, so a
    role that may add a member of staff can still send it; every other
    registered detail here is governed by its Write switch on this path as on
    the edit form.
    """

    field_resource = "school.teachers"

    # Account half, passed through to UserCreateSerializer.
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    gender = serializers.CharField(required=False, allow_blank=True, default="")
    #: Onboarding only: School Admin or Branch Admin. At a live school the
    #: server grants the starting role itself and refuses any other.
    role = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    #: Onboarding only: how far the grant reaches, where that is not the
    #: posting. A branch reference pins it there, the word "school" asks for the
    #: whole school deliberately, and leaving it out follows the posting. At a
    #: live school the grant always follows the posting.
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

    # Qualifications and teaching duties, written in the same transaction.
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
        from .services.numbers import staff_number_taken

        if staff_number_taken(self.context["tenant"], value):
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
    branch_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False,
    )
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
    #: Why the address changed, in the administrator's words. Optional.
    note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200,
    )


class ClassTeacherSerializer(serializers.Serializer):
    school_class = serializers.IntegerField()
    #: Null clears the designation, which is a real thing a school does when
    #: somebody leaves and nobody has taken the class yet.
    staff = serializers.IntegerField(required=False, allow_null=True)


# =============================================================================
# A school's own staff rules
# =============================================================================


class StaffNumberPolicySerializer(serializers.Serializer):
    """The school's staff-number rule, or one branch's.

    ``auto_issue`` may be left out, which keeps the value the school or branch
    reads today, so a client that does not show it cannot switch it off by
    saving.
    """

    required = serializers.BooleanField(error_messages={
        "required": "Say whether every member of staff needs a staff number.",
        "invalid": "Say whether every member of staff needs a staff number.",
    })
    pattern = serializers.CharField(
        allow_blank=True, max_length=200, trim_whitespace=False,
        error_messages={
            "required": "Send the pattern, or an empty one for any shape.",
            "max_length": "Keep the pattern under 200 characters.",
        },
    )
    hint = serializers.CharField(
        allow_blank=True, max_length=200,
        error_messages={
            "required": "Send the hint, or an empty one for none.",
            "max_length": "Keep the hint under 200 characters.",
        },
    )
    auto_issue = serializers.BooleanField(required=False, error_messages={
        "invalid": "Say whether staff numbers are issued automatically.",
    })
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_pattern(self, value):
        from .services.number_policy import compile_pattern

        try:
            compile_pattern(value)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(exc.detail["pattern"]) from exc
        return value


class LeaveRulesSerializer(serializers.Serializer):
    """How a school counts and limits leave, as the settings screen saves it."""

    allowances = serializers.DictField(
        child=serializers.JSONField(allow_null=True), allow_empty=True,
        error_messages={
            "required": "Send the allowances, or an empty set for no limits.",
            "not_a_dict": "Give the allowances as leave types and days.",
        },
    )
    working_days = serializers.ListField(
        child=serializers.JSONField(allow_null=True), allow_empty=True,
        error_messages={
            "required": "Say which days of the week count for leave.",
            "not_a_list": "Give the working days as a list of weekdays.",
        },
    )
    exclude_closures = serializers.BooleanField(error_messages={
        "required": "Say whether days the school is closed count for leave.",
        "invalid": "Say whether days the school is closed count for leave.",
    })

    def validate_allowances(self, value):
        from .constants import LEAVE_ALLOWANCE_MAX

        labels = dict(LeaveType.choices)
        cleaned = {}
        for code, days in value.items():
            if code not in labels:
                raise serializers.ValidationError(
                    f"'{code}' is not a leave type this school records.",
                )
            if days is None:
                cleaned[code] = None
                continue
            if (
                isinstance(days, bool) or not isinstance(days, int)
                or not 0 <= days <= LEAVE_ALLOWANCE_MAX
            ):
                raise serializers.ValidationError(
                    f"Give {labels[code].lower()} leave as a whole number of days "
                    f"from 0 to {LEAVE_ALLOWANCE_MAX}, or leave it empty for no limit.",
                )
            cleaned[code] = days
        return cleaned

    def validate_working_days(self, value):
        if not value:
            raise serializers.ValidationError(
                "Choose at least one day of the week that counts for leave.",
            )
        for day in value:
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7:
                raise serializers.ValidationError(
                    "Give the working days as weekdays numbered 1 (Monday) to 7 "
                    "(Sunday).",
                )
        return sorted(set(value))


class StaffRulesSerializer(serializers.Serializer):
    """The full set of a school's staff rules, as the settings screen saves it.

    Every rule is sent every time, because the screen shows every rule; a
    partial save would leave a rule the admin could see set to something they
    did not choose. Each refusal is keyed on its own field and written as a
    sentence. ``context`` carries the ``tenant`` and the ``request``, because
    the starting role is checked against the school's roles and against what
    the person saving may grant.
    """

    starting_role = serializers.CharField(max_length=120, error_messages={
        "required": "Choose the role new staff start with.",
        "blank": "Choose the role new staff start with.",
    })
    required_documents = serializers.ListField(
        child=serializers.CharField(), allow_empty=True,
        error_messages={
            "required": "Send the required documents, or an empty list for none.",
            "not_a_list": "Give the required documents as a list.",
        },
    )
    self_editable_fields = serializers.ListField(
        child=serializers.CharField(), allow_empty=True,
        error_messages={
            "required": "Send the fields staff may edit themselves, or an empty list for none.",
            "not_a_list": "Give the fields staff may edit themselves as a list.",
        },
    )
    hire_requires_approval = serializers.BooleanField(error_messages={
        "required": "Say whether new staff wait for approval before they are invited.",
        "invalid": "Say whether new staff wait for approval before they are invited.",
    })
    leave = LeaveRulesSerializer(error_messages={
        "required": "Send the leave rules.",
    })
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_starting_role(self, value):
        """An active role of this school that the person saving may hand out.

        The restricted check applies only when the role changes: saving the
        screen with the role it already has must not be refused to a settings
        administrator who holds none of that role's restricted keys.
        """
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.services import grant_needs_approval

        from .services.rules import starting_role_key

        tenant = self.context["tenant"]
        role = TenantRoleTemplate.objects.filter(
            tenant=tenant, status="ACTIVE", key=value.strip(),
        ).first()
        if role is None:
            raise serializers.ValidationError(
                "Choose one of this school's active roles for new staff to start with.",
            )
        request = self.context.get("request")
        if (
            role.key != starting_role_key(tenant)
            and request is not None
            and grant_needs_approval(request.user, role)
        ):
            raise serializers.ValidationError(
                f"{role.name} carries restricted permissions you do not hold, so "
                f"it cannot be the role every new member of staff starts with. "
                f"Choose another, or ask an administrator who holds them.",
            )
        return role.key

    def validate_required_documents(self, value):
        known = set(DocumentType.values)
        for code in value:
            if code not in known:
                raise serializers.ValidationError(
                    f"'{code}' is not a document type this school can expect.",
                )
        return value

    def validate_self_editable_fields(self, value):
        from .services.rules import self_editable_locked, self_editable_options

        allowed = {option["value"] for option in self_editable_options()}
        locked = {option["value"]: option["label"] for option in self_editable_locked()}
        for name in value:
            if name in locked:
                raise serializers.ValidationError(
                    f"{locked[name]} is the school's to set, so staff can never "
                    f"change it about themselves.",
                )
            if name not in allowed:
                raise serializers.ValidationError(
                    f"'{name}' is not a detail of a staff record that staff could "
                    f"change about themselves.",
                )
        return value


# =============================================================================
# The organogram
# =============================================================================
#
# Every payload here is read by every member of staff, so a person on the chart
# is a StaffHolder and nothing more: a name, a photograph, a job title and two
# ids. No email address, no phone number, no pay and no leave, on any of them.
# The records behind the chart stay behind the directory's own keys.
#
# Units, posts, appointments and dotted lines each carry ``can_manage``, so a
# screen can hide the controls a branch administrator would be refused.


def branch_ref(branch):
    """A branch as ``{id, name}``, or None for school-wide."""
    if branch is None:
        return None
    return {"id": branch.pk, "name": branch.name}


def _photos_readable(context) -> bool:
    """Whether this viewer may see staff photographs, asked once per request.

    The photograph is a registered field of ``school.teachers``, so a school
    that switched it off for a role has it switched off on the chart as well.
    The name and job title are not asked about: a chart without them is not a
    chart, and every member of staff reading the whole school's chart is the
    point of it.
    """
    cached = context.get("_photos_readable")
    if cached is None:
        from vs_rbac.field_enforcement import can_read

        cached = can_read(context.get("request"), "school.teachers.photo")
        context["_photos_readable"] = cached
    return cached


def staff_holder(staff, context) -> dict:
    """One person as the chart draws them.

    ``id`` is the account's id, because that is what the rest of the platform
    links a person by; ``staff_id`` is the staff record, for opening it. The
    photograph is a signed URL bound to the viewer, as every media link is.
    ``is_suspended`` is true while their employment or their account is
    suspended: they keep their post on the chart and the chart says so.
    """
    from core.media import signed_url
    from vs_user.models import User

    user = staff.user
    request = context.get("request")
    photo = None
    if staff.photo and _photos_readable(context):
        photo = signed_url(staff.photo.name, absolute_for=request) or None
    return {
        "id": str(user.pk),
        "staff_id": staff.pk,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": _full_name(user),
        "photo": photo,
        "job_title": staff.job_title,
        "is_suspended": (
            staff.employment_status == EmploymentStatus.SUSPENDED
            or user.status == User.Status.SUSPENDED
        ),
    }


def org_node_inline(node):
    if node is None:
        return None
    return {"id": node.pk, "name": node.name, "code": node.code, "kind": node.kind}


def position_inline(position):
    if position is None:
        return None
    return {
        "id": position.pk,
        "title": position.title,
        "code": position.code,
        "org_node": org_node_inline(position.org_node) if position.org_node_id else None,
    }


class _Managed:
    """Adds ``can_manage`` to a row, from the branch the row belongs to."""

    def to_representation(self, instance):
        from schools.vs_academics.services.scoping import add_manage_flag

        return add_manage_flag(self, instance, super().to_representation(instance))


class StaffOrgNodeSerializer(_Managed, serializers.ModelSerializer):
    """One unit, with its parent, its head post and whoever holds that post."""

    branch = serializers.SerializerMethodField()
    parent = serializers.SerializerMethodField()
    head_position = serializers.SerializerMethodField()
    head = serializers.SerializerMethodField()
    children_count = serializers.SerializerMethodField()

    class Meta:
        model = StaffOrgNode
        fields = [
            "id", "name", "code", "kind", "branch", "parent", "head_position",
            "head", "description", "is_active", "children_count",
            "created_at", "updated_at",
        ]

    def get_branch(self, obj):
        return branch_ref(obj.branch)

    def get_parent(self, obj):
        return org_node_inline(obj.parent)

    def get_head_position(self, obj):
        return position_inline(obj.head_position)

    def get_head(self, obj):
        from .services.organogram import holders_of

        if obj.head_position_id is None:
            return None
        holders = holders_of(obj.head_position)
        return staff_holder(holders[0], self.context) if holders else None

    def get_children_count(self, obj) -> int:
        annotated = getattr(obj, "children_total", None)
        return annotated if annotated is not None else obj.children.count()


class StaffPositionSerializer(_Managed, serializers.ModelSerializer):
    """One post, its unit and branch, its line upward, and who is in it."""

    org_node = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()
    reports_to = serializers.SerializerMethodField()
    current_holders = serializers.SerializerMethodField()
    is_vacant = serializers.SerializerMethodField()
    open_seats = serializers.SerializerMethodField()

    class Meta:
        model = StaffPosition
        fields = [
            "id", "title", "code", "org_node", "branch", "reports_to",
            "headcount", "is_active", "current_holders", "is_vacant",
            "open_seats", "created_at", "updated_at",
        ]

    def _holders(self, obj):
        from .services.organogram import holders_of

        cached = getattr(obj, "_holders_cache", None)
        if cached is None:
            cached = holders_of(obj)
            obj._holders_cache = cached
        return cached

    def get_org_node(self, obj):
        return org_node_inline(obj.org_node)

    def get_branch(self, obj):
        return branch_ref(obj.org_node.branch)

    def get_reports_to(self, obj):
        return position_inline(obj.reports_to)

    def get_current_holders(self, obj):
        return [staff_holder(staff, self.context) for staff in self._holders(obj)]

    def get_is_vacant(self, obj) -> bool:
        return not self._holders(obj)

    def get_open_seats(self, obj) -> int:
        return max(obj.headcount - len(self._holders(obj)), 0)


class StaffPositionAssignmentSerializer(_Managed, serializers.ModelSerializer):
    """One appointment, dated. The history, which only the register's keys read."""

    staff = serializers.SerializerMethodField()
    position = serializers.SerializerMethodField()
    is_current = serializers.BooleanField(read_only=True)

    class Meta:
        model = StaffPositionAssignment
        fields = [
            "id", "staff", "position", "is_primary", "is_acting", "start_date",
            "end_date", "is_current", "created_at", "updated_at",
        ]

    def get_staff(self, obj):
        return staff_holder(obj.staff, self.context)

    def get_position(self, obj):
        return position_inline(obj.position)

    def to_representation(self, instance):
        """``can_manage`` needs both halves: the post's branch and the person.

        Appointing is refused unless the caller may change the post and manage
        the person, so a row that shows the controls for one and not the other
        would offer an end date the server then refuses.
        """
        data = super().to_representation(instance)
        if data.get("can_manage"):
            from .services.scoping import caller_manages

            request = self.context.get("request")
            data["can_manage"] = caller_manages(
                request.user, getattr(request, "tenant", None), instance.staff,
            )
        return data


class StaffMatrixReportSerializer(_Managed, serializers.ModelSerializer):
    """A dotted line between two posts."""

    position = serializers.SerializerMethodField()
    reports_to = serializers.SerializerMethodField()

    class Meta:
        model = StaffMatrixReport
        fields = [
            "id", "position", "reports_to", "relationship_label",
            "created_at", "updated_at",
        ]

    def get_position(self, obj):
        return position_inline(obj.position)

    def get_reports_to(self, obj):
        return position_inline(obj.reports_to)


def tree_node(node, context) -> dict:
    """One node of :meth:`StaffOrganogramService.build_tree`, and all below it."""
    return {
        "id": node["id"],
        "title": node["title"],
        "code": node["code"],
        "org_node": org_node_inline(node["org_node"]),
        "branch": branch_ref(node["branch"]),
        "holders": [staff_holder(staff, context) for staff in node["holders"]],
        "is_vacant": node["is_vacant"],
        "direct_reports": [tree_node(child, context) for child in node["direct_reports"]],
    }


def current_assignment(assignment, context) -> dict:
    """The chart's view of an appointment: who, which post, and whether acting.

    No dates and no history. Who covered a post last spring is the register's
    business, not every colleague's.
    """
    return {
        "staff": staff_holder(assignment.staff, context),
        "position": position_inline(assignment.position),
        "is_acting": assignment.is_acting,
    }


# ── What a form may send ──────────────────────────────────────────────────


class _TenantScoped(serializers.Serializer):
    """Resolves every ``*_id`` field inside the caller's school.

    An id from another school is reported exactly like an id that does not
    exist, so a form field cannot be used to learn what another school has.
    Built without a tenant in its context, every id names nothing.
    """

    scoped_fields: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = self.context.get("tenant")
        if tenant is None:
            return
        for name, model in self.scoped_fields.items():
            if name in self.fields:
                self.fields[name].queryset = model.all_objects.filter(tenant=tenant)


class OrgNodeWriteSerializer(_TenantScoped):
    scoped_fields = {"parent_id": StaffOrgNode, "head_position_id": StaffPosition}

    name = serializers.CharField(max_length=150)
    #: Sent without its tier prefix or with it; stored with it.
    code = serializers.CharField(max_length=37)
    kind = serializers.ChoiceField(choices=OrgUnitKind.choices)
    parent_id = serializers.PrimaryKeyRelatedField(
        source="parent", queryset=StaffOrgNode.all_objects.none(),
        required=False, allow_null=True,
    )
    head_position_id = serializers.PrimaryKeyRelatedField(
        source="head_position", queryset=StaffPosition.all_objects.none(),
        required=False, allow_null=True,
    )
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    #: Absent, null and an id are three different answers: see the view.
    branch_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class PositionWriteSerializer(_TenantScoped):
    scoped_fields = {"org_node_id": StaffOrgNode, "reports_to_id": StaffPosition}

    title = serializers.CharField(max_length=150)
    code = serializers.CharField(max_length=40)
    org_node_id = serializers.PrimaryKeyRelatedField(
        source="org_node", queryset=StaffOrgNode.all_objects.none(),
    )
    reports_to_id = serializers.PrimaryKeyRelatedField(
        source="reports_to", queryset=StaffPosition.all_objects.none(),
        required=False, allow_null=True,
    )
    headcount = serializers.IntegerField(required=False, min_value=1, max_value=500)
    is_active = serializers.BooleanField(required=False)


class AppointmentWriteSerializer(_TenantScoped):
    scoped_fields = {"position_id": StaffPosition}

    staff_id = serializers.IntegerField(min_value=1)
    position_id = serializers.PrimaryKeyRelatedField(
        source="position", queryset=StaffPosition.all_objects.none(),
    )
    is_primary = serializers.BooleanField(required=False, default=True)
    is_acting = serializers.BooleanField(required=False, default=False)
    start_date = serializers.DateField(required=False, allow_null=True, default=None)


class AppointmentCloseSerializer(serializers.Serializer):
    end_date = serializers.DateField(required=False, allow_null=True, default=None)


class MatrixReportWriteSerializer(_TenantScoped):
    scoped_fields = {"position_id": StaffPosition, "reports_to_id": StaffPosition}

    position_id = serializers.PrimaryKeyRelatedField(
        source="position", queryset=StaffPosition.all_objects.none(),
    )
    reports_to_id = serializers.PrimaryKeyRelatedField(
        source="reports_to", queryset=StaffPosition.all_objects.none(),
    )
    relationship_label = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default="",
    )
