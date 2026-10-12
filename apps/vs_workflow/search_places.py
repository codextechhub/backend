"""The approval screens the search bar can route to.

Approvals are core: every tenant has them whatever its plan includes, so no
place here names a capability.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

from .constants import PERM_GROUP_VIEW, PERM_INSTANCE_VIEW, PERM_TEMPLATE_VIEW

register(
    Place(
        kind=ACTION, key="workflow.notification_settings", label="Workflow notifications",
        description="Whether approvals notify anybody.",
        keywords=("approval", "notifications"),
        screen="workflow.notification_settings", permission=PERM_TEMPLATE_VIEW,
    ),
    Place(
        kind=ACTION, key="workflow.templates", label="Approval workflows",
        description="Who approves what, stage by stage.",
        keywords=("approvers", "workflow"),
        screen="workflow.templates", permission=PERM_TEMPLATE_VIEW,
    ),
    Place(
        kind=ACTION, key="workflow.pending", label="My pending approvals",
        description="Requests waiting for your decision.",
        keywords=("approve", "inbox"),
        screen="workflow.pending",
    ),
    Place(
        kind=ACTION, key="workflow.submitted", label="My submissions",
        description="Requests you raised and where each one stands.",
        keywords=("approval",),
        screen="workflow.submitted",
    ),
    Place(
        kind=ACTION, key="workflow.delegations", label="Delegations",
        description="Who approves on your behalf while you are away.",
        keywords=("approval", "out of office", "deputy"),
        screen="workflow.delegations",
    ),
    Place(
        kind=ACTION, key="workflow.instances", label="Manage approvals",
        description="Every document in an approval and where it stands.",
        keywords=("approval", "in flight"),
        screen="workflow.instances", permission=PERM_INSTANCE_VIEW,
    ),
    Place(
        kind=ACTION, key="workflow.team_load", label="Approval team load",
        description="How many approvals wait on each approver.",
        keywords=("approval", "workload"),
        screen="workflow.team_load", permission=PERM_INSTANCE_VIEW,
    ),
    Place(
        kind=ACTION, key="workflow.approver_groups", label="Approvers",
        description="Named approver groups and dynamic roles.",
        keywords=("approval", "approver groups", "dynamic roles"),
        screen="workflow.approver_groups",
        permission=(PERM_GROUP_VIEW, "workflow.template.update"),
    ),
)
