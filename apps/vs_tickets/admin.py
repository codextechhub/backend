from django.contrib import admin

from .models import Ticket, TicketAttachment, TicketAuditLog, TicketComment


# Show ticket conversation rows inline while preserving their creation timestamp.
class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 0
    fields = ("author", "visibility", "body", "created_at")
    readonly_fields = ("created_at",)


# Show uploaded evidence inline with immutable file metadata.
class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 0
    fields = ("original_filename", "uploaded_by", "content_type", "size", "created_at")
    readonly_fields = ("created_at",)


# Inspect support tickets by lifecycle, urgency, and tenant ownership.
@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_number", "title", "status", "priority", "category", "requester", "assignee", "tenant", "created_at")
    list_filter = ("status", "priority", "category", "source", "tenant")
    search_fields = ("ticket_number", "title", "description", "requester__email", "assignee__email")
    readonly_fields = ("ticket_number", "created_at", "updated_at", "resolved_at", "closed_at")
    inlines = [TicketCommentInline, TicketAttachmentInline]


# Inspect public replies and internal notes independently from the ticket page.
@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "author", "visibility", "created_at")
    list_filter = ("visibility", "created_at")
    search_fields = ("ticket__ticket_number", "body", "author__email")


# Inspect uploaded ticket files and their uploader metadata.
@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "original_filename", "uploaded_by", "content_type", "size", "created_at")
    search_fields = ("ticket__ticket_number", "original_filename", "uploaded_by__email")


# Inspect immutable ticket audit events for support investigations.
@admin.register(TicketAuditLog)
class TicketAuditLogAdmin(admin.ModelAdmin):
    list_display = ("ticket", "action", "actor", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("ticket__ticket_number", "summary", "actor__email")
    readonly_fields = ("ticket", "actor", "action", "summary", "before_data", "after_data", "metadata", "created_at")
