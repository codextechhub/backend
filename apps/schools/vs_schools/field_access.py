"""School profile fields a school may restrict per role.

The school-side resources are separate from CodeX's platform management
surfaces. A school's choices govern what its own roles read or change in the
school app; platform staff continue to use the platform permission keys and
serializers that administer every tenant.

Address and registration number belong to the school's own profile form, so
each receives independent Read and Write control. The declarations are open by
default to preserve the screens existing roles already use.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"


def register():
    """Publish the school-owned fields to the Field Access registry."""
    register_fields(
        "school",
        "profile",
        surfaces=(
            "schools.vs_schools.serializers.SchoolProfileSerializer",
            "schools.vs_schools.serializers.SchoolProfileUpdateSerializer",
        ),
        fields=(
            FieldSpec(
                "registration_id", "Registration number", group="Identity",
                scope=_TENANT, sort_order=20,
            ),
            FieldSpec(
                "address", "Address", group="Contact", scope=_TENANT,
                sort_order=10,
            ),
        ),
    )
