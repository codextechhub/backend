"""The students dataset, published to the Export Centre.

Registered from ``VsStudentsConfig.ready`` so the engine never imports a domain
app - the same direction vs_schools and vs_academics already run in.

**This dataset is tenant-fenced, unlike ``platform.schools``.** That exception
exists because the School register is a platform screen; a student list is not,
and a dataset that answered the same way would export every school's children
to anyone holding the key.

**No medical field is in it at all**, and that is a stronger rule than the
field-level gate on the profile. A gate protects a screen; a file leaves the
building, and there is no way to un-send it. The same reasoning removes the
emergency contact, which is ungated on the profile precisely because somebody
may need to read it in a hurry - a need a spreadsheet does not have.

FRD M11 v2.4 section 8.5 and FR-014.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATETIME,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    choice_labels,
    register,
)

_STATUS = choice_labels("schools.vs_students.constants.StudentStatus")
_GENDER = choice_labels("schools.vs_students.constants.Gender")


def _students(scope):
    """Fenced to the tenant, then narrowed by branch.

    The branch narrowing matters as much as the tenant fence: without it a
    caller pinned to Ikeja could export Lekki's children, which is exactly the
    boundary the screen they started from enforces.
    """
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    from .models import Student

    qs = Student.objects.filter(tenant=scope.tenant)
    visible = visible_branch_ids(getattr(scope, "user", None), scope.tenant)
    if visible is WHOLE_TENANT:
        return qs
    if not visible:
        return qs.none()
    return qs.filter(branch_id__in=tuple(sorted(visible)))


def register_datasets():
    register(Dataset(
        key="school.students",
        module="Students",
        name="Students",
        description=(
            "The student roll, with admission number, status, branch and "
            "enrolment date. Medical details and emergency contacts are "
            "deliberately not available in any export."
        ),
        base=_students,
        scope=DatasetScope.TENANT,
        permission="school.students.export",
        row_cap=100_000,
        default_columns=(
            "id", "student_number", "last_name", "first_name", "status",
            "enrolment_date",
        ),
        fields=(
            # Locked, and it has to be the primary key rather than the
            # admission number: the number is optional by design, so a file
            # keyed on it could not identify the rows of a school that has not
            # numbered its children.
            Field("id", "Student ID", "Student", KIND_TEXT, locked=True,
                  description="The row's identity. Present on every student."),
            Field("student_number", "Admission no.", "Student", KIND_TEXT,
                  description="The school's own number. Blank where none was given."),
            Field("first_name", "First name", "Student", KIND_TEXT, sensitive=True,
                  description="A child's name. Including it is audited."),
            Field("middle_name", "Middle name", "Student", KIND_TEXT, sensitive=True),
            Field("last_name", "Last name", "Student", KIND_TEXT, sensitive=True),
            Field("date_of_birth", "Date of birth", "Student", KIND_TEXT,
                  sensitive=True,
                  description="A child's date of birth. Including it is audited."),
            Field("gender", "Gender", "Student", KIND_CHOICE, choices=_GENDER),
            Field("nationality", "Nationality", "Student", KIND_TEXT),
            Field("state_of_origin", "State of origin", "Student", KIND_TEXT),
            Field("address", "Home address", "Contact", KIND_TEXT, sensitive=True,
                  description="A child's home address. Including it is audited."),
            Field("phone", "Student phone", "Contact", KIND_TEXT, sensitive=True),
            Field("email", "Student email", "Contact", KIND_TEXT, sensitive=True),
            Field("previous_school", "Previous school", "Student", KIND_TEXT),
            Field("status", "Status", "Lifecycle", KIND_CHOICE, choices=_STATUS),
            Field("enrolment_date", "Enrolled", "Lifecycle", KIND_TEXT),
            Field("branch__name", "Branch", "Placement", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("enrolment_date", "Enrolled", FILTER_DATE_RANGE,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_STATUS),
            FilterDef("branch__name", "Branch", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("first_name", "First name"), ("last_name", "Last name"),
                ("student_number", "Admission no."),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))
