from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager
from vs_schools.models import Branch
from vs_user.models import TimeStampedModel

from .constants import (
    CommentVisibility,
    TicketAuditAction,
    TicketCategory,
    TicketPriority,
    TicketSource,
    TicketStatus,
)


def ticket_attachment_upload_to(instance: "TicketAttachment", filename: str) -> str:
    ticket_number = instance.ticket.ticket_number or f"ticket-{instance.ticket_id}"
    return f"ticket-attachments/{ticket_number}/{filename}"


class TicketSequence(models.Model):
    """Per-tenant, per-day counter backing ticket numbers (<SLUG>-CX<YYMMDD><n>).

    Each (tenant, day) pair has its own counter that starts at 1 and is not
    zero-padded. Allocation locks the row with ``select_for_update`` so
    concurrent creators serialise and can never be handed the same number —
    same pattern as ``vs_finance.numbering.DocumentSequence``.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE, related_name="ticket_sequences",
    )
    date = models.DateField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "vs_tickets_sequence"
        unique_together = (("tenant", "date"),)

    def __str__(self) -> str:
        return f"{self.tenant_id}/{self.date}: {self.last_number}"


class Ticket(TimeStampedModel):
    # Wide enough for <SLUG>-CX<YYMMDD><n> with the longest allowed tenant slug.
    ticket_number = models.CharField(max_length=100, unique=True, editable=False)
    title = models.CharField(max_length=220)
    description = models.TextField()
    category = models.CharField(
        max_length=20,
        choices=TicketCategory.choices,
        default=TicketCategory.SUPPORT,
        db_index=True,
    )
    priority = models.CharField(
        max_length=20,
        choices=TicketPriority.choices,
        default=TicketPriority.MEDIUM,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
        db_index=True,
    )
    source = models.CharField(
        max_length=20,
        choices=TicketSource.choices,
        default=TicketSource.CUSTOMER,
        db_index=True,
    )

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_tickets",
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="tickets",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tickets",
        db_index=True,
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "vs_tickets_ticket"
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "priority"]),
            models.Index(fields=["requester", "status"]),
            models.Index(fields=["assignee", "status"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["category", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

    def clean(self):
        super().clean()
        if self.requester_id and self.requester.tenant_id != self.tenant_id:
            raise ValidationError("Ticket requester must belong to the selected tenant.")
        if self.branch_id and self.branch.school.tenant_id != self.tenant_id:
            raise ValidationError("Ticket branch must belong to the selected tenant.")
        if self.status == TicketStatus.ASSIGNED and not self.assignee_id:
            raise ValidationError("Assigned tickets require an assignee.")

    def save(self, *args, **kwargs):
        if not self.tenant_id and self.requester_id:
            self.tenant_id = self.requester.tenant_id
        if not self.ticket_number:
            self.ticket_number = self._allocate_ticket_number(self.tenant)
        super().save(*args, **kwargs)

    @property
    def school(self):
        return getattr(self.tenant, "school_profile", None)

    @property
    def school_id(self):
        return getattr(self.school, "pk", None)

    @staticmethod
    def _allocate_ticket_number(tenant) -> str:
        """Allocate ``<SLUG>-CX<YYMMDD><n>`` — n resets to 1 each day per tenant.

        ``CX`` marks a CodeX ticket; ``<SLUG>`` is the raising tenant's slug.
        """
        with transaction.atomic():
            today = timezone.localdate()
            TicketSequence.objects.get_or_create(tenant=tenant, date=today)
            seq = TicketSequence.objects.select_for_update().get(tenant=tenant, date=today)
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            slug = (tenant.slug or "").upper()
            return f"{slug}-CX{today:%y%m%d}{seq.last_number}"

    def __str__(self) -> str:
        return f"{self.ticket_number}: {self.title[:40]}"


class TicketComment(TimeStampedModel):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ticket_comments",
    )
    body = models.TextField()
    visibility = models.CharField(
        max_length=20,
        choices=CommentVisibility.choices,
        default=CommentVisibility.PUBLIC,
        db_index=True,
    )

    class Meta:
        db_table = "vs_tickets_comment"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["ticket", "visibility"]),
            models.Index(fields=["author", "created_at"]),
        ]

    @property
    def is_internal(self) -> bool:
        return self.visibility == CommentVisibility.INTERNAL

    def __str__(self) -> str:
        return f"Comment<{self.ticket_id}:{self.author_id}>"


class TicketAttachment(TimeStampedModel):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    comment = models.ForeignKey(
        TicketComment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ticket_attachments",
    )
    file = models.FileField(upload_to=ticket_attachment_upload_to)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "vs_tickets_attachment"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"]),
            models.Index(fields=["uploaded_by", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.original_filename


class TicketAuditLog(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_audit_logs",
    )
    action = models.CharField(max_length=40, choices=TicketAuditAction.choices, db_index=True)
    summary = models.TextField(blank=True, default="")
    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    class Meta:
        db_table = "vs_tickets_audit_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.ticket.ticket_number} {self.action}"
