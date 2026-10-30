"""Staff personal details an administrator may restrict per role.

``school.teachers`` is the staff register: the directory row, the record behind
it, and the two endpoints that create and correct it. The qualifications,
certificates and documents sit under ``school.staff_records`` instead, sold at
a different depth, so the personal details belong here and not there.

None of these is sensitive. Everybody who may open a staff record reads the
date of birth, the phone number, the email address and the gender today, and
declaring them sensitive would close them to every role on the day the switches
start being enforced. They are declared open, and a school turns them off for
the roles it chooses.

Where the value lives is not where the client meets it. ``date_of_birth`` is a
column on the staff record; ``phone``, ``email`` and ``gender`` are read from
the linked account (``user.phone``, ``user.email``, ``user.gender``). A switch
covers the name a client receives, which is the same in both cases.

The name, the employment details and the photograph are open as well, and
for the same reason: a school turns each off for the roles it chooses. The
first and last name live on the account (``user.first_name``), are required by
the Add form and so are open on create; ``full_name`` is rebuilt from the name
parts a caller may read. The exit date is written only by the status change
that ends employment, so it has a Read switch and nothing can write it here.
The photograph is sent as ``photo`` and read back as ``photo_url``.

The employment status has one switch for every form it takes (see
:data:`EMPLOYMENT_STATUS_NAMES`). It moves only through the lifecycle endpoint,
behind ``school.teachers.transition``, never through the record's edit form,
and that endpoint asks the Write switch as well, as the email change endpoint
asks the email's: a role holding the key with the status read-only is refused.
A reader who may not read it gets no status column, chip or filter on the
directory, no status breakdown in its header, and no employment half on a
person's history.

The email is the one field the Add form requires and the edit form does not
carry at all: an account's sign-in address changes on an endpoint of its own,
behind its own key. It is therefore declared open on create, so a role that may
add a staff member can still send the address every new account needs, while
the write switch keeps deciding nothing about it afterwards. The date of birth,
gender and phone number are optional on the Add form and present on the edit
form, so each is governed by its switch on both paths.

Leave
-----

``school.leave`` is a staff member's leave requests, declared in
:func:`register_leave`. Its switches follow what a person decides about a
request rather than the columns of the table: the **type of leave**, the
**dates**, the **days** counted, the **note**, the **status**, and the
**balance**. All are open by default, because everybody who may open a request
reads every one of them today.

* The dates are one switch for the start, the end and the day the person
  returns, which is worked out from the end date and is never written by a
  caller. The resumption flag that says the day is an estimate follows it.
* The days counted are their own switch, so a role can see when somebody is
  away without being told for how many working days.
* The note is the person's own free text, the most private thing on a request.
* The status is one fact under every name it takes: the stored value, what it
  reads as once the dates have passed, when it was decided, and where the request
  sits in the approval ladder (who it waits with exists only while it is
  pending). It is written only by the workflow callbacks that approve, reject
  and cancel, so it has a Read switch and nothing can write it.
* The balance is one switch for every figure that compares the leave taken with
  the allowance: the per-type sheet, the days taken per type, the figures a
  preview shows while dates are picked, the days over the allowance on a request
  and the session those figures count. They are a single decision, whether a
  role sees where somebody stands against their entitlement, and splitting them
  would offer a role the remaining days without the allowance they are
  remaining from. It is read-only: it is counted from the requests.

A breakdown by type is the leave type again, so the per-type balance sheet and
the days taken per type need the **balance and the type** to be readable. A
role that cannot read the type would otherwise read "Sick: 4 days taken" and
learn the type of requests it was told nothing about.

Owner rule: a person always reads their own leave in full, every switch
included, the way a person always reads their own payroll bank details whatever
their roles say. "Own" means the request's member of staff is the requesting
user (``staff.user_id``), not that the user filed it: an administrator who
files a request on Tunde's behalf is not its owner and reads it through their
roles. The rule is applied on every read path (the list and the echo of a
write, the as-at read, the balance page, the preview, the warnings a write
answers with, the section timeline, the approval card, the directory's "on
leave until" and the registers' mention of the leave), and a record's
``_read_only_fields`` names only what nobody can write, the status and the
balance, so a form shows the owner everything. It is a rule about reading and
about the Write switches of the fields a person supplies: the owner files and
corrects their own request whatever their role's Write switch says for the
type, the dates, the days and the note, while a read-only field (the status and
the balance) is still refused when sent.

The facts of a request that are not switched are deliberate. The person's name
and the request's id are labels. Who filed the request, when, and who last
changed it are the record's authorship, which every approver of the request is
shown in the approval trail, and carry no switch.
"""
from __future__ import annotations

