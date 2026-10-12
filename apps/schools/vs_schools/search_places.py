"""The school's own screens and settings screens, and the settings on them, for the search bar.

The overview, the school profile and the branches each read with their own
``school.*`` key; each settings screen reads with ``school.settings.view``.
All of them are core: every school has them whatever its plan includes, so no
place names a capability.

Every setting on these screens is a ``vs_config`` definition, so each is
labelled and described in its definition's own words and anchored at the field
name the screen's endpoint uses for it
(:func:`vs_config.search_places.definition_setting_places`).
"""
from __future__ import annotations

from schools.vs_staff.services.visibility import POLICY_KEY
from vs_config.clock import TIME_ZONE_KEY
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
from vs_config.runtime_settings import SECURITY_FIELDS
from vs_config.search_places import definition_setting_places
from vs_finance.payroll import PAYROLL_SCOPE_KEY
from vs_search.registry import ACTION, Place, register, register_contributor

SETTINGS_VIEW = "school.settings.view"

#: (screen, label, description, {field name on the screen: definition key}).
SCREENS = (
    ("school.settings.security", "School security settings",
     "Sign-in lockout, reset link lifetimes and invitation expiry for this school.",
     dict(SECURITY_FIELDS)),
    ("school.settings.payroll_scope", "Payroll scope",
     "One payroll for the whole school, or one per branch.",
     {"scope": PAYROLL_SCOPE_KEY}),
    ("school.settings.display", "Date and time display",
     "Time zone, date format and clock, for the school and each branch.",
     {"timezone": TIME_ZONE_KEY, "date_format": DATE_FORMAT_KEY, "clock": CLOCK_KEY}),
    ("school.settings.staff_profiles", "Staff profile visibility",
     "Who reads how much of a staff member's profile.",
     {"policy": POLICY_KEY}),
)


def setting_places():
    """Each setting on the school's settings screens, in its definition's words."""
    out = []
    for screen, group, _description, fields in SCREENS:
        out += definition_setting_places(
            screen, group, fields, permission=SETTINGS_VIEW, keywords=("school settings",),
        )
    return out


register_contributor(setting_places)

register(*(
    Place(
        kind=ACTION, key=screen, label=label, description=description,
        keywords=("school settings",), screen=screen, permission=SETTINGS_VIEW,
    )
    for screen, label, description, _fields in SCREENS
))

register(
    Place(
        kind=ACTION, key="school.dashboard", label="Dashboard",
        description="The school at a glance.", keywords=("overview", "home"),
        screen="school.dashboard", permission="school.dashboard.view",
    ),
    Place(
        kind=ACTION, key="school.profile", label="School profile",
        description="The school's name, logo, address and contact details.",
        keywords=("logo", "school details"),
        screen="school.profile", permission="school.profile.view",
    ),
    Place(
        kind=ACTION, key="school.branches", label="Branches",
        description="Every branch of the school and who runs it.",
        screen="school.branches", permission="school.branches.view",
    ),
)
