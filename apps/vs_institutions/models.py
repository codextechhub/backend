from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Q, Max
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
    ACTIVE = "Active", "Active"
    INACTIVE = "Inactive", "Inactive"
    DELETED = "Deleted", "Deleted"
    

class BranchStatus(models.TextChoices):
    ACTIVE = "Active", "Active"
    PENDING = "Pending", "Pending Activation"
    SUSPENDED = "Suspended", "Suspended"
    INACTIVE = "Inactive", "Inactive"
    CLOSED = "Closed", "Closed"


class InviteStatus(models.TextChoices):
    QUEUED = "Queued", "Queued"
    SENT = "Sent", "Sent"
    FAILED = "Failed", "Failed"


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
    Institution model representing the tenant identity within the system.

    The Institution is the stable, canonical entity that serves as the primary tenant.
    Multiple Branch objects are associated with each Institution to carry location-specific
    and contact data, following a multi-tenant architecture pattern.

    Attributes:
        name (CharField): Human-readable institution name.
        slug (SlugField): URL-safe, unique identifier for the institution. Primary key.
            Must be lowercase and hyphen-separated. Cannot be blank or reserved.
        category (CharField): Type classification (e.g., School, College, Organization).
        _type (CharField): Operational classification (e.g., Public, Private).
        plan_tier (CharField): Subscription tier for feature access and limitations.
            Defaults to PlanTier.STARTER.
        status (CharField): Current operational status of the institution.
            Defaults to InstitutionStatus.ACTIVE. Indexed for query performance.
        activated_at (DateTimeField): Timestamp when the institution became active.
            Nullable for pending activations.
        deleted_at (DateTimeField): Soft delete timestamp. Nullable for active institutions.

    Indexes:
        - slug: Optimizes primary key lookups and slug-based queries.
        - (status, created_at): Supports filtered list queries by institution status and creation date.

    Constraints:
        - slug cannot be empty string.
        - slug must not match reserved tenant slugs (validated in clean()).

    Notes:
        - Use select_related() or prefetch_related() when accessing main_branch in views
          for optimal query performance.
        - Supports soft deletes via deleted_at field for audit trail preservation.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(
        primary_key=True,
        max_length=80,
        unique=True,
        validators=[slug_validator],
        help_text="URL-safe unique institution identifier. Lowercase, hyphen-separated.",
    )

    _type = models.CharField(max_length=80)      # e.g., Public/Private

    status = models.CharField(
        max_length=16,
        choices=InstitutionStatus.choices,
        default=InstitutionStatus.ACTIVE,
        db_index=True,
    )

    activated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=~Q(slug=""), name="slug_not_empty"),
        ]

    def __str__(self) -> str:
        return self.slug

    def clean(self):
        super().clean()
        slug = (self.slug or "").strip().lower()
        if slug in RESERVED_TENANT_SLUGS:
            raise ValidationError({"slug": "This slug is reserved. Choose another."})

    # --- Branch helpers ---

    @property
    def main_branch(self):
        """
        Returns the main branch.
        Note: use select_related/prefetch_related in views for performance.
        """
        return self.branches.filter(is_main=True).first()