from dataclasses import dataclass

from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"

#: The exit date field, under its full registry key.
EXIT_DATE_FIELD = "school.teachers.exit_date"

#: The employment status switch, under its full registry key.
EMPLOYMENT_STATUS_FIELD = "school.teachers.employment_status"

#: Every name the employment status reaches a client under. The stored value,
#: its label, what a row reads as (On Leave laid over Active), whether the
#: person is still on the roll or has left, and the lifecycle strip on a
#: record are all the same fact, so one switch covers them all. The employment
#: events on a person's history are status changes from end to end, so a
#: reader without the switch gets none of them (``StaffHistoryView``).
EMPLOYMENT_STATUS_NAMES = (
    "employment_status", "employment_status_label",
    "display_employment_status", "display_employment_status_label",
    "on_roll", "is_exited", "lifecycle",
)


#: The resource the leave switches are declared under.
LEAVE_RESOURCE = "school.leave"

#: The leave switches, under their full registry keys.
LEAVE_TYPE_FIELD = f"{LEAVE_RESOURCE}.leave_type"
LEAVE_DATES_FIELD = f"{LEAVE_RESOURCE}.dates"
LEAVE_DAYS_FIELD = f"{LEAVE_RESOURCE}.days"
LEAVE_NOTE_FIELD = f"{LEAVE_RESOURCE}.note"
LEAVE_STATUS_FIELD = f"{LEAVE_RESOURCE}.status"
LEAVE_BALANCE_FIELD = f"{LEAVE_RESOURCE}.balance"

#: Every name each leave switch reaches a client under.
LEAVE_TYPE_NAMES = ("leave_type", "leave_type_label")
LEAVE_DATE_NAMES = ("start_date", "end_date", "resumption_date", "resumption_is_estimate")
LEAVE_STATUS_NAMES = ("status", "display_status", "decided_at", "approval")
LEAVE_BALANCE_NAMES = (
    "balances", "days_taken", "balance_session", "balance_note", "session",
    "allowance", "taken", "pending", "remaining", "remaining_after",
    "over_allowance_by",
)


@dataclass(frozen=True)
class LeaveReach:
    """What the caller of a leave response may read, asked once and carried.

    A warning, a figure or a sentence built outside a serializer is cut to this
    before it leaves. The default reads everything, which is what a caller that
    acts for nobody (a command, a fixture) gets. ``per_type_balance`` is the
    balance and the type together: a figure broken down by type says which types
    the person took.
    """

    leave_type: bool = True
    dates: bool = True
    days: bool = True
    note: bool = True
    status: bool = True
    balance: bool = True

    @classmethod
    def of(cls, request, *, owner: bool = False) -> "LeaveReach":
        """The reach of *request*'s caller; everything for no request or for the owner."""
        from vs_rbac.field_enforcement import can_read

        if owner:
            return cls()
        return cls(
            leave_type=can_read(request, LEAVE_TYPE_FIELD),
            dates=can_read(request, LEAVE_DATES_FIELD),
            days=can_read(request, LEAVE_DAYS_FIELD),
            note=can_read(request, LEAVE_NOTE_FIELD),
            status=can_read(request, LEAVE_STATUS_FIELD),
            balance=can_read(request, LEAVE_BALANCE_FIELD),
        )

    @property
    def per_type_balance(self) -> bool:
        return self.balance and self.leave_type


def is_leave_owner(request, staff_user_id) -> bool:
    """Whether *request*'s caller is the member of staff the leave belongs to."""
    user = getattr(request, "user", None)
    return bool(
        staff_user_id is not None and getattr(user, "is_authenticated", False)
        and user.pk == staff_user_id
    )


