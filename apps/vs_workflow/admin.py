"""Minimal admin registrations for vs_workflow."""
from django.contrib import admin
from vs_workflow.models import (
    WorkflowTemplate, WorkflowStage, WorkflowRoutePath, WorkflowInstance,
    WorkflowStageInstance, WorkflowStageApprover, WorkflowStageAction,
    ApprovalDelegation, WorkflowApproverGroup, WorkflowApproverGroupMember,
    WorkflowAuditLog,
)

# Inspect published templates and their school/branch scope.
@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("code","document_type","school","branch","updated_at")
    list_filter = ("document_type",)
    search_fields = ("code","name","document_type")

# Inspect ordered stages that drive approval routing.
@admin.register(WorkflowStage)
class WorkflowStageAdmin(admin.ModelAdmin):
    list_display = ("code","label","template","kind","order","advance_rule")

# Inspect live and terminal workflow instances by document.
@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(admin.ModelAdmin):
    list_display = ("id","document_type","document_object_id","status","requested_by","submitted_at")
    list_filter = ("status","document_type")

# Inspect immutable workflow audit events.
@admin.register(WorkflowAuditLog)
class WorkflowAuditLogAdmin(admin.ModelAdmin):
    list_display = ("instance","event_type","actor","occurred_at")
    list_filter = ("event_type",)
    readonly_fields = ("instance","event_type","stage_instance","actor","context","message","occurred_at")

# Inspect named approver pools and their mixed membership.
class WorkflowApproverGroupMemberInline(admin.TabularInline):
    model = WorkflowApproverGroupMember
    extra = 0
    autocomplete_fields = ()

@admin.register(WorkflowApproverGroup)
class WorkflowApproverGroupAdmin(admin.ModelAdmin):
    list_display = ("name","code","tenant","branch","is_active","updated_at")
    list_filter = ("is_active",)
    search_fields = ("code","name")
    inlines = [WorkflowApproverGroupMemberInline]

for model in [WorkflowRoutePath, WorkflowStageInstance, WorkflowStageApprover,
              WorkflowStageAction, ApprovalDelegation]:
    admin.site.register(model)
