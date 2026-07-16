from django.db import models


# User-facing issue buckets used for support queue routing and reporting.
class TicketCategory(models.TextChoices):
    BUG = "BUG", "Bug report"
    SUPPORT = "SUPPORT", "Support request"
    HELP = "HELP", "Help"
    ACCOUNT = "ACCOUNT", "Account"
    BILLING = "BILLING", "Billing"
    OTHER = "OTHER", "Other"


# Priority values used to triage support urgency.
class TicketPriority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


# Persisted lifecycle states for a support ticket.
class TicketStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    ASSIGNED = "ASSIGNED", "Assigned"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


# Distinguish staff-created operational tickets from customer-raised tickets.
class TicketSource(models.TextChoices):
    INTERNAL = "INTERNAL", "Internal"
    CUSTOMER = "CUSTOMER", "Customer"


# Visibility controls whether a comment is customer-facing or support-only.
class CommentVisibility(models.TextChoices):
    PUBLIC = "PUBLIC", "Public"
    INTERNAL = "INTERNAL", "Internal"


# Audit vocabulary emitted by ticket services.
class TicketAuditAction(models.TextChoices):
    CREATED = "CREATED", "Created"
    UPDATED = "UPDATED", "Updated"
    ASSIGNED = "ASSIGNED", "Assigned"
    STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
    COMMENTED = "COMMENTED", "Commented"
    INTERNAL_NOTE_ADDED = "INTERNAL_NOTE_ADDED", "Internal note added"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED", "Attachment added"


# RBAC keys for ticket desk actions; creation remains available to active users.
class TicketPermission:
    # Ticket creation is deliberately keyless: any authenticated active user
    # may file a ticket, and participants always keep access to their thread.
    VIEW = "tickets.ticket.view"
    UPDATE = "tickets.ticket.update"
    MANAGE = "tickets.ticket.manage"
    ASSIGN = "tickets.ticket.assign"
    COMMENT = "tickets.comment.post"
    INTERNAL_NOTE = "tickets.internal_note.post"
    ATTACH = "tickets.attachment.create"
    AUDIT_VIEW = "tickets.audit.view"
    REPORT_VIEW = "tickets.report.view"


# Platform support staff are the only users with cross-tenant ticket desk span.
SUPPORT_USER_TYPES = {"CX_STAFF"}


# Allowed lifecycle moves; services reject transitions outside this graph.
VALID_STATUS_TRANSITIONS = {
    TicketStatus.OPEN: {TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.ASSIGNED: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.IN_PROGRESS: {TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},
    TicketStatus.CLOSED: {TicketStatus.IN_PROGRESS},
}
