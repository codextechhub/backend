from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
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
    "media", "health", "status", "support", "system", "internal", "codex",
}


# -----------------------------------------------------------------------------
# Enumerations
# -----------------------------------------------------------------------------

class SchoolStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    PENDING = "PENDING", "Pending"
    

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

    School captures the durable identity for a school or organization while its
    tenant's `vs_tenants.Branch` rows store per-location details. The slug is a
    unique business identifier so tenants can be addressed via subdomains and
    API scopes.

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
        activated_at / deactivated_at: Lifecycle timestamps for activation and deactivation.

    Meta:
        - indexes on `slug` and (`status`, `created_at`) for list views.
        - `slug_not_empty` check complements the strict validator.

    Notes:
        - `clean()` blocks slugs listed in `RESERVED_TENANT_SLUGS`.
        - Use `main_branch` with `select_related` to avoid extra queries.
    """

    tenant = models.OneToOneField(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="school_profile",
        help_text="Canonical ownership boundary.",
    )
    name = models.CharField(max_length=255)
    # The slug WAS the primary key. It's now a unique business identifier
    # over a surrogate BigAuto id (added implicitly via DEFAULT_AUTO_FIELD), so
    # renaming a school's slug no longer means rewriting every FK in the
    # platform - and FK indexes carry 8-byte ints instead of varchar(80).
    slug = models.SlugField(
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

    def save(self, *args, **kwargs):
        # Keep direct ORM/test creation safe as well as the onboarding service:
        # the pair is committed or rolled back as one unit.
        with transaction.atomic():
            if not self.tenant_id:
                from vs_tenants.models import Tenant
                status = (
                    Tenant.Status.ACTIVE
                    if self.status == SchoolStatus.ACTIVE
                    else Tenant.Status.PENDING
                )
                self.tenant = Tenant.objects.create(
                    name=self.name,
                    slug=self.slug,
                    kind=Tenant.Kind.SCHOOL,
                    status=status,
                    activated_at=self.activated_at,
                )
            self.code = str(self.code or "").strip().upper()
            if not self.code:
                from vs_tenants.numbering import next_tenant_document_number

                self.code = next_tenant_document_number(
                    tenant=self.tenant,
                    document_code="SC",
                )
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"code"}
            result = super().save(*args, **kwargs)
            from vs_tenants.models import Tenant
            tenant_status = {
                SchoolStatus.ACTIVE: Tenant.Status.ACTIVE,
                SchoolStatus.INACTIVE: Tenant.Status.INACTIVE,
            }.get(self.status, Tenant.Status.PENDING)
            Tenant.objects.filter(pk=self.tenant_id).update(
                name=self.name,
                status=tenant_status,
                activated_at=self.activated_at,
                deactivated_at=self.deactivated_at,
            )
            return result

    # --- Branch helpers ---

    @property
    def branches(self):
        """This school's sites.

        ``Branch`` moved to ``vs_tenants`` and lost its ``school`` foreign key,
        so the reverse accessor that used to be generated here is now a hop
        through the tenant. ``School.tenant`` is a non-nullable OneToOneField,
        so this is exactly the same set of rows the FK produced, and it is
        still a manager, so ``.filter()``, ``.count()`` and DRF's ``many=True``
        all behave as before. Prefetch it as ``"tenant__branches"``.
        """
        return self.tenant.branches

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

    Stays in the school app while ``Branch`` moves to ``vs_tenants``: this is
    invite and onboarding machinery, and its defaults ("Head Teacher") are
    school vocabulary. Nothing outside ``vs_schools`` references it.
    """

    branch = models.OneToOneField(
        "vs_tenants.Branch",
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
