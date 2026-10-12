"""The onboarding screens the search bar can route to.

Onboarding is core (:mod:`vs_rbac.capability_map`): a school sets itself up
whatever its plan includes, so no place here names a capability.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

from .constants import PERM_GO_LIVE_VIEW, PERM_PROGRESS_VIEW

register(
    Place(
        kind=ACTION, key="onboarding.control_room", label="Setup control room",
        description="The setup checklist, what is left to do and whether the school can go live.",
        keywords=("onboarding", "setup", "checklist"),
        screen="onboarding.control_room", permission=PERM_PROGRESS_VIEW,
    ),
    Place(
        kind=ACTION, key="onboarding.go_live", label="Go-live requests",
        description="Requests to take the school live and what was decided.",
        keywords=("onboarding", "launch"),
        screen="onboarding.go_live", permission=PERM_GO_LIVE_VIEW,
    ),
)
