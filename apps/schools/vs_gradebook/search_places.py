"""The gradebook screens and settings the search bar can route to.

The gradebook answers to the ``gradebook`` capability
(:mod:`vs_rbac.capability_map`). Each place takes the key its screen's read
endpoint requires. The report card options on the assessment setup screen are
``vs_config`` definitions, labelled in their own words and anchored at the field
name the screen's endpoint uses
(:data:`~.services.configuration.KEY_FOR_FIELD`), so a setting added to the
screen is searchable with it.
"""
from __future__ import annotations

from vs_config.search_places import definition_setting_places
from vs_search.registry import ACTION, Place, register, register_contributor

from .constants import (
    PERM_CLASS_PUBLISH,
    PERM_CLASS_SUBMIT,
    PERM_HOLD_VIEW,
    PERM_HUB_VIEW,
    PERM_MODERATION_APPROVE,
    PERM_RELEASE_VIEW,
    PERM_SETUP_VIEW,
    PERM_SHEET_VIEW,
)

CAPABILITY = "gradebook"
SETUP_SCREEN = "gradebook.setup"
SHEETS_SCREEN = "gradebook.my_sheets"
HUB_SCREEN = "gradebook.hub"
MODERATION_SCREEN = "gradebook.moderation"
BROADSHEET_SCREEN = "gradebook.broadsheet"
APPROVALS_SCREEN = "gradebook.approvals"
HOLDS_SCREEN = "gradebook.holds"
RELEASE_SCREEN = "gradebook.release"
SETUP_GROUP = "Assessment setup"


def setting_places():
    """Each report card option on the assessment setup screen."""
    from .services.configuration import KEY_FOR_FIELD

    return definition_setting_places(
        SETUP_SCREEN, SETUP_GROUP, KEY_FOR_FIELD,
        permission=PERM_SETUP_VIEW, capability=CAPABILITY,
        keywords=("gradebook", "report card"),
    )


register_contributor(setting_places)

register(
    Place(
        kind=ACTION, key=SHEETS_SCREEN, label="Score sheets",
        description="The score sheets you are the main teacher of, and the classes "
                    "and departments you have results duties for.",
        keywords=("gradebook", "score sheet", "enter scores", "marks", "results",
                  "continuous assessment", "exam scores"),
        screen=SHEETS_SCREEN, permission=PERM_SHEET_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=HUB_SCREEN, label="Results hub",
        description="How the term's results are coming: sheets in, classes compiled "
                    "and published, and the late sheets with a reminder for each.",
        keywords=("gradebook", "results", "late sheets", "remind teacher",
                  "results day", "report cards"),
        screen=HUB_SCREEN, permission=PERM_HUB_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=MODERATION_SCREEN, label="Moderation",
        description="Score sheets waiting for the head of department to approve or "
                    "return, with the class average and pass rate beside each.",
        keywords=("gradebook", "moderate", "approve sheet", "return sheet",
                  "head of department", "results"),
        screen=MODERATION_SCREEN, permission=PERM_MODERATION_APPROVE,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=BROADSHEET_SCREEN, label="Broadsheet",
        description="Your class's totals for every subject, with the average and "
                    "position of each student, the comments to write and the button "
                    "that sends the class to the principal.",
        keywords=("gradebook", "broadsheet", "compile", "class teacher", "report card",
                  "comments", "ratings", "submit class", "results"),
        screen=BROADSHEET_SCREEN, permission=PERM_CLASS_SUBMIT, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=APPROVALS_SCREEN, label="Approvals and publishing",
        description="Classes waiting for the principal: return them with a note, "
                    "write the principal's comments and publish the results.",
        keywords=("gradebook", "approve results", "publish results", "principal",
                  "principal's comment", "release report cards", "return class"),
        screen=APPROVALS_SCREEN, permission=PERM_CLASS_PUBLISH, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=HOLDS_SCREEN, label="Fee holds",
        description="Students whose result is kept from release until fees are "
                    "settled, with what each family owes and who placed the hold.",
        keywords=("gradebook", "fee hold", "hold result", "withhold report card",
                  "outstanding fees", "bursar", "lift hold"),
        screen=HOLDS_SCREEN, permission=PERM_HOLD_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=RELEASE_SCREEN, label="Report cards and release",
        description="Each class's published results: who was released, who was held "
                    "and whether the guardian was emailed, with every report card to "
                    "view or print.",
        keywords=("gradebook", "report card", "print report cards", "release results",
                  "guardian email", "held results", "results"),
        screen=RELEASE_SCREEN, permission=PERM_RELEASE_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key=SETUP_SCREEN, label=SETUP_GROUP,
        description="The components a result is made of, the grading scales, the "
                    "report card options and the principal's remark templates.",
        keywords=("gradebook", "assessment", "grading scale", "continuous assessment",
                  "exam weighting", "report card", "remark", "moderation"),
        screen=SETUP_SCREEN, permission=PERM_SETUP_VIEW, capability=CAPABILITY,
    ),
)
