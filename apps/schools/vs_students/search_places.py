"""The student screens and settings, and the people the search bar finds in this module.

Two providers, students first and guardians second, so the search bar lists
them in that order. Each reuses its module endpoint's own matcher
(:func:`~.views.students.search_student_hits`,
:func:`~.views.guardians.search_guardian_hits`) behind the same key that
endpoint requires, ``school.students.view``, asked through the same door
(:func:`vs_search.access.caller_may`). A caller without it gets no hits.

A hit carries what the module's own palette endpoint returns, plus ``type``,
``label``, ``meta`` and ``screen``. No email address, phone number or
photograph: the search bar opens over whatever screen the reader is on.

Both hits are built from the Field-Access-filtered row, so a number or a name
part the caller may not read is absent from ``meta`` and ``label`` too. A
guardian's ``meta`` lists their wards, whose names follow the caller's switches
on a pupil's name parts. A person whose every name part is hidden is still a
hit, with an empty ``label``, as the directory keeps their row with an empty
name.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.access import caller_may
from vs_search.registry import (
    ACTION,
    Place,
    register,
    register_contributor,
    register_people_provider,
)

from .constants import (
    CFG_ADM_AUTO_ISSUE,
    CFG_ADM_HINT,
    CFG_ADM_PATTERN,
    CFG_ADM_REQUIRED,
    CFG_AGE_MAX,
    CFG_AGE_MIN,
    CFG_CAPACITY_DEFAULT,
    CFG_CAPACITY_MODE,
    CFG_GUARDIAN_EMAIL_REQUIRED,
    CFG_GUARDIAN_EXTRA_RELATIONSHIPS,
    CFG_GUARDIAN_MATCHING,
    CFG_GUARDIAN_MINIMUM,
    CFG_GUARDIAN_PARENT_APP_AUTO,
    CFG_PROMOTION_ARMS,
    CFG_PROMOTION_CAPACITY_MODE,
    CFG_PROMOTION_NOT_PLACED,
    CFG_PROMOTION_SUSPENDED,
    CFG_REQUIRED_DOCUMENTS,
    CFG_REQUIRED_FIELDS,
    PERM_CLASS_ASSIGN,
    PERM_PROMOTE,
    PERM_UPDATE,
    PERM_VIEW,
    SEARCH_MIN_CHARS,
)

#: The students module answers to the ``students`` capability (``vs_rbac.capability_map``).
CAPABILITY = "students"

#: The key the school's settings area asks before it opens any rules screen.
SETTINGS_VIEW = "school.settings.view"

STUDENT_SCREEN = "students.record"
GUARDIAN_SCREEN = "students.guardian_record"


def find_students(*, request, tenant, user, query, limit):
    """Students matching *query*, as search bar hits."""
    from .views.students import search_student_hits

    if len(query) < SEARCH_MIN_CHARS or not caller_may(user, tenant, PERM_VIEW):
        return []
    hits = []
    for row in search_student_hits(request, tenant, query, limit):
        hits.append({
            **row,
            "type": "student",
            "label": row.get("full_name", ""),
            "meta": " · ".join(
                filter(None, (row.get("student_number"), row.get("class_name"))),
            ),
            "screen": STUDENT_SCREEN,
        })
    return hits


def find_guardians(*, request, tenant, user, query, limit):
    """Guardians matching *query*, as search bar hits."""
    from .views.guardians import GuardianSearchView, search_guardian_hits

    if len(query) < GuardianSearchView.MIN_QUERY or not caller_may(user, tenant, PERM_VIEW):
        return []
    return [
        {
            **row,
            "type": "guardian",
            "label": row.get("full_name", ""),
            "meta": ", ".join(filter(None, row["ward_names"])),
            "screen": GUARDIAN_SCREEN,
        }
        for row in search_guardian_hits(request, tenant, query, limit)
    ]


register_people_provider(find_students)
register_people_provider(find_guardians)

#: (screen, label, description, permission, {field on the screen: definition key}).
#:
#: The rules screens open to ``school.settings.view``, the key the settings
#: area they sit in asks, and the admission number rule to the key that
#: corrects a pupil's record, because it is edited beside the enrolment form.
#: Every field is a ``vs_config`` definition, labelled in its own words.
SETTINGS_SCREENS = (
    ("students.settings.enrolment", "Enrolment rules",
     "Enrolment ages, required documents and fields, and what a full class does.",
     SETTINGS_VIEW,
     {"min_age_years": CFG_AGE_MIN, "max_age_years": CFG_AGE_MAX,
      "required_documents": CFG_REQUIRED_DOCUMENTS, "required_fields": CFG_REQUIRED_FIELDS,
      "capacity_mode": CFG_CAPACITY_MODE, "default_capacity": CFG_CAPACITY_DEFAULT}),
    ("students.settings.guardians", "Guardian rules",
     "How many guardians a pupil needs, how a guardian is matched, and the parent app.",
     SETTINGS_VIEW,
     {"min_per_student": CFG_GUARDIAN_MINIMUM, "email_required": CFG_GUARDIAN_EMAIL_REQUIRED,
      "matching": CFG_GUARDIAN_MATCHING, "extra_relationships": CFG_GUARDIAN_EXTRA_RELATIONSHIPS,
      "parent_app_auto_access": CFG_GUARDIAN_PARENT_APP_AUTO}),
    ("students.settings.admissions", "Admission steps",
     "The stages an application passes through, in the school's own order.",
     SETTINGS_VIEW, {}),
    ("students.settings.promotion", "Promotion rules",
     "What end-of-year promotion does with suspended and unplaced pupils, arms and full classes.",
     SETTINGS_VIEW,
     {"suspended": CFG_PROMOTION_SUSPENDED, "not_placed": CFG_PROMOTION_NOT_PLACED,
      "arms": CFG_PROMOTION_ARMS, "capacity_mode": CFG_PROMOTION_CAPACITY_MODE}),
    ("students.settings.admission_numbers", "Admission number rule",
     "Whether a pupil needs an admission number, its pattern, and whether one is issued.",
     PERM_UPDATE,
     {"required": CFG_ADM_REQUIRED, "pattern": CFG_ADM_PATTERN, "hint": CFG_ADM_HINT,
      "auto_issue": CFG_ADM_AUTO_ISSUE}),
)


def setting_places():
    """Each setting on the students settings screens, in its definition's words."""
    out = []
    for screen, group, _description, permission, fields in SETTINGS_SCREENS:
        out += definition_setting_places(
            screen, group, fields, permission=permission, capability=CAPABILITY,
            keywords=("student settings",),
        )
    return out


