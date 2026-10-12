"""The staff screens and settings, and the people the search bar finds in this module.

The provider reuses the palette endpoint's own matcher
(:func:`~.views.directory.search_staff_hits`) behind the key that endpoint
requires, ``school.teachers.view``, asked through the same door
(:func:`vs_search.access.caller_may`); a caller without it gets no hits. A hit
carries what that endpoint returns, plus ``type``, ``label`` and ``screen``,
and never an email address, a phone number or a photograph. Like that
endpoint, it is filtered by the caller's switches on the name parts, the job
title and the staff number; ``label`` is the filtered name, so a hidden name
part never reaches it, and a member of staff whose whole name is hidden is a
hit with an empty ``label``, as the directory keeps their row.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.access import caller_may
from vs_search.registry import (
    ACTION,
    Place,
    register,
    register_condition,
    register_contributor,
    register_people_provider,
)

from .constants import (
    CFG_HIRE_APPROVAL,
    CFG_LEAVE_ALLOWANCES,
    CFG_LEAVE_EXCLUDE_CLOSURES,
    CFG_LEAVE_GROUPS,
    CFG_LEAVE_OVERRIDES,
    CFG_LEAVE_WORKING_DAYS,
    CFG_NUMBER_AUTO_ISSUE,
    CFG_NUMBER_HINT,
    CFG_NUMBER_PATTERN,
    CFG_NUMBER_REQUIRED,
    CFG_REQUIRED_DOCUMENTS,
    CFG_SELF_EDITABLE,
    CFG_STARTING_ROLE,
    PERM_ORG_VIEW,
    PERM_UPDATE,
    PERM_VIEW,
)

#: The staff register answers to the ``teachers`` capability (``vs_rbac.capability_map``).
CAPABILITY = "teachers"

STAFF_SCREEN = "staff.record"

#: The key the school's settings area asks before it opens any rules screen.
SETTINGS_VIEW = "school.settings.view"

#: (screen, label, description, {field on the screen: definition key}). The leave
#: rules travel nested under ``leave``, so their anchors are dotted. Every field
#: is a ``vs_config`` definition, labelled in its own words.
SETTINGS_SCREENS = (
    ("staff.settings.rules", "Staff rules",
     "A new member of staff's starting role and documents, self-service edits, "
     "hire approval and leave.",
     {"starting_role": CFG_STARTING_ROLE, "required_documents": CFG_REQUIRED_DOCUMENTS,
      "self_editable_fields": CFG_SELF_EDITABLE, "hire_requires_approval": CFG_HIRE_APPROVAL,
      "leave.allowances": CFG_LEAVE_ALLOWANCES, "leave.groups": CFG_LEAVE_GROUPS,
      "leave.overrides": CFG_LEAVE_OVERRIDES, "leave.working_days": CFG_LEAVE_WORKING_DAYS,
      "leave.exclude_closures": CFG_LEAVE_EXCLUDE_CLOSURES}),
    ("staff.settings.staff_ids", "Staff ID rule",
     "Whether a member of staff needs a staff number, its pattern, and whether one is issued.",
     {"required": CFG_NUMBER_REQUIRED, "pattern": CFG_NUMBER_PATTERN,
      "hint": CFG_NUMBER_HINT, "auto_issue": CFG_NUMBER_AUTO_ISSUE}),
)


#: The caller has a staff record at the tenant ("My staff record" exists for them).
HAS_STAFF_RECORD = "staff.has_record"
#: The branch dimension is drawn for this caller (the postings screen exists for them).
VIEWER_SEES_BRANCHES = "staff.viewer_sees_branches"


def has_staff_record(*, request, tenant, branch, user) -> bool:
    """Whether "My staff record" would answer this caller rather than 404."""
    from .views.directory import own_staff_record_id

    return own_staff_record_id(tenant, user) is not None


def viewer_sees_branches_condition(*, request, tenant, branch, user) -> bool:
    """Whether the staff screens draw branches for this caller, as their views ask."""
    from .services.scoping import viewer_sees_branches

    return tenant is not None and viewer_sees_branches(user, tenant)


register_condition(HAS_STAFF_RECORD, has_staff_record)
register_condition(VIEWER_SEES_BRANCHES, viewer_sees_branches_condition)


def find_staff(*, request, tenant, user, query, limit):
    """Staff matching *query*, as search bar hits."""
    from .views.directory import StaffSearchView, search_staff_hits

    if len(query) < StaffSearchView.MIN_QUERY or not caller_may(user, tenant, PERM_VIEW):
        return []
    return [
        {**row, "type": "staff", "label": row.get("name", ""), "screen": STAFF_SCREEN}
        for row in search_staff_hits(request, tenant, query, limit)
    ]


register_people_provider(find_staff)



def setting_places():
    """Each setting on the staff settings screens, in its definition's words."""
    out = []
    for screen, group, _description, fields in SETTINGS_SCREENS:
        out += definition_setting_places(
            screen, group, fields, permission=SETTINGS_VIEW, capability=CAPABILITY,
            keywords=("staff settings",),
        )
    return out


register_contributor(setting_places)

register(*(
    Place(
        kind=ACTION, key=screen, label=label, description=description,
        keywords=("staff settings",), screen=screen, permission=SETTINGS_VIEW,
        capability=CAPABILITY,
    )
    for screen, label, description, _fields in SETTINGS_SCREENS
))

register(
    Place(
        kind=ACTION, key="staff.directory", label="Staff",
        description="Everybody who works here.", keywords=("teachers", "employees"),
        screen="staff.directory", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="staff.teaching", label="Teaching duties",
        description="Who teaches which subject to which class.",
        keywords=("subjects", "classes", "timetable load"),
        screen="staff.teaching", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    # Absent where the caller sees one branch, as the roster and the posting
    # controls are (``StaffViewMixin.multi_branch``).
    Place(
        kind=ACTION, key="staff.posting", label="Postings and reach",
        description="Which branch each member of staff works at and reaches.",
        keywords=("branch", "posting"),
        screen="staff.posting", permission=PERM_UPDATE, capability=CAPABILITY,
        condition=VIEWER_SEES_BRANCHES,
    ),
    # Needs no key: every member of staff reaches their own record.
    Place(
        kind=ACTION, key="staff.my_record", label="My staff record",
        description="Your own profile, leave and documents.",
        keywords=("my profile", "apply for leave", "my details"),
        screen="staff.my_record", capability=CAPABILITY, condition=HAS_STAFF_RECORD,
    ),
    # The organogram's keys sit under ``school.organogram``, which is core.
    Place(
        kind=ACTION, key="staff.organogram", label="Organogram",
        description="Posts, who holds them and who reports to whom.",
        keywords=("org chart", "reporting lines", "positions"),
        screen="staff.organogram", permission=PERM_ORG_VIEW,
    ),
)
