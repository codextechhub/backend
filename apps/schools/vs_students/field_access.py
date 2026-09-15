"""Student record fields an administrator may restrict per role.

Blood group, allergies and conditions are hidden from a caller without
``school.students.view_sensitive`` and refused on write for the same caller,
whether the write is an edit or an enrolment.
The enrolment date is readable by everybody who can open the record, and only
changing it needs ``school.students.manage``, so it is not sensitive.
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
                      scope=_TENANT, sort_order=10),
        ),
    )