def register_audit_words():
    """Hold what an employment status move records to the switches that govern it.

    A move is audited on the staff profile
    (``entity_type="StaffProfile"``, ``STAFF_EMPLOYMENT_STATUS_CHANGED``) with
    the statuses it moved between, the reason given, the day it took effect
    and, for a leaver, the last working day in the row's metadata. The
    employment status switch governs the statuses, the reason and the
    effective date, and the summary ("Employment status moved from X to Y.")
    is cut where the statuses begin. The exit date switch governs the last
    working day. The invitation that failed at go-live is audited under the
    same action without these keys, and is not a status move.
    """
    from vs_audit.protected_words import ProtectedWords, register_protected_words

    move = frozenset({"from_status", "to_status", "effective_date"})
    action = frozenset({"STAFF_EMPLOYMENT_STATUS_CHANGED"})
    register_protected_words("school.teachers.employment_status_move", ProtectedWords(
        field_key=EMPLOYMENT_STATUS_FIELD, entity_type="StaffProfile",
        action_types=action, metadata_keys=move,
        metadata_key="reason", summary_marker=" from ",
        metadata_hide=move,
    ))
    register_protected_words("school.teachers.exit_date_move", ProtectedWords(
        field_key=EXIT_DATE_FIELD, entity_type="StaffProfile",
        action_types=action, metadata_keys=move, metadata_key="",
        metadata_hide=frozenset({"last_working_day"}),
    ))
    register_leave_audit_words()


def register_leave_audit_words():
    """Hold what a leave request records to the switches that govern it.

    A request is audited when it is filed (``STAFF_LEAVE_RECORDED``) and when it
    is decided or cancelled (``STAFF_LEAVE_DECIDED``), on the request
    (``entity_type="LeaveRequest"``). Both rows name the type, the dates and the
    status in their metadata; a filing also names the days, and carries the type
    in its summary ("Leave filed: Annual leave."), where a decision carries the
    status ("Leave approved."). Each switch governs the values it owns, and the
    summary is cut at the point those words begin. The note is never audited, and
    a correction records only whose request it was.
    """
    from vs_audit.protected_words import ProtectedWords, register_protected_words

    filed = frozenset({"STAFF_LEAVE_RECORDED"})
    decided = frozenset({"STAFF_LEAVE_DECIDED"})
    both = filed | decided

    def rule(name, field_key, actions, keys, *, words="", marker=" Reason: ", hide=()):
        register_protected_words(f"school.leave.{name}", ProtectedWords(
            field_key=field_key, entity_type="LeaveRequest", action_types=actions,
            metadata_keys=frozenset(keys), metadata_key=words,
            summary_marker=marker, metadata_hide=frozenset(hide),
        ))

    rule("type_filed", LEAVE_TYPE_FIELD, filed, ("leave_type",),
         words="leave_type", marker=": ")
    rule("type_decided", LEAVE_TYPE_FIELD, decided, ("leave_type",), hide=("leave_type",))
    rule("dates", LEAVE_DATES_FIELD, both, ("start_date", "end_date"),
         hide=("start_date", "end_date"))
    rule("days", LEAVE_DAYS_FIELD, filed, ("days",), hide=("days",))
    rule("status_decided", LEAVE_STATUS_FIELD, decided, ("status",),
         words="status", marker=" ")
    rule("status_filed", LEAVE_STATUS_FIELD, filed, ("status",), hide=("status",))


