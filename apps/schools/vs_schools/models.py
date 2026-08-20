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


# The one list, which now lives in the platform app beside the tenant slug
# validator: the names it protects are platform hostnames, and an ORGANIZATION
# or VIGIL tenant gets a subdomain off the same wildcard as a school. Re-exported
# here because this app, its serializers and vs_import_data all read it by this
# name. Extend it in vs_tenants, not here.
from vs_tenants.models import (  # noqa: E402  (kept beside the name it replaces)
    RESERVED_TENANT_SLUGS,
    slug_is_reserved,
)


# -----------------------------------------------------------------------------
# Enumerations
# -----------------------------------------------------------------------------

class SchoolStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    PENDING = "PENDING", "Pending"
    # A school whose onboarding was abandoned and expired. It is a school
    # status, and not only a tenant one, because `save()` mirrors this column
    # onto the tenant on every write: a tenant suspended on its own would be
    # quietly returned to PENDING by the next ordinary edit of its school.
    SUSPENDED = "SUSPENDED", "Suspended"


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

    # How many sites a school runs is not stored here: it is counted. There was
    # a boolean for it, and a flag can disagree with the rows it describes, so a
    # school could claim one site while its ``tenant.branches`` said three. Ask
    # the branches. Every school has at least one from the moment it is created.

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
        if slug_is_reserved(self.slug):
            raise ValidationError({"slug": "This slug is reserved. Choose another."})

    def _check_slug_change(self) -> bool:
        """Refuse a post-go-live rename, and report whether the school is live.

        Returns ``True`` once the school has been live, which is also the
        answer to "may its slug still be mirrored onto its tenant?" - see
        :meth:`save`.

        ``School.save()`` seeds ``Tenant.slug`` at creation and deliberately
        never syncs it afterwards, so the two are only equal because nothing
        moves either one. Guarding the tenant alone would leave the school's
        own slug - the ``/v1/i/<slug>/`` path key - free to drift away from the
        sign-in address after go-live, which is a different bug with the same
        cause. Same test as ``Tenant._assert_slug_unchanged_once_live`` and for
        the same reasons: ``activated_at`` is written once, so it answers "has
        this school ever been live?" rather than "is it live right now?", and a
        school suspended for an unpaid invoice cannot rename itself while it is
        off.
        """
        stored = self._stored_identity()
        if stored is None:
            return False
        has_been_live = self._has_been_live(stored)
        if has_been_live and stored["slug"] != self.slug:
            raise ValidationError({
                "slug": (
                    "This school is live, so its address is fixed. Changing it "
                    "would break every link and sign-in its users already have."
                )
            })
        return has_been_live

    def _stored_identity(self):
        """The row as the database currently holds it, or ``None`` if unsaved.

        Read as a dict rather than as ``self``, because every caller here is
        asking what the *stored* school looks like, and the in-memory instance
        is exactly the thing that may already have been edited.
        """
        if not self.pk:
            return None
        return (
            School.objects.filter(pk=self.pk)
            .values("slug", "activated_at", "status")
            .first()
        )

    @staticmethod
    def _has_been_live(stored) -> bool:
        return (
            stored["activated_at"] is not None
            or stored["status"] == SchoolStatus.ACTIVE
        )

    def has_ever_been_live(self) -> bool:
        """Whether this school has been live at any point, per the stored row.

        The public half of :meth:`_check_slug_change`, for callers that need to
        ask the question before attempting the write - the update serializer
        refuses a rename in ``validate_slug`` so the caller gets a typed 409
        rather than a field error escaping from ``save()``. Both read the same
        row and apply the same test, so the API and the model cannot disagree
        about which schools are frozen.
        """
        stored = self._stored_identity()
        return stored is not None and self._has_been_live(stored)

    def save(self, *args, **kwargs):
        self.slug = (self.slug or "").strip().lower()
        slug_is_frozen = self._check_slug_change()
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
                    pending_since=Tenant.pending_since_for(
                        new_status=status, previous_status=None, current=None,
                    ),
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
                SchoolStatus.SUSPENDED: Tenant.Status.SUSPENDED,
            }.get(self.status, Tenant.Status.PENDING)
            # This update is the only place a school's tenant changes status, so
            # it is also the only place that can tell "became pending" from
            # "was already pending". Reading the row first is what keeps an
            # ordinary save (a rename, a metadata fix) from restarting the
            # 90-day clock the onboarding expiry sweep reads.
            previous = (
                Tenant.objects
                .filter(pk=self.tenant_id)
                .values("status", "pending_since", "expiry_warned_at")
                .first()
                or {}
            )
            pending_since, expiry_warned_at = Tenant.pending_stamps_for(
                new_status=tenant_status,
                previous_status=previous.get("status"),
                pending_since=previous.get("pending_since"),
                warned_at=previous.get("expiry_warned_at"),
            )
            # The slug is mirrored now, where it used to be seeded at creation
            # and then left alone. Correcting a typo before go-live has to
            # reach the tenant or it does not reach the sign-in address at all,
            # which is the only address that matters: a school that fixed
            # ``corona-secondry`` on its own row would still be served at the
            # misspelt host its admins actually type.
            #
            # And only before go-live. A live school's slug cannot have changed
            # (``_check_slug_change`` refused it), but a school whose slug
            # drifted from its tenant's under the old rules must not have that
            # drift resolved by an ordinary metadata save silently moving a
            # live school's sign-in address.
            mirrored = {
                "name": self.name,
                "status": tenant_status,
                "activated_at": self.activated_at,
                "deactivated_at": self.deactivated_at,
                "pending_since": pending_since,
                "expiry_warned_at": expiry_warned_at,
            }
            if not slug_is_frozen:
                mirrored["slug"] = self.slug
            Tenant.objects.filter(pk=self.tenant_id).update(**mirrored)
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

    Stores the primary School contact, its optional job title, and invite
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