class Branch(TimeStampedModel):
    """
    Django model representing an institution branch/campus location.
    A Branch tracks location-specific information for an Institution. Each institution
    can have multiple branches, with exactly one designated as the main branch (is_main=True).
    Branches have a lifecycle managed through status transitions (PENDING → ACTIVE → SUSPENDED/INACTIVE → CLOSED).
    All status transitions are logged in the BranchLifecycle model for audit purposes.
    Key Features:
    - Location & contact information (address, email, website, phone, timezone, currency)
    - Status tracking with lifecycle logging via transition() methods
    - Enforced constraints: one main branch per institution, unique code per institution
    - Automatic timestamp management (created_at, updated_at, deleted_at)
    - Indexed queries for common lookups (institution + is_main, institution + status, institution + code)
    Attributes:
        institution (ForeignKey): Parent institution this branch belongs to
        name (CharField): Display name (e.g., 'Lekki Campus')
        code (AutoField): Auto-incrementing identifier unique within the institution
        is_main (BooleanField): Marks the primary branch for the institution
        address, email, website, phone_number (str): Contact details
        country, state, city (str): Geographic location
        timezone, currency (str): Operational preferences
        status (CharField): Current state (PENDING, ACTIVE, SUSPENDED, INACTIVE, CLOSED)
        opened_at, closed_at, activated_at (DateTimeField): Lifecycle timestamps
    Methods:
        mark_active(): Transition to ACTIVE status
        suspend(): Transition to SUSPENDED status with reason
        reactivate(): Return to ACTIVE from suspended/inactive state
        mark_inactive(): Transition to INACTIVE status
        transition(): Core state machine for managing status changes and logging
        clean(): Validates business rules (auto-sets closed_at if status=CLOSED)
    """

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="branches",
        db_index=True,
    )

    name = models.CharField(max_length=255, help_text="Branch display name, e.g., 'Lekki Campus'")
    code = models.PositiveIntegerField(
        editable=False,
        null=False,
        help_text="Branch code unique per institution (1..N).",
        db_index=True,
    )
    is_main = models.BooleanField(
        default=False,
        help_text="Marks the primary/main branch for this institution.",
    )

    category = models.CharField(max_length=80)  # e.g., School/College/Org
    plan_tier = models.CharField(
        max_length=80,
        default=PlanTier.STARTER,
        choices=PlanTier.choices,
    )

    # Branch contact/location info
    address = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    phone_number = models.CharField(max_length=15, blank=True, default="")

    country = models.CharField(max_length=80, default="Nigeria")
    state = models.CharField(max_length=120, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    timezone = models.CharField(max_length=64, blank=True, default="")
    currency = models.CharField(max_length=8, blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=BranchStatus.choices,
        default=BranchStatus.PENDING,
        db_index=True,
    )

    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    activated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "is_main"]),
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "code"]),
        ]
        constraints = [
            # Optional: if code is supplied, enforce uniqueness within institution
            models.UniqueConstraint(
                fields=["institution", "code"],
                condition=~Q(code=0),  # AutoField starts at 1, so code=0 can represent "not set"
                name="uniq_branch_code_per_institution_when_present",
            ),

            # Enforce only ONE main branch per institution
            models.UniqueConstraint(
                fields=["institution"],
                condition=Q(is_main=True),
                name="uniq_one_main_branch_per_institution",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.institution.slug}:{self.code}"

    def clean(self):
        super().clean()

        # Closed implies closed_at (optional policy)
        if self.status == BranchStatus.CLOSED and self.closed_at is None:
            self.closed_at = timezone.now()
    
    @staticmethod
    def allocate_next_code(*, institution: Institution) -> int:
        """
        Allocates the next branch code per institution safely.
        Uses row locking to prevent duplicate codes under concurrency.
        """
        # Lock rows for this institution so two creates don't pick the same Max(code)
        qs = Branch.objects.select_for_update().filter(institution=institution)
        current_max = qs.aggregate(m=Max("code"))["m"] or 0
        return current_max + 1

    def save(self, *args, **kwargs):
        # Allocate code only on first save if missing/zero.
        if not self.code:
            with transaction.atomic():
                self.code = Branch.allocate_next_code(institution=self.institution)
                super().save(*args, **kwargs)
            return
        return super().save(*args, **kwargs)
    
    # --- Lifecycle helpers ---

    def mark_active(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=BranchStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def suspend(self, *, actor_id: str, reason: str):
        self.transition(to_state=BranchStatus.SUSPENDED, actor_id=actor_id, reason=reason)

    def reactivate(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=BranchStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def mark_inactive(self, *, actor_id: str, reason: str):
        self.transition(to_state=BranchStatus.INACTIVE, actor_id=actor_id, reason=reason)

    def transition(self, *, to_state: str, actor_id: str, reason: str = ""):
        from_state = self.status
        if from_state == to_state:
            return

        self.status = to_state
        if to_state == BranchStatus.ACTIVE and self.activated_at is None:
            self.activated_at = timezone.now()

        self.save(update_fields=["status", "activated_at", "updated_at", "deleted_at"])

        BranchLifecycle.objects.create(
            branch=self,
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


class BranchLifecycle(models.Model):
    """
    Represents an event in the lifecycle of a branch, capturing state transitions.

    Attributes:
        branch (ForeignKey): A reference to the related Branch.
        from_state (str): The state from which the branch is transitioning.
        to_state (str): The state to which the branch is transitioning.
        actor_id (str): Identifier for the actor responsible for the transition.
        reason (str): An optional text field providing the reason for the transition.
        occurred_at (datetime): The timestamp when the event occurred, indexed for performance.

    Meta:
        db_table (str): Specifies the database table name for this model.
        indexes (list): Defines indexes for efficient querying on branch and state transitions.
    """

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )

    from_state = models.CharField(max_length=32, choices=BranchStatus.choices)
    to_state = models.CharField(max_length=32, choices=BranchStatus.choices)

    actor_id = models.CharField(max_length=120)
    reason = models.TextField(blank=True, default=None)

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["branch", "occurred_at"]),
            models.Index(fields=["branch", "to_state"]),
        ]


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


class BranchPrimaryAdmin(TimeStampedModel):
    """
    BranchPrimaryAdmin model for managing primary administrators of branches.

    This model establishes a one-to-one relationship between a Branch and its
    primary administrator contact. It tracks the administrative assignment, role details,
    and invitation lifecycle for the primary admin.

    Attributes:
        branch (OneToOneField): The branch this admin is assigned to.
            Cascade delete ensures cleanup when branch is removed.
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
        indexes: Composite index on (institution, invite_status) for efficient queries
            on invitation status by institution.
    """

    branch = models.OneToOneField(
        Branch,
        on_delete=models.CASCADE,
        related_name="primary_admin",
    )
    contact = models.ForeignKey(
        ContactInfo,
        on_delete=models.PROTECT,
        related_name="primary_admin_for_branches",
    )

    role_label = models.CharField(max_length=80, blank=True, default="BR_AD")

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
            models.Index(fields=["branch", "invite_status"]),
        ]


# -----------------------------------------------------------------------------
# Audit trails
# -----------------------------------------------------------------------------

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
        
        before_change (TextField): Optional snapshot of the resource state before the change.
            Can be stored as JSON or a stringified representation. Useful for auditing
            the exact changes made, especially for critical operations.

        diff_change (TextField): Optional field capturing the difference between before and after states.
            This can be a JSON diff or a string representation of the changes, allowing for easier 
            analysis of what was modified without needing to store the entire before/after states.   
        
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
    resource_slug = models.CharField(max_length=64)      # stringified Slug or natural key

    before_change = models.TextField(blank=True, default="")  # optional JSON or stringified state snapshot before the change
    diff_change = models.TextField(blank=True, default="")   # optional JSON or stringified diff of the change for easier analysis (could be generated from before/after hashes)

    outcome = models.CharField(max_length=16, choices=OperationOutcome.choices)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["institution", "occurred_at"]),
            models.Index(fields=["action", "occurred_at"]),
            models.Index(fields=["resource_type", "resource_slug"]),
        ]
