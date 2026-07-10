from django.db import models


class TicketCategory(models.TextChoices):
    BUG = "BUG", "Bug report"
    SUPPORT = "SUPPORT", "Support request"
    HELP = "HELP", "Help"
    ACCOUNT = "ACCOUNT", "Account"
    BILLING = "BILLING", "Billing"
    OTHER = "OTHER", "Other"


class TicketPriority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


class TicketStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    ASSIGNED = "ASSIGNED", "Assigned"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


class TicketSource(models.TextChoices):
    INTERNAL = "INTERNAL", "Internal"
    CUSTOMER = "CUSTOMER", "Customer"


class CommentVisibility(models.TextChoices):
    PUBLIC = "PUBLIC", "Public"
    INTERNAL = "INTERNAL", "Internal"


class TicketAuditAction(models.TextChoices):
    CREATED = "CREATED", "Created"
    UPDATED = "UPDATED", "Updated"
    ASSIGNED = "ASSIGNED", "Assigned"
    STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
    COMMENTED = "COMMENTED", "Commented"
    INTERNAL_NOTE_ADDED = "INTERNAL_NOTE_ADDED", "Internal note added"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED", "Attachment added"


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


SUPPORT_USER_TYPES = {"CX_STAFF"}


VALID_STATUS_TRANSITIONS = {
    TicketStatus.OPEN: {TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.ASSIGNED: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.IN_PROGRESS: {TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},
    TicketStatus.CLOSED: {TicketStatus.IN_PROGRESS},
}
