"""Student and guardian fields an administrator may restrict per role.

Blood group, allergies and conditions are hidden from a caller without
``school.students.view_sensitive`` and refused on write for the same caller,
whether the write is an edit or an enrolment.
The enrolment date is readable by everybody who can open the record, and only
changing it needs ``school.students.manage``, so it is not sensitive. It is
declared open on create for the same reason: whoever enrols the pupil, by form
or by spreadsheet, sets the date they enrolled on, and the write switch decides
only who may correct it afterwards.

A guardian's contact details are not sensitive either. Nothing withholds them
today: every caller who may open a student or the guardian directory reads the
phone number, the email address, the home address and the occupation. Marking
them sensitive would close them to every role the day the switches start being
enforced, which is the one thing Field Access must not do, so they are declared
open and a school turns them off for the roles it chooses.

``school.guardians`` carries fields and no permission keys. A guardian is read
with ``school.students.view`` and corrected with ``school.students.update``
(``views/guardians.py``), so the resource exists to hang switches on and mints
nothing new. ``core.seed_school_permissions`` registers it.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"


def register():
    """Publish the student field declarations to the Field Access registry."""
    register_fields(
        "school",
        "students",
        surfaces=(
            "schools.vs_students.serializers.StudentDetailSerializer",
            "schools.vs_students.serializers.StudentWriteSerializer",
            "schools.vs_students.serializers.EnrolmentWriteSerializer",
        ),
        fields=(
            FieldSpec("blood_group", "Blood group", group="Medical", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("allergies", "Allergies", group="Medical", sensitive=True,
                      scope=_TENANT, sort_order=20),
            FieldSpec("conditions", "Medical conditions", group="Medical",
                      sensitive=True, scope=_TENANT, sort_order=30),
            FieldSpec("enrolment_date", "Enrolment date", group="Enrolment",
                      scope=_TENANT, sort_order=10, open_on_create=True),
        ),
    )
    register_fields(
        "school",
        "guardians",
        surfaces=(
            "schools.vs_students.serializers.GuardianSerializer",
            "schools.vs_students.serializers.GuardianDirectorySerializer",
            "schools.vs_students.serializers.GuardianUpdateSerializer",
            "schools.vs_students.serializers.GuardianWriteSerializer",
            # Carries the same details nested under ``guardian`` on a student's
            # guardian list.
            "schools.vs_students.serializers.GuardianLinkSerializer",
        ),
        fields=(
            FieldSpec("phone", "Phone", group="Contact", scope=_TENANT,
                      sort_order=10,
                      description="The number the school calls about the child."),
            FieldSpec("email", "Email", group="Contact", scope=_TENANT,
                      sort_order=20,
                      description="Where the guardian's own login is issued."),
            FieldSpec("address", "Address", group="Contact", scope=_TENANT,
                      sort_order=30,
                      description="The guardian's own address, which is not "
                                  "always the child's."),
            FieldSpec("occupation", "Occupation", group="Contact", scope=_TENANT,
                      sort_order=40),
        ),
    )
