"""The console's own screens the search bar can route to.

Every key here is the platform's own, so only platform staff see these places.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

register(
    Place(
        kind=ACTION, key="console.dashboard", label="Console home",
        description="The platform at a glance.", keywords=("dashboard", "overview"),
        screen="console.dashboard", permission="platform.dashboard.view",
    ),
    Place(
        kind=ACTION, key="console.schools", label="School onboarding",
        description="Every school on the platform and where its setup stands.",
        keywords=("schools", "tenants", "onboarding"),
        screen="console.schools", permission="platform.schools.view",
    ),
    Place(
        kind=ACTION, key="console.documents", label="Requirements documents",
        description="Every version of the module and functional requirements documents.",
        keywords=("frd", "mrd", "requirements"),
        screen="console.documents", permission="platform.documents.view",
    ),
)
