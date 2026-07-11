"""Constants, enums, and permission keys for vs_workflow."""
from django.db import models

# Persisted lifecycle states for a workflow instance.
class WorkflowInstanceStatus(models.TextChoices):
    DRAFT       = "DRAFT",       "Draft"
    SUBMITTED   = "SUBMITTED",   "Submitted"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    RETURNED    = "RETURNED",    "Returned to Requester"
    APPROVED    = "APPROVED",    "Approved"
    REJECTED    = "REJECTED",    "Rejected"
    WITHDRAWN   = "WITHDRAWN",   "Withdrawn"
    CANCELLED   = "CANCELLED",   "Cancelled (Admin)"

# Instance statuses after which no normal approver action may continue.
WORKFLOW_TERMINAL_STATUSES = {
    WorkflowInstanceStatus.APPROVED, WorkflowInstanceStatus.REJECTED,
    WorkflowInstanceStatus.WITHDRAWN, WorkflowInstanceStatus.CANCELLED,
}

# Persisted lifecycle states for a single stage attempt.
class WorkflowStageStatus(models.TextChoices):
    PENDING  = "PENDING",  "Pending"
    ACTIVE   = "ACTIVE",   "Active"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    RETURNED = "RETURNED", "Returned to Requester"
    SKIPPED  = "SKIPPED",  "Skipped"

# Approver/requester actions recorded against a stage.
class WorkflowStageAction(models.TextChoices):
    APPROVED  = "APPROVED",  "Approved"
    REJECTED  = "REJECTED",  "Rejected"
    RETURNED  = "RETURNED",  "Returned to Requester"
    WITHDRAWN = "WITHDRAWN", "Withdrawn by Requester"

# Rules for deciding when an approval stage is complete.
class StageAdvanceRule(models.TextChoices):
    UNANIMOUS = "UNANIMOUS", "Unanimous (all must approve)"
    QUORUM    = "QUORUM",    "Quorum (N of M must approve)"
    ANY       = "ANY",       "Any one approver"

# Rejection handling policy for an approval stage.
class StageOnRejection(models.TextChoices):
    TERMINAL            = "TERMINAL",            "Rejection terminates the workflow"
    RETURN_TO_REQUESTER = "RETURN_TO_REQUESTER", "Rejection returns to requester"

# Scope used when resolving RBAC permission-based approvers.
class ApproverScope(models.TextChoices):
    BRANCH   = "BRANCH",   "Branch-scoped"
    SCHOOL   = "SCHOOL",   "School-scoped"
    PLATFORM = "PLATFORM", "Platform-scoped"

# Audit event vocabulary written by workflow services.
class AuditEventType(models.TextChoices):
    INSTANCE_SUBMITTED        = "INSTANCE_SUBMITTED",        "Instance submitted"
    INSTANCE_WITHDRAWN        = "INSTANCE_WITHDRAWN",        "Instance withdrawn by requester"
    INSTANCE_CANCELLED        = "INSTANCE_CANCELLED",        "Instance cancelled by admin"
    INSTANCE_APPROVED         = "INSTANCE_APPROVED",         "Instance fully approved"
    INSTANCE_REJECTED         = "INSTANCE_REJECTED",         "Instance terminally rejected"
    INSTANCE_RETURNED         = "INSTANCE_RETURNED",         "Instance returned to requester"
    INSTANCE_RESUBMITTED      = "INSTANCE_RESUBMITTED",      "Instance resubmitted after return"
    STAGE_ACTIVATED           = "STAGE_ACTIVATED",           "Stage became active"
    STAGE_APPROVED            = "STAGE_APPROVED",            "Stage approved"
    STAGE_REJECTED            = "STAGE_REJECTED",            "Stage rejected"
    STAGE_SKIPPED_NO_APPROVER = "STAGE_SKIPPED_NO_APPROVER", "Stage auto-skipped (no eligible approvers)"
    STAGE_SKIPPED_CONDITION   = "STAGE_SKIPPED_CONDITION",   "Stage skipped (conditional branch)"
    APPROVER_ACTED            = "APPROVER_ACTED",            "An approver recorded a vote"
    ACTION_REVERSED           = "ACTION_REVERSED",           "Admin reversed an approver action"
    ROUTE_EVALUATED           = "ROUTE_EVALUATED",           "Route recomputed at stage transition"

# Stage categories used by routing.
class StageKind(models.TextChoices):
    APPROVAL = "APPROVAL", "Approval"
    BRANCH   = "BRANCH",   "Branch"

