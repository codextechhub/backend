"""
Data models for vs_workflow — 8 models.

WorkflowTemplate      — reusable blueprint.
WorkflowStage         — one node (APPROVAL or BRANCH).
WorkflowRoutePath     — directed edge between stages, optionally condition-guarded.
WorkflowInstance      — one running execution against one business document.
WorkflowStageInstance — per-instance, per-stage lifecycle record.
WorkflowStageApprover — audit-grade snapshot of who was eligible when a stage activated.
WorkflowStageAction   — every recorded approver vote, including reversals.
ApprovalDelegation    — date-ranged delegation of approval authority.
WorkflowAuditLog      — append-only structured event log.
"""

import shortuuid
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q  # used in WorkflowStageAction constraint

from vs_workflow.constants import (
    ApproverScope,
    AuditEventType,
    StageAdvanceRule,
    StageKind,
    StageOnRejection,
    WorkflowInstanceStatus,
    WorkflowStageStatus,
    WorkflowStageAction as WorkflowStageActionEnum,
    WORKFLOW_TERMINAL_STATUSES,
)


def _short_id():
    return shortuuid.ShortUUID().random(length=8)


class WorkflowTemplate(models.Model):
    """Reusable blueprint defining the approval stages and routing for a document type.

    A template is identified by the combination of (school, document_type, code).
    Multiple templates can exist for the same document type under different codes,
    enabling different approval paths (e.g. ``standard`` vs ``high_value``).
    Publishing the same key again updates the template in place — no versioning.

    Attributes:
        school: Optional school scope. Null means the template applies platform-wide.
        branch: Optional branch scope. Set when a branch admin creates a template that
            applies only to their branch. Takes precedence over the school-level template
            when a document originates from the same branch.
        document_type: Dotted string identifying the document kind (e.g. ``leave.request``).
        code: Slug identifying this template variant (e.g. ``standard``, ``high_value``).
        notification_events: Dict of event keys to booleans controlling which lifecycle
            events trigger notifications.
        created_by: The admin user who last published this template.
    """

    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    school = models.ForeignKey(
        "vs_schools.School", on_delete=models.PROTECT,
        null=True, blank=True, related_name="workflow_templates",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="workflow_templates",
    )
    document_type = models.CharField(max_length=100, db_index=True)
    code = models.SlugField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    notification_events = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["school", "branch", "document_type", "code"],
                name="uniq_workflow_template",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "branch", "document_type"]),
        ]

    def __str__(self):
        return f"{self.code} ({self.document_type})"


class WorkflowStage(models.Model):
    """A single step within a WorkflowTemplate.

    Stages are either ``APPROVAL`` (pauses for approver votes) or ``BRANCH``
    (a routing-only decision point that the engine passes through instantly).

    Attributes:
        template: The parent template this stage belongs to.
        code: Unique slug within the template (e.g. ``line-manager``, ``finance``).
        kind: ``APPROVAL`` or ``BRANCH``. BRANCH stages are auto-skipped by the engine.
        order: Ascending integer used for linear routing when no routes are defined.
        approver_permission_key: RBAC permission key used to resolve eligible approvers.
        approver_scope: ``BRANCH``, ``SCHOOL``, or ``PLATFORM`` — narrows the RBAC lookup.
        advance_rule: ``UNANIMOUS``, ``QUORUM``, or ``ANY`` — how many approvals advance the stage.
        quorum_count: Minimum approvals required when advance_rule is ``QUORUM``.
        on_rejection: ``TERMINAL`` ends the workflow; ``RETURN_TO_REQUESTER`` sends it back.
        skip_if_no_approvers: Auto-skip this stage if no eligible approvers are found.
        inclusion_condition: JSON condition evaluated against the document at runtime.
            The stage is skipped entirely if it evaluates to False.
    """

    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name="stages")
    code = models.SlugField(max_length=80)
    label = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=StageKind.choices, default=StageKind.APPROVAL)
    order = models.PositiveIntegerField(default=0)
    approver_permission_key = models.CharField(max_length=150, blank=True, default="")
    approver_scope = models.CharField(max_length=20, choices=ApproverScope.choices,
                                      default=ApproverScope.SCHOOL)
    advance_rule = models.CharField(max_length=20, choices=StageAdvanceRule.choices,
                                    default=StageAdvanceRule.UNANIMOUS)
    quorum_count = models.PositiveIntegerField(default=0)
    on_rejection = models.CharField(max_length=30, choices=StageOnRejection.choices,
                                    default=StageOnRejection.TERMINAL)
    skip_if_no_approvers = models.BooleanField(default=True)
    # Declarative inclusion condition — stage only runs when this evaluates True.
    # {"op": "gte", "field": "amount", "value": 100000} or {"fn": "module.fn_name"}
    inclusion_condition = models.JSONField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template", "code"], 
                                    name="uniq_stage_code_per_template"),
        ]
        ordering = ["order"]

    def __str__(self):
        return f"{self.label} [{self.template.code}]"


