from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


tenant_slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message="Slug must be lowercase letters/numbers separated by single hyphens.",
)


class Tenant(models.Model):
    """A customer or platform security boundary.

    Numeric primary keys are deliberately internal. ``slug`` is the stable,
    human-readable identifier accepted by tenant-scoped APIs.
    """

    class Kind(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform"
        SCHOOL = "SCHOOL", "School"
        ORGANIZATION = "ORGANIZATION", "Organization"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        INACTIVE = "INACTIVE", "Inactive"

    name = models.CharField(max_length=255)
    slug = models.SlugField(
        max_length=80, unique=True, validators=[tenant_slug_validator],
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["kind", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(slug=""), name="tenant_slug_not_empty",
            ),
        ]

    def clean(self):
        super().clean()
        self.slug = (self.slug or "").strip().lower()

    def activate(self):
        self.status = self.Status.ACTIVE
        self.activated_at = self.activated_at or timezone.now()
        self.deactivated_at = None

    def __str__(self):
        return self.slug


class TenantOwnedModel(models.Model):
    """Abstract contract for rows owned by exactly one tenant."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        db_index=True,
    )

    class Meta:
        abstract = True
