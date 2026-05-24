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

class SchoolStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    PENDING = "PENDING", "Pending"
    

class BranchStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    PENDING = "PENDING", "Pending Activation"
    SUSPENDED = "SUSPENDED", "Suspended"
    INACTIVE = "INACTIVE", "Inactive"
    CLOSED = "CLOSED", "Closed"


class InviteStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class OperationOutcome(models.TextChoices):
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class PlanTier(models.TextChoices):
    BASIC = "BASIC", "Basic"
    STANDARD = "STANDARD", "Standard"
    PREMIUM = "PREMIUM", "Premium"
    ENTERPRISE = "ENTERPRISE", "Enterprise"


class Modules(models.TextChoices):
    STUDENTS = "STUDENTS", "Students Management"
    TEACHERS = "TEACHERS", "Teachers Management"
    PARENTS = "PARENTS", "Parents Management"
    ATTENDANCE = "ATTENDANCE", "Attendance Tracking"
    FINANCE = "FINANCE", "Finance"
    PROCUREMENT = "PROCUREMENT", "Procurement"
    VENDORS = "VENDORS", "Vendors Management"


class OwnershipType(models.TextChoices):
    PUBLIC = "PUBLIC", "Public"
    PRIVATE = "PRIVATE", "Private"
    FAITH_BASED = "FAITH_BASED", "Faith-Based"
    NGO = "NGO", "Non-Governmental Organization"


class TermStructure(models.TextChoices):
    TWO_SEMESTERS = "2_SEMESTERS", "2 Semesters"
    THREE_TERMS = "3_TERMS", "3 Terms"


class Currency(models.TextChoices):
    NGN = "NGN", "Nigerian Naira"
    USD = "USD", "US Dollar"


class BillingCycle(models.TextChoices):
    YEARLY = "YEARLY", "Yearly"
    MONTHLY = "MONTHLY", "Monthly"


# -----------------------------------------------------------------------------
# Core Entities
# -----------------------------------------------------------------------------

class School(TimeStampedModel):
    """
    Canonical tenant record for the platform.

    School captures the durable identity for a school or organization while
    related Branch rows store per-location details. The slug doubles as the primary
    key so tenants can be addressed via subdomains and API scopes.

    Fields:
        name: Human-friendly display name.
        slug: URL-safe unique identifier (primary key) validated against reserved names.
        address: Optional summary address.
        ownership_type: Operational classification from `OwnershipType`.
        code: Optional alphanumeric identifier exposed in reporting; unique.
        website / motto / registration_id: Optional metadata displayed in onboarding.
        term_structure: Academic calendar definition (`TermStructure` choices).
        currency: Preferred billing currency (`Currency` choices).
        status: Operational flag (`SchoolStatus` choices, indexed).
        activated_at / deleted_at: Lifecycle timestamps for activation and soft-deletes.

    Meta:
        - indexes on `slug` and (`status`, `created_at`) for list views.
        - `slug_not_empty` check complements the strict validator.

    Notes:
        - `clean()` blocks slugs listed in `RESERVED_TENANT_SLUGS`.
        - Use `main_branch` with `select_related` to avoid extra queries.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(
        primary_key=True,
        max_length=80,
        unique=True,
        validators=[slug_validator],
        help_text="URL-safe unique school identifier. Lowercase, hyphen-separated.",
    )
    address = models.CharField(max_length=255, blank=True, default="")
    ownership_type = models.CharField(max_length=80, choices=OwnershipType.choices, default=OwnershipType.PUBLIC)
    code = models.CharField(max_length=32, blank=True, default="", unique=True)
    website = models.URLField(blank=True, default="")
    motto = models.CharField(max_length=255, blank=True, default="")
    term_structure = models.CharField(max_length=255, blank=True, default=TermStructure.THREE_TERMS, choices=TermStructure.choices)
    currency = models.CharField(max_length=8, blank=True, choices=Currency.choices, default=Currency.NGN)
    registration_id = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=SchoolStatus.choices,
        default=SchoolStatus.PENDING,
        db_index=True,
    )

    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(slug=""), name="slug_not_empty"),
        ]
        ordering = ["-created_at"]

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
        Returns the main branch with its primary_admin pre-loaded to avoid
        DoesNotExist on the reverse OneToOne when serializing.
        """
        return (
            self.branches
            .select_related("primary_admin", "primary_admin__contact")
            .filter(is_main=True)
            .first()
        )


