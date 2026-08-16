"""Durable record of every finance document emailed to a customer.

Finance used to send customer email fire-and-forget: :mod:`vs_finance.notifications`
called ``send_notification`` on the success path of a posting and kept nothing. That
made three things impossible - saying whether a customer was ever emailed, re-sending
a document on request, and retrying a delivery that failed - which is why the invoice
and statement send buttons stayed disabled while the emails themselves were going out.

This model closes that gap the way :class:`vs_procurement.models.PurchaseOrderVendorDelivery`
does for vendor purchase orders: one row per attempt, carrying who asked, who it went
to, what was attached, and how it ended. Automatic sends on posting create rows too, so
the history a user reads is the whole story rather than only the parts they triggered.

The document reference is a ``(document_type, document_id)`` pair rather than an FK
because a statement of account is a *report* over a customer and a date range, with no
row of its own to point at. That mirrors ``FinanceAuditLog.target_type``/``target_id``.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from ..constants import (
    FinanceDeliveryDocument,
    FinanceDeliverySource,
    FinanceDeliveryStatus,
)
from .ar import Customer
from .core import LedgerEntity, TimeStampedModel


# Keep generated customer copies grouped by tenant, entity and customer.
def finance_delivery_pdf_path(instance, filename):
    """Group stored PDFs by tenant, entity and customer so a purge stays targeted."""
    return (
        f"finance/document-emails/{instance.entity.tenant_id}/{instance.entity_id}/"
        f"{instance.customer_id}/{instance.pk or 'new'}/{filename}"
    )


class FinanceDocumentDelivery(TimeStampedModel):
    """One attempt to email one finance document to one customer."""

    # Stored rather than derived: a statement has no document row to read it from,
    # and every list of deliveries is entity-scoped.
    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="document_deliveries",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="document_deliveries",
    )
    document_type = models.CharField(
        max_length=16, choices=FinanceDeliveryDocument.choices,
    )
    # Blank for a statement, which is a period rather than a document.
    document_id = models.CharField(max_length=64, blank=True, default="")
    document_number = models.CharField(max_length=48, blank=True, default="")
    # Statement period. Kept so a retry reproduces the same statement rather than
    # silently re-cutting it against today's balances.
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    source = models.CharField(max_length=16, choices=FinanceDeliverySource.choices)
    status = models.CharField(max_length=16, choices=FinanceDeliveryStatus.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requested_finance_document_deliveries",
        help_text="Null for an automatic send on posting.",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="retries",
    )

    # A covering note from the sender, shown in the email above the document.
    note = models.TextField(blank=True, default="")
    recipients = models.JSONField(default=list)
    # Monitoring copies are blind: these addresses are ours, not the customer's.
    bcc = models.JSONField(default=list)
    notification_ids = models.JSONField(default=list)
    pdf_file = models.FileField(upload_to=finance_delivery_pdf_path, blank=True)

    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The document drawer's history tab.
            models.Index(fields=["document_type", "document_id", "-created_at"]),
            # "Everything we have emailed this customer", on the customer drawer.
            models.Index(fields=["customer", "-created_at"]),
            # Entity-scoped listing and the failed-delivery sweep.
            models.Index(fields=["entity", "status", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.document_type} {self.document_number or self.customer_id} -> {self.status}"
