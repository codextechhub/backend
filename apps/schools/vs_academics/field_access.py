"""The academic fields an administrator may restrict per role.

Two resources, each with the fields that are not already open to everyone who
can open the screen.

**``academics.department``: who heads the department.** ``head`` is a stored
field with a Read switch and a Write switch. It travels as ``head`` on a
department's read (an ``{id, name}`` pair, or null) and as ``head`` on a
create or an edit (the staff id). Hiding Read removes the pair; denying Write
refuses a department save that names a head, however the rest of the body
looks. A department's name, code and description are open like the rest of the
structure and carry no switch.

**``academics.electives``: who takes which non-core subject.** One switch
covers the choice, because the two figures that follow from it are the same
fact. ``takes`` is the subjects a student takes (on the roll of a class) and
the answer a save carries for one student and one subject; ``takers`` is the
number of the roll taking each subject, which is that choice added up. Hiding
Read removes both from the screen, and a role that cannot read the choice cannot
read how many make it. Write governs the save only: a role that may read but
not write sees the grid and is refused any change. The student's name and
admission number on the roll, and the subject's name and code, are the roll
and the subject list, fields of the student records and the subjects, and
carry no switch here.

The head's name is the staff member's name as the class teacher's is on a
class: a department screen is unusable without who runs it, so it is not
governed by Staff Management's name switches.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"

DEPARTMENT_SURFACES = (
    "schools.vs_academics.serializers.DepartmentSerializer",
    "schools.vs_academics.serializers.DepartmentWriteSerializer",
)

ELECTIVES_SURFACES = (
    "schools.vs_academics.serializers.ElectiveClassTermSerializer",
    "schools.vs_academics.serializers.ElectiveSubjectSerializer",
    "schools.vs_academics.serializers.ElectiveStudentSerializer",
    "schools.vs_academics.serializers.ElectiveChangeSerializer",
)


def register():
    """Publish the academic fields to the Field Access registry."""
    register_fields(
        "academics",
        "department",
        surfaces=DEPARTMENT_SURFACES,
        fields=(
            FieldSpec("head", "Head of department", group="Department", scope=_TENANT,
                      sort_order=10,
                      description="The member of staff who heads the department, on "
                                  "the department list and its edit form."),
        ),
    )
    register_fields(
        "academics",
        "electives",
        surfaces=ELECTIVES_SURFACES,
        fields=(
            FieldSpec("takes", "Takes the subject", group="Who takes what", scope=_TENANT,
                      sort_order=10, api_names=("takes", "takers"),
                      description="Which non-core subjects each student takes in a "
                                  "class, and how many students take each subject."),
        ),
    )
