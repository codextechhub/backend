"""The support screen the search bar can route to.

Every signed-in member may raise a ticket and read their own, so the place
needs no key. Support is core (:mod:`vs_rbac.capability_map`). The how-to
guides live in the frontend, which fills the search bar's guides group itself.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

register(
    Place(
        kind=ACTION, key="support.tickets", label="Support tickets",
        description="Ask for help and follow your requests.",
        keywords=("help", "support centre", "contact support"),
        screen="support.tickets",
    ),
)
