from django.contrib import admin

from .models import Ticket, TicketAttachment, TicketAuditLog, TicketComment


class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 0
    fields = ("author", "visibility", "body", "created_at")
    readonly_fields = ("created_at",)


class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 0
    fields = ("original_filename", "uploaded_by", "content_type", "size", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_number", "title", "status", "priority", "category", "requester", "assignee", "school", "created_at")
    list_filter = ("status", "priority", "category", "source", "school")
    search_fields = ("ticket_number", "title", "description", "requester__email", "assignee__email")
    readonly_fields = ("ticket_number", "created_at", "updated_at", "resolved_at", "closed_at")
    inlines = [TicketCommentInline, TicketAttachmentInline]


@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "author", "visibility", "created_at")
    list_filter = ("visibility", "created_at")
    search_fields = ("ticket__ticket_number", "body", "author__email")


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "original_filename", "uploaded_by", "content_type", "size", "created_at")
    search_fields = ("ticket__ticket_number", "original_filename", "uploaded_by__email")


@admin.register(TicketAuditLog)
class TicketAuditLogAdmin(admin.ModelAdmin):
    list_display = ("ticket", "action", "actor", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("ticket__ticket_number", "summary", "actor__email")
    readonly_fields = ("ticket", "actor", "action", "summary", "before_data", "after_data", "metadata", "created_at")