class WorkflowRoutePath(models.Model):
    """Directed edge between two stages within a template.

    Routes are evaluated in ascending ``order``. The first route whose condition
    matches the document is followed. If no routes are defined, the engine falls
    back to linear ordering by ``WorkflowStage.order``.

    Attributes:
        template: The parent template this route belongs to.
        from_stage: Source stage. Null means this is the entry edge into the template.
        to_stage: Destination stage. Null means the workflow ends as APPROVED here.
        order: Evaluation order among routes sharing the same from_stage.
        condition: JSON condition evaluated against the document. Null always matches.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name="routes")
    from_stage = models.ForeignKey(WorkflowStage, on_delete=models.CASCADE,
                                   null=True, blank=True, related_name="outbound_routes")
    to_stage = models.ForeignKey(WorkflowStage, on_delete=models.CASCADE,
                                 null=True, blank=True, related_name="inbound_routes")
    order = models.PositiveIntegerField(default=0)
    condition = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["from_stage_id", "order"]

    def __str__(self):
        f = self.from_stage.code if self.from_stage else "ENTRY"
        t = self.to_stage.code if self.to_stage else "EXIT"
        return f"{f} -> {t}"


class WorkflowInstanceQuerySet(models.QuerySet):
    """Custom QuerySet for WorkflowInstance with convenience filter methods.

    Methods:
        for_school: Filter instances belonging to a specific school.
        for_branch: Filter instances belonging to a specific branch.
        for_document: Filter instances tracking a specific business document.
        active: Exclude instances that have reached a terminal status.
    """

    def for_school(self, school):
        return self.filter(school=school)

    def for_branch(self, branch):
        return self.filter(branch=branch)

    def for_document(self, document):
        ct = ContentType.objects.get_for_model(type(document))
        return self.filter(document_content_type=ct, document_object_id=str(document.pk))

    def active(self):
        return self.exclude(status__in=list(WORKFLOW_TERMINAL_STATUSES))


class WorkflowInstance(models.Model):
    """One live execution of a WorkflowTemplate against a single business document.

    Created the moment a document is submitted for approval. Tracks the full
    lifecycle from SUBMITTED through to a terminal status. This is the engine's
    authoritative record of workflow state for a given document.

    Attributes:
        school: Optional school scope, copied from the document at submission time.
        branch: Optional branch scope, copied from the document at submission time.
        template: The blueprint this instance is running against.
        document_content_type: ContentType of the related business document.
        document_object_id: Primary key of the related business document.
        document: GenericForeignKey resolving to the actual business document object.
        document_type: Denormalised copy of the type string for fast filtering without a join.
        status: Current lifecycle status (see WorkflowInstanceStatus).
        requested_by: The user who submitted the document for approval.
        current_stage: The stage the engine is currently waiting on. Null when terminal.
        state_version: Incremented on every status transition; useful for stale-read detection.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    school = models.ForeignKey("vs_schools.School", on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="workflow_instances")
    branch = models.ForeignKey("vs_schools.Branch", on_delete=models.PROTECT,
                               related_name="workflow_instances", null=True, blank=True)
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.PROTECT, related_name="instances")
    # Generic FK to the business document (E1).
    document_content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    document_object_id = models.CharField(max_length=64, db_index=True)
    document = GenericForeignKey("document_content_type", "document_object_id")
    # Denormalised for fast filtering — avoids a join through contenttypes.
    document_type = models.CharField(max_length=100, db_index=True)
    document_summary = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=30, choices=WorkflowInstanceStatus.choices,
                              default=WorkflowInstanceStatus.DRAFT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="submitted_workflow_instances")
    current_stage = models.ForeignKey(WorkflowStage, on_delete=models.PROTECT,
                                      null=True, blank=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    state_version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkflowInstanceQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["school", "document_type", "status"]),
            models.Index(fields=["document_content_type", "document_object_id"]),
            models.Index(fields=["requested_by", "status"]),
        ]

    def __str__(self):
        return f"{self.document_type}#{self.document_object_id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in WORKFLOW_TERMINAL_STATUSES


class WorkflowStageInstance(models.Model):
    """Per-instance record of a single stage activation.

    Created each time the engine activates a stage for a given WorkflowInstance.
    If the same stage is revisited after a RETURN cycle, a new row is written with
    a higher attempt number. Previous attempt rows are retained for audit purposes.

    Attributes:
        instance: The parent workflow instance this stage activation belongs to.
        stage: The template stage definition being executed.
        status: ``PENDING`` → ``ACTIVE`` → ``APPROVED`` / ``REJECTED`` / ``SKIPPED``.
        attempt: Starts at 1; incremented each time this stage is re-entered after a RETURN.
        skip_reason: Short code describing why the stage was auto-skipped, if applicable.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    instance = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE,
                                 related_name="stage_instances")
    stage = models.ForeignKey(WorkflowStage, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=20, choices=WorkflowStageStatus.choices,
                              default=WorkflowStageStatus.PENDING)
    activated_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.CharField(max_length=100, blank=True, default="")
    # Incremented each time this stage is revisited after a RETURN.
    attempt = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["instance", "status"]),
        ]


class WorkflowStageApprover(models.Model):
    """Point-in-time snapshot of who was eligible to act on a stage at activation.

    Rows are written once when a stage is activated and never updated thereafter.
    This preserves the eligible approver list as it existed at that exact moment,
    even if RBAC roles change later. Active delegation entries are expanded and
    included here as separate rows with on_behalf_of set.

    Attributes:
        stage_instance: The stage activation this snapshot belongs to.
        user: The eligible approver (may be a delegate acting on behalf of another user).
        on_behalf_of: Set when this approver is a delegate acting for another user.
        attempt: Mirrors the attempt number of the parent stage_instance row.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    stage_instance = models.ForeignKey(WorkflowStageInstance, on_delete=models.CASCADE,
                                       related_name="eligible_approvers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    on_behalf_of = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     null=True, blank=True, related_name="+")
    attempt = models.PositiveIntegerField(default=1)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["stage_instance", "attempt"])]


