"""School datasets published to the Export Centre.

Registered from :meth:`vs_schools.apps.VsSchoolsConfig.ready`.

**This is the one dataset in the platform whose base queryset is not the tenant
boundary, and that is a deliberate, reviewed exception.**

Every other dataset fences its rows with ``filter(tenant=scope.tenant)`` or
``filter(entity=scope.entity)``, so no code path can read past it. The School
Management console is different by design: it is the CX platform's register of
*every* school, and :class:`vs_schools.views.school.SchoolListView` already
serves ``School.objects.all()`` behind ``platform.schools.view``. An export that
fenced itself to one tenant would not match the screen it is started from, which
is the one thing this feature promises.

So the boundary here is the **permission key**, exactly as it is on the list
endpoint this mirrors. The consequence is worth stating plainly: anyone holding
``platform.schools.view`` can export the whole register, so that key must stay a
platform-actor grant. Granting it to a school-level role would let that school
export every other school's record - which is already true of the screen, and
becomes true of a file the moment it is granted.
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


# The platform register of schools. Not tenant-fenced - see the module docstring.
def _schools(scope):
    from .models import School

    return School.objects.all()


_SCHOOL_STATUS = choice_labels("vs_schools.models.SchoolStatus")
_OWNERSHIP = choice_labels("vs_schools.models.OwnershipType")


# Register every schools dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="platform.schools",
        module="Administration",
        name="Schools",
        description=(
            "The platform register of schools, with status, ownership and the dates "
            "each was activated or deactivated. Covers every school on the platform, "
            "the same as the School Management screen."
        ),
        base=_schools,
        # Declared TENANT because it has no ledger entity - not because the rows
        # are tenant-fenced. `_schools` ignores the scope; the permission is the
        # boundary. See the module docstring.
        scope=DatasetScope.TENANT,
        permission="platform.schools.view",
        row_cap=100_000,
        default_columns=("name", "code", "status", "ownership_type", "created_at"),
        fields=(
            Field("slug", "Slug", "School", KIND_TEXT, locked=True,
                  description="URL-safe unique identifier - the row's identity."),
            Field("name", "Name", "School", KIND_TEXT),
            Field("code", "Code", "School", KIND_TEXT),
            Field("status", "Status", "School", KIND_CHOICE, choices=_SCHOOL_STATUS),
            Field("ownership_type", "Ownership", "School", KIND_CHOICE, choices=_OWNERSHIP),
            Field("term_structure", "Term structure", "School", KIND_TEXT),
            Field("currency", "Currency", "School", KIND_TEXT),
            Field("address", "Address", "Contact", KIND_TEXT),
            Field("website", "Website", "Contact", KIND_TEXT),
            Field("motto", "Motto", "School", KIND_TEXT),
            Field("registration_id", "Registration ID", "School", KIND_TEXT,
                  sensitive=True,
                  description="Restricted: the school's external registration number."),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("activated_at", "Activated", "Lifecycle", KIND_DATETIME),
            Field("deactivated_at", "Deactivated", "Lifecycle", KIND_DATETIME),
        ),
        filters=(
            # Not required: this is master data, and "every school" is a
            # legitimate and small answer - unlike a transaction table.
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_SCHOOL_STATUS),
            FilterDef("ownership_type", "Ownership", FILTER_CHOICE, choices=_OWNERSHIP),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"), ("slug", "Slug"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("name", "Name", FILTER_TEXT),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the School Management screen's filters into export filters.
def _translate_schools(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    # The screen searches on `q`; `search` is accepted too so a caller that
    # spells it the other way is not silently ignored.
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    return filters, unmapped


# Register the schools screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="platform.schools",
        handles=(
            "status", "q", "search",
        ),
        label="Administration - Schools",
        dataset_key="platform.schools",
        translate=_translate_schools,
    ))
