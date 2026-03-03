from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


# -----------------------------------------------------------------------------
# Shared base + helpers
# -----------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    """Common created/updated timestamps."""
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# More strict than Django's default SlugField in practice (still URL-safe)
slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message="Slug must be lowercase letters/numbers separated by single hyphens.",
)


RESERVED_TENANT_SLUGS = {
    "admin", "api", "auth", "login", "logout", "www", "root", "static",
    "media", "health", "status", "support", "system", "internal",
}


# -----------------------------------------------------------------------------
# Enumerations
# -----------------------------------------------------------------------------

class InstitutionStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active",
    PENDING = "PENDING", "Pending Activation",
    SUSPENDED = "SUSPENDED", "Suspended"
    INACTIVE = "INACTIVE", "Inactive"


class ProvisioningStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    ROLLED_BACK = "ROLLED_BACK", "Rolled Back"
    ROLLBACK_FAILED = "ROLLBACK_FAILED", "Rollback Failed"


class InviteStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class OperationType(models.TextChoices):
    SUSPEND = "SUSPEND", "Suspend"
    REACTIVATE = "REACTIVATE", "Reactivate"
    SOFT_DELETE = "SOFT_DELETE", "Soft Delete"
    HARD_DELETE = "HARD_DELETE", "Hard Delete"
    RESET = "RESET", "Reset Config"


class OperationOutcome(models.TextChoices):
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class PlanTier(models.TextChoices):
    STARTER = "STARTER", "Starter"
    PRO = "PRO", "Pro"
    ENTERPRISE = "ENTERPRISE", "Enterprise"


# -----------------------------------------------------------------------------
# Core Entities
# -----------------------------------------------------------------------------