class WorkflowStageAction(models.Model):
    """A recorded approver vote or admin reversal. Append-only.

    Every decision taken on a stage — APPROVED, REJECTED, or RETURNED — writes a
    row here. Reversals never delete or modify the original row; instead a new row
    is created with is_reversal_of pointing to the original, and the original receives
    a reversed_at timestamp.

    Attributes:
        stage_instance: The stage activation this action belongs to.
        actor: The user who performed the action.
        on_behalf_of: Set when the actor is a delegate acting for another user.
        action: ``APPROVED``, ``REJECTED``, or ``RETURNED``.
        attempt: Mirrors the attempt number of the parent stage_instance row.
        is_reversal_of: Points to the original action row if this row is a reversal.
        reversed_at: Timestamp stamped on the original row when it is reversed.
        reversed_by: The admin who performed the reversal.
        reversal_reason: Explanation recorded at the time of reversal.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    stage_instance = models.ForeignKey(WorkflowStageInstance, on_delete=models.PROTECT,
                                       related_name="actions")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               related_name="workflow_actions")
    on_behalf_of = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     null=True, blank=True, related_name="+")
    action = models.CharField(max_length=20, choices=WorkflowStageActionEnum.choices)
    comment = models.TextField(blank=True, default="")
    attempt = models.PositiveIntegerField(default=1)
    # Reversal tracking
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="+")
    reversal_reason = models.TextField(blank=True, default="")
    is_reversal_of = models.OneToOneField("self", on_delete=models.PROTECT,
                                          null=True, blank=True, related_name="reversal")
    acted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stage_instance", "actor", "attempt"],
                condition=Q(is_reversal_of__isnull=True),
                name="uniq_live_action_per_actor_per_attempt",
            ),
        ]
        indexes = [models.Index(fields=["stage_instance", "attempt"])]


class ApprovalDelegation(models.Model):
    """Date-ranged grant of approval authority from one user to another.

    While a delegation is active (starts_at <= now <= ends_at and not revoked),
    the delegate appears in the eligible approver list for any stage that the
    delegator would otherwise qualify for.

    Attributes:
        school: Optional school scope. Null means the delegation applies platform-wide.
        delegator: The user granting their approval authority.
        delegate: The user receiving the approval authority.
        starts_at: Datetime from which the delegation becomes effective.
        ends_at: Datetime after which the delegation expires.
        document_type: Limits the delegation to a specific document type. Blank means all types.
        exclusive: If True, the delegator is removed from the eligible list for the duration —
            only the delegate can approve, not both.
        revoked_at: Set when an admin or the delegator manually revokes the delegation early.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    school = models.ForeignKey("vs_schools.School", on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="approval_delegations")
    delegator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="delegations_granted")
    delegate = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="delegations_received")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    document_type = models.CharField(max_length=100, blank=True, default="")
    exclusive = models.BooleanField(default=False,
        help_text="If True, delegator is excluded from eligibility for the duration.")
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["delegator", "starts_at", "ends_at"]),
            models.Index(fields=["delegate", "starts_at", "ends_at"]),
        ]


class WorkflowAuditLog(models.Model):
    """Append-only log of every material event in a workflow instance's lifecycle.

    One row is written for each significant engine event — submission, stage activation,
    vote, approval, rejection, and so on. Rows are never updated or deleted. Used for
    auditing, debugging, and driving notification dispatch.

    Attributes:
        instance: The workflow instance this event belongs to.
        event_type: Categorised event key (see AuditEventType).
        stage_instance: The stage activation involved in the event, if applicable.
        actor: The user who triggered the event, if applicable.
        context: Freeform JSON payload carrying event-specific detail.
        message: Optional human-readable summary of the event.
        occurred_at: Timestamp of when the event was recorded.
    """
    id = models.CharField(primary_key=True, max_length=8, default=_short_id, editable=False)
    instance = models.ForeignKey(WorkflowInstance, on_delete=models.PROTECT, related_name="audit_logs")
    event_type = models.CharField(max_length=50, choices=AuditEventType.choices, db_index=True)
    stage_instance = models.ForeignKey(WorkflowStageInstance, on_delete=models.PROTECT,
                                       null=True, blank=True, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               null=True, blank=True, related_name="+")
    context = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["instance", "occurred_at"]),
            models.Index(fields=["instance", "event_type"]),
        ]
        ordering = ["-occurred_at"]