class ApproverSource(models.TextChoices):
    """
    How a stage resolves its eligible approvers.

    RBAC_PERMISSION is the original (and default) strategy: anyone holding
    `approver_permission_key` within `approver_scope`. ORGANOGRAM is an
    additive, opt-in strategy that climbs the CX organogram relative to the
    requester. The two are mutually exclusive per stage.
    """
    RBAC_PERMISSION = "RBAC_PERMISSION", "RBAC permission holders (default)"
    ORGANOGRAM      = "ORGANOGRAM",      "Organogram (relative to requester)"

class OrganogramTarget(models.TextChoices):
    """The climb mode used when ApproverSource is ORGANOGRAM."""
    DIRECT_MANAGER   = "DIRECT_MANAGER",   "Requester's direct manager"
    N_LEVELS_UP      = "N_LEVELS_UP",      "N levels up the reporting chain"
    DEPARTMENT_HEAD  = "DEPARTMENT_HEAD",  "Head of requester's department"
    SPECIFIC_POSITION = "SPECIFIC_POSITION", "Holder(s) of a specific position"

# Permission keys (vs_rbac contract)
# RBAC keys that protect workflow template and instance operations.
PERM_TEMPLATE_MANAGE = "workflow.template.manage"
PERM_TEMPLATE_VIEW   = "workflow.template.view"
PERM_INSTANCE_SUBMIT = "workflow.instance.submit"
PERM_INSTANCE_VIEW   = "workflow.instance.view"
PERM_INSTANCE_CANCEL = "workflow.instance.cancel"
PERM_ACTION_REVERSE  = "workflow.action.reverse"

# Notification event keys
# Notification event keys emitted for workflow lifecycle transitions.
NOTIF_EVENT_SUBMITTED       = "workflow.submitted"
NOTIF_EVENT_STAGE_ACTIVATED = "workflow.stage_activated"
NOTIF_EVENT_STAGE_APPROVED  = "workflow.stage_approved"
NOTIF_EVENT_STAGE_REJECTED  = "workflow.stage_rejected"
NOTIF_EVENT_RETURNED        = "workflow.returned"
NOTIF_EVENT_APPROVED        = "workflow.approved"
NOTIF_EVENT_REJECTED        = "workflow.rejected"
NOTIF_EVENT_WITHDRAWN       = "workflow.withdrawn"
NOTIF_EVENT_CANCELLED       = "workflow.cancelled"
NOTIF_EVENT_FINAL_APPROVED  = "workflow.final_approved"
NOTIF_EVENT_KEYS = [
    NOTIF_EVENT_SUBMITTED, NOTIF_EVENT_STAGE_ACTIVATED, NOTIF_EVENT_STAGE_APPROVED,
    NOTIF_EVENT_STAGE_REJECTED, NOTIF_EVENT_RETURNED, NOTIF_EVENT_APPROVED,
    NOTIF_EVENT_REJECTED, NOTIF_EVENT_WITHDRAWN, NOTIF_EVENT_CANCELLED,
    NOTIF_EVENT_FINAL_APPROVED,
]

# The lifecycle points the engine actually emits (routing.py). The other keys
# above stay reserved for future wiring; dispatch_notification refuses keys
# outside NOTIF_EVENT_KEYS either way.
NOTIF_WIRED_EVENT_KEYS = [
    NOTIF_EVENT_STAGE_ACTIVATED,   # → the activated stage's approvers
    NOTIF_EVENT_RETURNED,          # → requester
    NOTIF_EVENT_REJECTED,          # → requester
    NOTIF_EVENT_FINAL_APPROVED,    # → requester
]

# Condition operators (fixed set)
# Condition operators supported by route evaluation.
CONDITION_OP_EQ       = "eq"
CONDITION_OP_NE       = "ne"
CONDITION_OP_GT       = "gt"
CONDITION_OP_GTE      = "gte"
CONDITION_OP_LT       = "lt"
CONDITION_OP_LTE      = "lte"
CONDITION_OP_IN       = "in"
CONDITION_OP_NOT_IN   = "not_in"
CONDITION_OP_CONTAINS = "contains"
CONDITION_OPERATORS = {
    CONDITION_OP_EQ, CONDITION_OP_NE, CONDITION_OP_GT, CONDITION_OP_GTE,
    CONDITION_OP_LT, CONDITION_OP_LTE, CONDITION_OP_IN, CONDITION_OP_NOT_IN,
    CONDITION_OP_CONTAINS,
}
