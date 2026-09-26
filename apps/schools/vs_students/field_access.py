"""Student and guardian fields an administrator may restrict per role.

Blood group, allergies and conditions are declared sensitive, so a role reads
and corrects them only where a school has turned the switches on, and a write
is refused whether it arrives as an edit or as an enrolment.

The enrolment date is readable by everybody who can open the record, so it is
not sensitive. It is declared open on create: whoever enrols the pupil, by form
or by spreadsheet, sets the date they enrolled on, and the write switch decides
only who may correct it afterwards.

A guardian's contact details are not sensitive either. Nothing withholds them
today: every caller who may open a student or the guardian directory reads the
phone number, the email address, the home address and the occupation. Marking
them sensitive would close them to every role the day the switches start being
enforced, which is the one thing Field Access must not do, so they are declared
open and a school turns them off for the roles it chooses.

A guardian's phone number is declared open on create. Every route that adds a
guardian (a row of the enrol form, the link form on a student's record, and the
guardian and student spreadsheet imports) requires one, while the correction
form may still change it under its switch, so without the flag a role that may
not change a phone number could never add a guardian at all. Setting it while
the guardian is created is not changing it, and the Write switch keeps
deciding who may correct it afterwards. The email address, home address and
occupation are optional on every create route and a blank one is dropped, so
each stays governed by its switch on both paths.

The name, the personal and contact details, the emergency contact, the
background and the student number are open too, for the same reason: every
caller who may open a student reads them today. Each is a fact a school may be
asked about later ("what was this child's surname on the day of the exam?"),
so each has a switch a school can turn off per role. The name parts, the date
of birth and the gender are required to enrol a pupil and the student number
is chosen then, so those are open on create: the switch decides who may
correct them afterwards, not who may enrol.

The photograph is the passport photograph on the document checklist and reaches
a client as ``photo_url``. Its Write switch is enforced where a passport
photograph is attached (``views/records.py``), because no serializer writes it.

A name switch covers the student's own record, list row and profile, where
``full_name`` is rebuilt from the parts the caller may read. A name printed on
another module's document (an invoice, a class list) is that module's field.

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
            "schools.vs_students.serializers.StudentListSerializer",
            "schools.vs_students.serializers.StudentDetailSerializer",
            "schools.vs_students.serializers.StudentWriteSerializer",
            "schools.vs_students.serializers.EnrolmentWriteSerializer",
            "schools.vs_students.serializers.SearchHitSerializer",
        ),
        fields=(
            FieldSpec("first_name", "First name", group="Name", scope=_TENANT,
                      sort_order=10, open_on_create=True),
            FieldSpec("middle_name", "Middle name", group="Name", scope=_TENANT,
                      sort_order=20),
            FieldSpec("last_name", "Last name", group="Name", scope=_TENANT,
                      sort_order=30, open_on_create=True),
            FieldSpec("date_of_birth", "Date of birth", group="Personal",
                      scope=_TENANT, sort_order=10, open_on_create=True),
            FieldSpec("gender", "Gender", group="Personal", scope=_TENANT,
                      sort_order=20, open_on_create=True),
            FieldSpec("nationality", "Nationality", group="Personal",
                      scope=_TENANT, sort_order=30),
            FieldSpec("state_of_origin", "State of origin", group="Personal",
                      scope=_TENANT, sort_order=40),
            FieldSpec("photo", "Photo", group="Personal", scope=_TENANT,
                      sort_order=50, api_names=("photo_url",),
                      description="The passport photograph on the document checklist."),
            FieldSpec("address", "Address", group="Contact", scope=_TENANT,
                      sort_order=10),
            FieldSpec("phone", "Phone", group="Contact", scope=_TENANT,
                      sort_order=20),
            FieldSpec("email", "Email", group="Contact", scope=_TENANT,
                      sort_order=30),
            FieldSpec("emergency_contact_name", "Emergency contact name",
                      group="Emergency", scope=_TENANT, sort_order=10),
            FieldSpec("emergency_contact_phone", "Emergency contact phone",
                      group="Emergency", scope=_TENANT, sort_order=20),
            FieldSpec("previous_school", "Previous school", group="Background",
                      scope=_TENANT, sort_order=10),
            FieldSpec("student_number", "Student number", group="Background",
                      scope=_TENANT, sort_order=20, open_on_create=True),
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
            FieldSpec("first_name", "First name", group="Name", scope=_TENANT,
                      sort_order=10, open_on_create=True),
            FieldSpec("middle_name", "Middle name", group="Name", scope=_TENANT,
                      sort_order=20),
            FieldSpec("last_name", "Last name", group="Name", scope=_TENANT,
                      sort_order=30, open_on_create=True),
            FieldSpec("photo", "Photo", group="Personal", scope=_TENANT,
                      sort_order=10, api_names=("photo_url",),
                      description="Set and removed on the guardian's photograph route."),
            FieldSpec("phone", "Phone", group="Contact", scope=_TENANT,
                      sort_order=10, open_on_create=True,
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