class Branch(TimeStampedModel):
    """
    Physical branch or campus associated with an `School`.

    An school can own multiple branches but only one may be flagged as
    `is_main=True`. Branch codes are automatically allocated per school inside
    a transaction to avoid duplicates. Status changes flow through the helper
    methods and are logged via `BranchLifecycle`.

    Fields:
        school: FK back to the owning School (`branches` related name).
        name: Display label such as "Lekki Campus".
        code: Integer code unique within the school; filled on first save.
        is_main: Boolean marker for the canonical branch (unique constraint enforces 1).
        _type: Optional free-form descriptor (e.g., Primary, Secondary).
        address / email / country / state: Contact + location metadata captured today.
        status: Lifecycle state (BranchStatus choices, indexed).
        opened_at / closed_at / activated_at / deleted_at: Optional lifecycle timestamps.

    Meta:
        - indexes on (`school`, `is_main`), (`school`, `status`), (`school`, `code`)
        - unique constraints for non-zero codes per school and single main branch.

    Helpers:
        allocate_next_code() wraps a SELECT .. FOR UPDATE sequence per school.
        transition()/mark_*() mutate status and append a BranchLifecycle event.
        clean() auto-populates `closed_at` when the status is CLOSED.
    """

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="branches",
        db_index=True,
    )

    name = models.CharField(max_length=255, help_text="Branch display name, e.g., 'Lekki Campus'")
    code = models.PositiveIntegerField(
        editable=False,
        null=False,
        help_text="Branch code unique per school (1..N).",
        db_index=True,
    )
    is_main = models.BooleanField(
        default=False,
        help_text="Marks the primary/main branch for this school.",
    )

    _type = models.CharField(max_length=80)  # e.g., Primary, Secondary, etc. --- optional freeform for now

    # Branch contact/location info
    address = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    country = models.CharField(max_length=80, default="Nigeria")
    state = models.CharField(max_length=120, blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=BranchStatus.choices,
        default=BranchStatus.PENDING,
        db_index=True,
    )

    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["school", "is_main"]),
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "code"]),
        ]
        constraints = []
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.school.slug}:{self.code}"

    def clean(self):
        super().clean()

        if self.status == BranchStatus.CLOSED and self.closed_at is None:
            self.closed_at = timezone.now()
    
    @staticmethod
    def allocate_next_code(*, school: School) -> int:
        """
        Allocates the next branch code per school safely.
        Uses row locking to prevent duplicate codes under concurrency.
        """
        # Lock rows for this school so two creates don't pick the same Max(code)
        qs = Branch.objects.select_for_update().filter(school=school)
        current_max = qs.aggregate(m=Max("code"))["m"] or 0
        return current_max + 1

    def save(self, *args, **kwargs):
        # Allocate code only on first save if missing/zero.
        if not self.code:
            with transaction.atomic():
                self.code = Branch.allocate_next_code(school=self.school)
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

class SchoolBranding(TimeStampedModel):
    """
    Lightweight container for school-specific branding assets.

    Each School owns exactly one branding row which currently stores an optional
    `logo` upload. Additional theme fields can be added later without bloating the
    core School table.
    """

    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="branding",
    )

    logo = models.ImageField(upload_to="school_logos/", null=True, blank=True)


