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
