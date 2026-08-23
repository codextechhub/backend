"""The three onboarding tables.

Three, and no more. The escalation model an earlier draft specified is the live
ticket desk (``vs_tickets``), and the event log it specified is ``vs_audit``:
both already store more than a private copy would, and a mirror is a second
source of truth waiting to disagree with the first.

Everything here is owned by ``vs_tenants.Tenant``, not by ``School``. That is
what the JWT asserts, what ``request.tenant`` holds and what
``TenantAwareManager`` filters on. The school is one hop away as
``tenant.school_profile`` whenever a display name is wanted.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import GoLiveStatus, ReadinessState, TaskKey, TaskStatus


class OnboardingProgress(models.Model):
    """One row per tenant: the summary the go-live gate reads.

    ``readiness_state`` is written by exactly one place,
    :class:`~schools.vs_onboarding.services.readiness.ReadinessService`, and
    ``go_live_at`` by exactly one place, activation. Views compute neither, so
    the control room display and the gate cannot disagree about the same school.
    """

    tenant = models.OneToOneField(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="onboarding_progress",
    )
    readiness_state = models.CharField(
        max_length=20,
        choices=ReadinessState.choices,
        default=ReadinessState.NOT_READY,
        db_index=True,
    )
    go_live_at = models.DateTimeField(null=True, blank=True)
    last_validation_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        verbose_name_plural = "onboarding progress"

    def __str__(self) -> str:
        return f"{self.tenant_id}: {self.readiness_state}"


class OnboardingTask(models.Model):
    """One row per tenant per catalog key.

    ``title``, ``is_required`` and ``order_index`` are copies of the catalog
    taken at provisioning time, never values a request supplied. Copying rather
    than joining is deliberate: a catalog edit must not silently rewrite the
    history of a school that already finished onboarding under the old wording.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="onboarding_tasks",
    )
    key = models.CharField(max_length=40, choices=TaskKey.choices)
    title = models.CharField(max_length=120)
    is_required = models.BooleanField(default=True)
    status = models.CharField(
        max_length=20,
        choices=TaskStatus.choices,
        default=TaskStatus.NOT_STARTED,
        db_index=True,
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    order_index = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key"],
                name="uq_onboarding_task_tenant_key",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}: {self.key} ({self.status})"


class GoLiveRequest(models.Model):
    """A school asking to be taken live, and the platform's answer.

    The partial unique constraint is what actually prevents two pending
    requests racing; the 409 the service raises is the friendly path in front
    of it. Both are required, exactly as ``Branch`` does it for its
    one-main-per-tenant rule: a pre-check alone loses the race it exists to
    prevent.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="go_live_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="go_live_requests",
    )
    preferred_go_live_at = models.DateTimeField()
    note = models.TextField(blank=True, default="")
    acknowledged = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12,
        choices=GoLiveStatus.choices,
        default=GoLiveStatus.PENDING,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="go_live_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    failure_reference = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status=GoLiveStatus.PENDING),
                name="uq_golive_one_pending_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}: go-live {self.status}"