class XVSModules(TimeStampedModel):
    """
    Master list of modules that can be enabled for an school.
    Example: students, staff, finance, procurement, attendance, analytics.
    This fits your product docs where modules are enabled/disabled per school.
    """
    key = models.SlugField(
        max_length=100,
        unique=True,
        help_text="Unique machine key, e.g. students, finance, procurement"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Platform Module"
        verbose_name_plural = "Platform Modules"

    def __str__(self) -> str:
        return self.name
    

class PackagePlan(TimeStampedModel):
    """
    Catalog entry describing an available subscription package.

    Holds display data (`name`, `code`, `description`), billing cadence, seat caps,
    and an `is_active` flag so deprecated plans can be hidden while keeping history.
    """
    name = models.CharField(max_length=120, unique=True)
    code = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    billing_cycle = models.CharField(
        max_length=20,
        choices=BillingCycle.choices,
        default=BillingCycle.YEARLY,
    )

    max_students = models.PositiveIntegerField(null=True, blank=True)
    max_teachers = models.PositiveIntegerField(null=True, blank=True)
    max_admins = models.PositiveIntegerField(null=True, blank=True)
    max_branch = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Package Plan"
        verbose_name_plural = "Package Plans"

    def __str__(self) -> str:
        return self.name


class SchoolPackageSetup(TimeStampedModel):
    """
    Applied subscription configuration for an school.

    Records the chosen `PackagePlan`, seat capacities for key roles, subscription
    expiry, activation flag, and optional operator notes. `clean()` ensures
    capacities are positive, the expiry date is not in the past, and that each
    capacity respects the limits enforced by the associated `PackagePlan`. The
    one-to-one relationship guarantees at most one active setup per school.
    """
    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="package_setup",
    )
    package_plan = models.ForeignKey(
        PackagePlan,
        on_delete=models.PROTECT,
        related_name="school_setups",
    )
    enabled_modules = models.ManyToManyField(
        XVSModules,
        blank=True,
        related_name="school_package_setups",
    )

    student_capacity = models.PositiveIntegerField()
    teacher_capacity = models.PositiveIntegerField()
    admin_capacity = models.PositiveIntegerField()

    subscription_expires_at = models.DateField()

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "School Package Setup"
        verbose_name_plural = "School Package Setups"

    def __str__(self) -> str:
        return f"{self.school} - {self.package_plan}"

    def clean(self):
        errors = {}

        if self.student_capacity < 1:
            errors["student_capacity"] = "Student capacity must be at least 1."

        if self.teacher_capacity < 1:
            errors["teacher_capacity"] = "Teacher capacity must be at least 1."

        if self.admin_capacity < 1:
            errors["admin_capacity"] = "Admin capacity must be at least 1."

        if self.subscription_expires_at < timezone.localdate():
            errors["subscription_expires_at"] = "Subscription expiry cannot be in the past."

        # Plan limits
        if self.package_plan_id:
            if (
                self.package_plan.max_students is not None
                and self.student_capacity > self.package_plan.max_students
            ):
                errors["student_capacity"] = (
                    f"Student capacity exceeds plan limit "
                    f"({self.package_plan.max_students})."
                )

            if (
                self.package_plan.max_teachers is not None
                and self.teacher_capacity > self.package_plan.max_teachers
            ):
                errors["teacher_capacity"] = (
                    f"Teacher capacity exceeds plan limit "
                    f"({self.package_plan.max_teachers})."
                )

            if (
                self.package_plan.max_admins is not None
                and self.admin_capacity > self.package_plan.max_admins
            ):
                errors["admin_capacity"] = (
                    f"Admin capacity exceeds plan limit "
                    f"({self.package_plan.max_admins})."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
    

class BranchLifecycle(models.Model):
    """
    Audit log entry for a Branch status transition.

    Rows are created by `Branch.transition()`, capturing who initiated the change,
    the previous and new states, an optional free-form reason, and when it occurred.
    Indexed lookups support timeline views per branch or filtering by resulting state
    via (`branch`, `occurred_at`) and (`branch`, `to_state`).
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
    Stand-alone contact card used by invitation workflows.

    Stores a name, email, and optional phone number with a case-insensitive email
    index to prevent duplicates and power lookups without requiring a User record.
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
    Tracks the contact who serves as the primary administrator for a Branch.

    The record links a branch to a reusable `ContactInfo`, captures human-readable
    role labels, and records the invite status/timestamps for onboarding flows.
    Indexing by (`branch`, `invite_status`) helps find pending invites quickly.
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
    branch_role = models.CharField(max_length=80, blank=True, default="Head Teacher")
    role_label = models.CharField(max_length=80, blank=True, default="BRANCH_ADMIN")

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


class SchoolPrimaryAdmin(TimeStampedModel):
    """
    Same concept as `BranchPrimaryAdmin` but at the school level.

    Stores the primary School contact, optional role labels, and invite
    status/timestamps so onboarding jobs can reconcile which tenants still need
    primary admins activated. Indexed by (`school`, `invite_status`) for
    efficient filtering.
    """
    
    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="primary_admin",
    )
    contact = models.ForeignKey(
        ContactInfo,
        on_delete=models.PROTECT,
        related_name="primary_admin_for_schools",
    )
    school_role = models.CharField(max_length=80, blank=True, default="IT Head")
    role_label = models.CharField(max_length=80, blank=True, default="SCHOOL_ADMIN")

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
            models.Index(fields=["school", "invite_status"]),
        ]
