from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title", "assignee", "assigned_by", "priority",
        "deadline", "is_done", "status", "department",
    )
    list_filter = ("priority", "is_done", "department")
    search_fields = ("title", "description", "assignee__email", "assignee__first_name")
    raw_id_fields = ("assignee", "assigned_by")
    readonly_fields = ("completed_at", "created_at", "updated_at")
    date_hierarchy = "deadline"

    @admin.display(description="Status")
    def status(self, obj):
        return obj.status
