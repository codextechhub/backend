"""The platform audit screens the search bar can route to.

Every key here is the platform's own, so only platform staff see these places,
in the console. Audit is core (:mod:`vs_rbac.capability_map`).
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

AUDIT_VIEW = "platform.audit.view"

register(
    Place(
        kind=ACTION, key="audit.dashboard", label="Security dashboard",
        description="Sign-ins, lockouts and changes across the platform at a glance.",
        keywords=("audit", "security"),
        screen="audit.dashboard", permission=AUDIT_VIEW,
    ),
    Place(
        kind=ACTION, key="audit.events", label="Audit events",
        description="Every recorded change on the platform, filtered and searched.",
        keywords=("audit log", "history", "events explorer"),
        screen="audit.events", permission=AUDIT_VIEW,
    ),
    Place(
        kind=ACTION, key="audit.entity_trails", label="Entity trails",
        description="The full history of one record.", keywords=("audit", "history"),
        screen="audit.entity_trails", permission=AUDIT_VIEW,
    ),
    Place(
        kind=ACTION, key="audit.exports", label="Audit exports",
        description="Audit trails exported to a file.", keywords=("audit", "download"),
        screen="audit.exports", permission="platform.audit.export",
    ),
    Place(
        kind=ACTION, key="audit.compliance_rules", label="Compliance rules",
        description="How long audit records are kept and what is flagged.",
        keywords=("audit", "retention"),
        screen="audit.compliance_rules", permission=AUDIT_VIEW,
    ),
)