class Institution(TimeStampedModel):
    """
    Institution model representing the core institution entity in a multi-institution SaaS application.

    Derived from system diagrams as the TENANT (core) entity, responsible for holding
    canonical institution identity, lifecycle management, and key metadata.

    Attributes:
        institution_name (str): Display name of the institution.
        slug (str): URL-safe unique identifier for the institution (lowercase, hyphen-separated).
        institution_group (str): Optional grouping identifier (e.g., subsidiary/parent group).
        category (str): Classification of institution (e.g., School, College, Organization).
        institution_type (str): Type of institution (e.g., Public, Private).
        plan_tier (str): Subscription tier level (e.g., Starter, Pro, Enterprise).
        country (str): Country where the institution is located.
        region (str): Geographic region of the institution.
        timezone (str, optional): IANA timezone identifier for the institution.
        currency (str, optional): ISO 4217 currency code for billing/transactions.
        primary_contact_name (str, optional): Name of primary business contact.
        primary_contact_email (str, optional): Email of primary business contact.
        primary_contact_phone (str, optional): Phone of primary business contact.
        status (str): Current lifecycle status of the institution (see InstitutionStatus choices).
        activated_at (datetime, optional): Timestamp when institution transitioned to LIVE status.
        deleted_at (datetime, optional): Timestamp of soft-delete operation, null if active.

    Methods:
        mark_live(): Transition institution to LIVE status.
        suspend(): Transition institution to SUSPENDED status with reason.
        reactivate(): Restore institution from suspended/inactive state.
        soft_delete(): Perform soft-delete operation with audit trail.
        transition(): Core method handling state transitions with validation and event recording.
        clean(): Validate slug against reserved slugs list.

    Meta:
        - Database table: "institution"
        - Indexes on: slug, (status, created_at)
        - Constraint: slug cannot be empty string
        - Includes soft-delete support via deleted_at field
        - Inherits created_at, updated_at from TimeStampedModel

    Note:
        Heavy business logic and advanced state transition policies are delegated to
        service layer components (e.g., LifecycleService). This model provides lightweight
        domain helpers and audit trails via InstitutionLifecycleEvent.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(
        primary_key=True,
        max_length=80,
        unique=True,
        validators=[slug_validator],
        help_text="URL-safe unique institution identifier. Lowercase, hyphen-separated.",
    )
    group = models.CharField(max_length=80, blank=True, default="")  # e.g., subsidiary/parent group
    email = models.EmailField(blank=True, default="")  # Optional general contact email for the institution
    website = models.URLField(blank=True, default="")  # Optional official website URL for the institution
    phone_number = models.CharField(max_length=15, blank=True, default="")  # Optional general contact phone number

    category = models.CharField(max_length=80)            # e.g., School/College/Org
    _type = models.CharField(max_length=80)    # e.g., Public/Private
    plan_tier = models.CharField(max_length=80, default=PlanTier.STARTER, choices=PlanTier.choices)           # e.g., Starter/Pro/Enterprise

    country = models.CharField(max_length=80)
    state = models.CharField(max_length=120)
    city = models.CharField(max_length=120)

    timezone = models.CharField(max_length=64, blank=True, default="")
    currency = models.CharField(max_length=8, blank=True, default="")

    status = models.CharField(
        max_length=32,
        choices=InstitutionStatus.choices,
        default=InstitutionStatus.PENDING,
        db_index=True,
    )

    activated_at = models.DateTimeField(null=True, blank=True)

    # Soft-delete markers (kept explicit for query clarity)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=~Q(slug=""),
                name="slug_not_empty",
            ),
        ]

    def clean(self):
        super().clean()
        slug = (self.slug or "").strip().lower()
        if slug in RESERVED_TENANT_SLUGS:
            raise ValidationError({"slug": "This slug is reserved. Choose another."})

    # --- Domain-ish helpers (kept lightweight; services can enforce heavy rules) ---

    def mark_live(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=InstitutionStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def suspend(self, *, actor_id: str, reason: str):
        self.transition(to_state=InstitutionStatus.SUSPENDED, actor_id=actor_id, reason=reason)

    def reactivate(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=InstitutionStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def soft_delete(self, *, actor_id: str, reason: str):
        self.deleted_at = timezone.now()
        self.transition(to_state=InstitutionStatus.INACTIVE, actor_id=actor_id, reason=reason)

    def transition(self, *, to_state: str, actor_id: str, reason: str = ""):
        """Records lifecycle event and updates current status."""
        from_state = self.status
        if from_state == to_state:
            return

        # Minimal guardrails (full policy typically lives in LifecycleService)
        # if from_state == InstitutionStatus.DELETED_SOFT:
        #     raise ValidationError("Cannot transition a soft-deleted institution without restore policy.")
        # if from_state == InstitutionStatus.LOCKED and to_state not in (InstitutionStatus.LOCKED,):
        #     # You might allow super-admin override in a service layer.
        #     raise ValidationError("Institution is locked. Resolve provisioning/ops issue first.")

        self.status = to_state
        if to_state == InstitutionStatus.LIVE and self.activated_at is None:
            self.activated_at = timezone.now()

        self.save(update_fields=["status", "activated_at", "updated_at", "deleted_at"])

        InstitutionLifecycleEvent.objects.create(
            institution=self,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            reason=reason or "",
        )


class InstitutionBranding(TimeStampedModel):
    """
    Model for managing institution-specific branding and theming configuration.

    This model stores customizable branding elements for each institution, including
    logos, color schemes, and theme settings. Each institution has exactly one branding
    configuration (one-to-one relationship).

    Attributes:
        institution (OneToOneField): Reference to the Institution instance. Cascade delete
            ensures branding is removed when institution is deleted.
    """

    institution = models.OneToOneField(
        Institution,
        on_delete=models.CASCADE,
        related_name="branding",
    )

    logo = models.ImageField(upload_to="institution_logos/", null=True, blank=True)


class InstitutionModuleSetting(TimeStampedModel):
    """
    Represents the settings for modules associated with an institution.

    This model allows for multiple settings per institution, with uniqueness enforced
    for each combination of institution and module key. The settings can be enabled 
    or disabled and may have an effective date from which they apply.

    Attributes:
        institution (ForeignKey): A reference to the Institution this setting belongs to.
        module_key (str): A key representing the module (e.g., STUDENTS, FINANCE, ATTENDANCE).
        enabled (bool): Indicates whether the module is enabled for the institution.
        effective_from (datetime): The date and time from which this setting is effective.
        changed_by_actor_id (str): An identifier for the actor who last changed this setting.

    Meta:
        db_table (str): The name of the database table for this model.
        constraints (list): Unique constraint to ensure that each institution can have only one setting per module key.
        indexes (list): Indexes to optimize queries based on institution and module key, as well as institution and enabled status.
    """

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="module_settings",
    )

    module_key = models.CharField(max_length=80)  # e.g., STUDENTS, FINANCE, ATTENDANCE
    enabled = models.BooleanField(default=False)

    effective_from = models.DateTimeField(null=True, blank=True)
    changed_by_actor_id = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "module_key"],
                name="uniq_module_key_per_institution",
            ),
        ]
        indexes = [
            models.Index(fields=["institution", "module_key"]),
            models.Index(fields=["institution", "enabled"]),
        ]


class InstitutionLifecycleEvent(TimeStampedModel):
    """
    Represents an event in the lifecycle of an institution, capturing state transitions.

    Attributes:
        institution (ForeignKey): A reference to the related Institution.
        from_state (str): The state from which the institution is transitioning.
        to_state (str): The state to which the institution is transitioning.
        actor_id (str): Identifier for the actor responsible for the transition.
        reason (str): An optional text field providing the reason for the transition.
        occurred_at (datetime): The timestamp when the event occurred, indexed for performance.

    Meta:
        db_table (str): Specifies the database table name for this model.
        indexes (list): Defines indexes for efficient querying on institution and state transitions.
    """

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )

    from_state = models.CharField(max_length=32, choices=InstitutionStatus.choices)
    to_state = models.CharField(max_length=32, choices=InstitutionStatus.choices)

    actor_id = models.CharField(max_length=120)
    reason = models.TextField(blank=True, default="")

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "occurred_at"]),
            models.Index(fields=["institution", "to_state"]),
        ]


class ProvisioningRecord(TimeStampedModel):
    """
    Represents the provisioning attempts for an institution.

    This model maintains a one-to-one relationship with the Institution model, 
    tracking the status and details of the latest provisioning attempt.

    Attributes:
        institution (OneToOneField): A reference to the associated Institution instance. 
            Deletion cascades to remove the provisioning record when the institution is deleted.
        provisioning_status (str): Current status of the provisioning attempt (e.g., QUEUED, RUNNING, SUCCEEDED, FAILED).
        last_error_code (str): Code representing the last error encountered during provisioning, if any.
        last_error_message (str): Detailed message regarding the last error encountered during provisioning.
        queued_at (datetime): Timestamp indicating when the provisioning attempt was queued.
        started_at (datetime, optional): Timestamp indicating when the provisioning attempt started.
        completed_at (datetime, optional): Timestamp indicating when the provisioning attempt completed.
        rollback_status (str): Status of the rollback operation if the provisioning attempt failed.
        rollback_completed_at (datetime, optional): Timestamp indicating when the rollback was completed, if applicable.

    Meta:
        db_table (str): Specifies the name of the database table for this model.
        indexes (list): Indexes to optimize queries based on institution and provisioning status.

    Methods:
        mark_running(): Updates the provisioning status to RUNNING and sets the started_at timestamp.
        mark_succeeded(): Updates the provisioning status to SUCCEEDED and sets the completed_at timestamp.
        mark_failed(code: str = "", message: str = ""): Updates the provisioning status to FAILED, 
            records the last error code and message, and sets the completed_at timestamp.
    """

    institution = models.OneToOneField(
        Institution,
        on_delete=models.CASCADE,
        related_name="provisioning",
    )

    provisioning_status = models.CharField(
        max_length=32,
        choices=ProvisioningStatus.choices,
        default=ProvisioningStatus.QUEUED,
        db_index=True,
    )

    last_error_code = models.CharField(max_length=80, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")

    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    rollback_status = models.CharField(
        max_length=32,
        choices=ProvisioningStatus.choices,
        blank=True,
        default="",
        help_text="Optional status reflecting rollback outcome if provisioning failed.",
    )
    rollback_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "provisioning_status"]),
        ]

    def mark_running(self):
        self.provisioning_status = ProvisioningStatus.RUNNING
        self.started_at = self.started_at or timezone.now()
        self.save(update_fields=["provisioning_status", "started_at", "updated_at"])

    def mark_succeeded(self):
        self.provisioning_status = ProvisioningStatus.SUCCEEDED
        self.completed_at = timezone.now()
        self.save(update_fields=["provisioning_status", "completed_at", "updated_at"])

    def mark_failed(self, code: str = "", message: str = ""):
        self.provisioning_status = ProvisioningStatus.FAILED
        self.last_error_code = code or ""
        self.last_error_message = message or ""
        self.completed_at = timezone.now()
        self.save(update_fields=["provisioning_status", "last_error_code", "last_error_message", "completed_at", "updated_at"])


# -----------------------------------------------------------------------------
# Primary Admin linkage
# -----------------------------------------------------------------------------

class ContactInfo(TimeStampedModel):
    """
    Represents a reusable contact record that is not necessarily linked to an authentication user.

    Attributes:
        full_name (str): The full name of the contact, limited to 120 characters.
        email (str): The email address of the contact, must be a valid email format.
        phone (str): The phone number of the contact, optional, with a maximum length of 32 characters.

    Meta:
        db_table (str): Specifies the database table name as "contact_info".
        indexes (list): Creates an index on the email field for efficient querying.
    """

    full_name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
        ]


class InstitutionPrimaryAdmin(TimeStampedModel):
    """
    InstitutionPrimaryAdmin model for managing primary administrators of institutions.

    This model establishes a one-to-one relationship between an Institution and its
    primary administrator contact. It tracks the administrative assignment, role details,
    and invitation lifecycle for the primary admin.

    Attributes:
        institution (OneToOneField): The institution this admin is assigned to.
            Cascade delete ensures cleanup when institution is removed.
        contact (ForeignKey): The contact information of the primary administrator.
            Protected delete prevents accidental removal of referenced contacts.
        role_label (CharField): Optional label describing the admin's role or title.
            Defaults to empty string.
        invite_status (CharField): Current status of the invitation to the primary admin.
            Uses InviteStatus choices with 'QUEUED' as default. Database indexed.
        invite_queued_at (DateTimeField): Timestamp when the invitation was queued.
        invite_sent_at (DateTimeField): Timestamp when the invitation was sent to the admin.

    Inherits:
        TimeStampedModel: Provides created_at and updated_at timestamps.

    Meta:
        db_table: Explicitly names the database table as 'institution_primary_admin'.
        indexes: Composite index on (institution, invite_status) for efficient queries
            on invitation status by institution.
    """

    institution = models.OneToOneField(
        Institution,
        on_delete=models.CASCADE,
        related_name="primary_admin",
    )
    contact = models.ForeignKey(
        ContactInfo,
        on_delete=models.PROTECT,
        related_name="primary_admin_for_institutions",
    )

    role_label = models.CharField(max_length=80, blank=True, default="")

    invite_status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.QUEUED,
        db_index=True,
    )
    invite_queued_at = models.DateTimeField(null=True, blank=True)
    invite_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "invite_status"]),
        ]


# -----------------------------------------------------------------------------
# Operational + Audit trails
# -----------------------------------------------------------------------------

class InstitutionOperationEvent(TimeStampedModel):
    """
    Immutable audit log for institution-level operational events.

    Captures all state-changing operations performed on institutions (suspend, reactivate, 
    delete, reset) with complete traceability including the actor, reason, confirmation 
    evidence, and outcome details. Designed as an append-only event log for compliance 
    and audit trail purposes.

    Attributes:
        institution: Foreign key reference to the affected Institution.
        operation_type: Type of operation performed (suspend, reactivate, delete, reset).
        actor_id: Identifier of the user or system that triggered the operation.
        reason: Optional narrative explanation or justification for the operation.
        confirmation_token: Hashed or reference token of user confirmation (never raw secrets).
        outcome: Result status of the operation (success, failed, pending, etc.).
        error_code: Machine-readable error classification if operation failed.
        error_message: Human-readable error details if operation failed.
        occurred_at: Timestamp when the event was recorded (indexed for query performance).

    Meta:
        db_table: institution_operation_event
        indexes: Composite index on (institution, operation_type, occurred_at) for 
                 efficient querying of operation history filtered by type and date range.
    """

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="operation_events",
    )

    operation_type = models.CharField(max_length=16, choices=OperationType.choices)
    actor_id = models.CharField(max_length=120)
    reason = models.TextField(blank=True, default="")

    confirmation_token = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Stores typed confirmation phrase/token or hash reference (never store raw secrets).",
    )

    outcome = models.CharField(max_length=16, choices=OperationOutcome.choices)
    error_code = models.CharField(max_length=80, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "operation_type", "occurred_at"]),
        ]


class AuditEvent(TimeStampedModel):
    """
    Audit Event model for tracking and recording all significant system actions and state changes.

    This model captures a complete audit trail of operations performed within the system,
    enabling compliance, security monitoring, and forensic analysis. Each event represents
    a discrete action taken by an actor on a specific resource.

    Attributes:
        institution (ForeignKey): Reference to the Institution affected by this event.
            Can be null for system-wide or global operations. Allows filtering audit
            logs by institution scope.
        
        actor_id (str): Identifier of the user, service, or system component that
            performed the action. Stored as a string to support multiple identity
            systems (UUID, username, service name, etc.).
        
        action (str): The type of operation performed. Examples include TENANT_CREATE,
            TENANT_SUSPEND, USER_DELETE, etc. Used for querying and analyzing audit
            patterns by operation type.
        
        resource_type (str): The type of entity affected by the action. Examples include
            Institution, InstitutionBranding, User, etc. Enables filtering events
            by the class of resources affected.
        
        resource_id (str): Unique identifier of the specific resource instance affected.
            Stored as a string representation (typically a UUID or natural key) to
            support lookups and correlation with the actual resource.
        
        before_hash (str): Cryptographic hash of the resource state prior to the action.
            Stored as a compact reference rather than full payload data to minimize
            storage overhead while maintaining integrity verification capability.
        
        after_hash (str): Cryptographic hash of the resource state after the action.
            Enables detection of unexpected state changes and data corruption.
        
        outcome (str): The result status of the operation. Must be one of the predefined
            OperationOutcome choices (e.g., SUCCESS, FAILURE, PARTIAL). Allows filtering
            by operation success/failure for compliance and troubleshooting.
        
        occurred_at (datetime): Precise timestamp when the event occurred. Automatically
            set to the current time. Indexed for efficient temporal queries and sorting.

    Metadata:
        Maintains composite indexes on common query patterns:
        - (institution, occurred_at): Retrieve institution-scoped audit history
        - (action, occurred_at): Analyze specific operation types over time
        - (resource_type, resource_id): Track all events affecting a specific resource
    """

    institution = models.ForeignKey(
        Institution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )

    actor_id = models.CharField(max_length=120)
    action = models.CharField(max_length=120)          # e.g., TENANT_CREATE, TENANT_SUSPEND
    resource_type = models.CharField(max_length=80)    # e.g., Institution, InstitutionBranding
    resource_id = models.CharField(max_length=64)      # stringified UUID or natural key

    before_hash = models.CharField(max_length=128, blank=True, default="")
    after_hash = models.CharField(max_length=128, blank=True, default="")

    outcome = models.CharField(max_length=16, choices=OperationOutcome.choices)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "occurred_at"]),
            models.Index(fields=["action", "occurred_at"]),
            models.Index(fields=["resource_type", "resource_id"]),
        ]
