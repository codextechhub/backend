"""The academic structure screens and settings the search bar can route to.

The ``academics`` permission module is core (:mod:`vs_rbac.capability_map`),
so no place here names a capability. The settings screen opens to
``school.settings.view``, the key the school's settings area asks, and each of
its fields is a ``vs_config`` definition labelled in its own words.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.registry import ACTION, Place, register, register_contributor

from .constants import CFG_DEFAULT_ARMS, CFG_TERM_NAMES, CFG_TERM_WORD

SETTINGS_SCREEN = "academics.settings"
SETTINGS_GROUP = "Academic structure settings"
SETTINGS_VIEW = "school.settings.view"


def setting_places():
    """Each setting on the academic structure settings screen."""
    return definition_setting_places(
        SETTINGS_SCREEN, SETTINGS_GROUP,
        {"term_word": CFG_TERM_WORD, "term_names": CFG_TERM_NAMES,
         "default_arms": CFG_DEFAULT_ARMS},
        permission=SETTINGS_VIEW, keywords=("school settings",),
    )


register_contributor(setting_places)

register(
    Place(
        kind=ACTION, key="academics.structure", label="Academic structure",
        description="Programmes, levels and departments.",
        keywords=("programmes", "levels", "departments"),
        screen="academics.structure", permission="academics.structure.view",
    ),
    Place(
        kind=ACTION, key="academics.departments", label="Departments",
        description="The school's departments and who heads them.",
        screen="academics.departments", permission="academics.structure.view",
    ),
    Place(
        kind=ACTION, key="academics.programmes", label="Programmes and levels",
        description="Each programme the school runs and the levels in it.",
        keywords=("programs", "levels"),
        screen="academics.programmes", permission="academics.structure.view",
    ),
    Place(
        kind=ACTION, key="academics.sessions", label="Academic sessions",
        description="School years and their terms.", keywords=("terms", "school year"),
        screen="academics.sessions", permission="academics.session.view",
    ),
    Place(
        kind=ACTION, key="academics.classes", label="Classes",
        description="Every class and its arm.", keywords=("arms",),
        screen="academics.classes", permission="academics.classes.view",
    ),
    Place(
        kind=ACTION, key="academics.subjects", label="Subjects",
        screen="academics.subjects", permission="academics.subject.view",
    ),
    Place(
        kind=ACTION, key=SETTINGS_SCREEN, label=SETTINGS_GROUP,
        description="What a term is called, the term names and the default class arms.",
        keywords=("school settings", "terms", "arms"),
        screen=SETTINGS_SCREEN, permission=SETTINGS_VIEW,
    ),
)