def register():
    """Publish the staff field declarations to the Field Access registry."""
    register_leave()
    register_fields(
        "school",
        "teachers",
        surfaces=(
            "schools.vs_staff.serializers.StaffListSerializer",
            "schools.vs_staff.serializers.StaffDetailSerializer",
            "schools.vs_staff.serializers.StaffUpdateSerializer",
            "schools.vs_staff.serializers.StaffCreateSerializer",
            # The account block nested in a staff record, which carries the
            # sign-in address a second time.
            "schools.vs_staff.serializers.AccountStateSerializer",
            # The command palette's and the search bar's staff hit.
            "schools.vs_staff.serializers.StaffSearchHitSerializer",
        ),
        fields=(
            FieldSpec("first_name", "First name", group="Name", scope=_TENANT,
                      sort_order=10, open_on_create=True,
                      description="Held on the staff member's account."),
            FieldSpec("middle_name", "Middle name", group="Name", scope=_TENANT,
                      sort_order=20),
            FieldSpec("last_name", "Last name", group="Name", scope=_TENANT,
                      sort_order=30, open_on_create=True,
                      description="Held on the staff member's account."),
            FieldSpec("staff_number", "Staff ID", group="Employment",
                      scope=_TENANT, sort_order=10),
            FieldSpec("leave_group", "Leave group", group="Employment",
                      scope=_TENANT, sort_order=15,
                      api_names=("leave_group", "group_id")),
            FieldSpec("job_title", "Job title", group="Employment", scope=_TENANT,
                      sort_order=20),
            FieldSpec("employment_type", "Employment type", group="Employment",
                      scope=_TENANT, sort_order=30),
            FieldSpec("hire_date", "Hire date", group="Employment", scope=_TENANT,
                      sort_order=40),
            FieldSpec("employment_status", "Employment status", group="Employment",
                      scope=_TENANT, sort_order=45, api_names=EMPLOYMENT_STATUS_NAMES,
                      description="Active, on leave, suspended or left, and "
                                  "whether they are still on the roll."),
            FieldSpec("exit_date", "Exit date", group="Employment", scope=_TENANT,
                      sort_order=50, writable=False,
                      description="Set by the status change that ends employment."),
            FieldSpec("photo", "Photo", group="Personal", scope=_TENANT,
                      sort_order=5, api_names=("photo", "photo_url")),
            FieldSpec("date_of_birth", "Date of birth", group="Personal",
                      scope=_TENANT, sort_order=10),
            FieldSpec("gender", "Gender", group="Personal", scope=_TENANT,
                      sort_order=20),
            FieldSpec("phone", "Phone", group="Personal", scope=_TENANT,
                      sort_order=30,
                      description="The staff member's own number, on their account."),
            FieldSpec("email", "Email", group="Personal", scope=_TENANT,
                      sort_order=40, open_on_create=True,
                      description="The address they sign in with."),
        ),
    )


def register_leave():
    """Publish the leave request switches to the Field Access registry."""
    register_fields(
        "school",
        "leave",
        surfaces=(
            "schools.vs_staff.serializers.LeaveSerializer",
            "schools.vs_staff.serializers.LeaveWriteSerializer",
            "schools.vs_staff.serializers.LeaveUpdateSerializer",
            # The list a person's leave page opens with: the requests, and the
            # balance and days taken beside them.
            "schools.vs_staff.serializers.StaffLeaveSerializer",
            # What the leave form shows while dates are picked.
            "schools.vs_staff.serializers.LeavePreviewResultSerializer",
        ),
        fields=(
            FieldSpec("leave_type", "Type of leave", group="Request", scope=_TENANT,
                      sort_order=10, api_names=LEAVE_TYPE_NAMES, open_on_create=True,
                      description="Annual, sick, maternity and the other kinds. "
                                  "Needed to file a request, so the Write switch "
                                  "governs corrections only."),
            FieldSpec("dates", "Dates", group="Request", scope=_TENANT,
                      sort_order=20, api_names=LEAVE_DATE_NAMES, open_on_create=True,
                      description="When the leave starts and ends, and the day the "
                                  "person returns. Needed to file a request, so the "
                                  "Write switch governs corrections only."),
            FieldSpec("days", "Days requested", group="Request", scope=_TENANT,
                      sort_order=30,
                      description="The working days the request counts."),
            FieldSpec("note", "Note", group="Request", scope=_TENANT, sort_order=40,
                      description="What the person wrote about the request."),
            FieldSpec("status", "Status", group="Request", scope=_TENANT,
                      sort_order=50, writable=False, api_names=LEAVE_STATUS_NAMES,
                      description="Pending, approved, rejected or cancelled, set by "
                                  "the approval and never by a form."),
            FieldSpec("balance", "Leave balance", group="Balance", scope=_TENANT,
                      sort_order=10, writable=False, api_names=LEAVE_BALANCE_NAMES,
                      description="Allowance, days taken, days pending and days "
                                  "remaining, counted from the requests."),
        ),
    )
