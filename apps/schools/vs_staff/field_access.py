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
"""
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


def register():
    """Publish the staff field declarations to the Field Access registry."""
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
