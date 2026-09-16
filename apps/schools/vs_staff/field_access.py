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
        ),
        fields=(
            FieldSpec("date_of_birth", "Date of birth", group="Personal",
                      scope=_TENANT, sort_order=10),
            FieldSpec("gender", "Gender", group="Personal", scope=_TENANT,
                      sort_order=20),
            FieldSpec("phone", "Phone", group="Personal", scope=_TENANT,
                      sort_order=30,
                      description="The staff member's own number, on their account."),
            FieldSpec("email", "Email", group="Personal", scope=_TENANT,
                      sort_order=40,
                      description="The address they sign in with."),
        ),
    )