register_contributor(setting_places)

register(*(
    Place(
        kind=ACTION, key=screen, label=label, description=description,
        keywords=("student settings",), screen=screen, permission=permission,
        capability=CAPABILITY,
    )
    for screen, label, description, permission, _fields in SETTINGS_SCREENS
))

register(
    Place(
        kind=ACTION, key="students.directory", label="Students",
        description="Every student on the roll.", keywords=("pupils", "roll"),
        screen="students.directory", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="students.guardians", label="Guardians",
        description="Parents and guardians, with their wards.",
        keywords=("parents",),
        screen="students.guardians", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="students.applicants", label="Applicants",
        description="Applications waiting for a decision.", keywords=("admissions",),
        screen="students.applicants", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="students.unplaced", label="Students with no class",
        description="Pupils on the roll who are not yet in a class.",
        keywords=("unplaced", "no class"),
        screen="students.unplaced", permission=PERM_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="students.class_transfers", label="Classes and transfers",
        description="Place pupils in classes and move them between them.",
        keywords=("assign class", "move class"),
        screen="students.class_transfers", permission=PERM_CLASS_ASSIGN,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="students.promotion", label="Promotion",
        description="Move a year group up at the end of the session.",
        keywords=("promote", "end of year"),
        screen="students.promotion", permission=PERM_PROMOTE, capability=CAPABILITY,
    ),
)
