"""Minimal admin registrations for vs_workflow."""
from django.contrib import admin
from vs_workflow.models import (
    WorkflowTemplate, WorkflowStage, WorkflowRoutePath, WorkflowInstance,
    WorkflowStageInstance, WorkflowStageApprover, WorkflowStageAction,
    ApprovalDelegation, WorkflowAuditLog,
)

@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("code","document_type","school","branch","updated_at")
    list_filter = ("document_type",)
    search_fields = ("code","name","document_type")

@admin.register(WorkflowStage)
class WorkflowStageAdmin(admin.ModelAdmin):
    list_display = ("code","label","template","kind","order","advance_rule")

@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(admin.ModelAdmin):
    list_display = ("id","document_type","document_object_id","status","requested_by","submitted_at")
    list_filter = ("status","document_type")

@admin.register(WorkflowAuditLog)
class WorkflowAuditLogAdmin(admin.ModelAdmin):
    list_display = ("instance","event_type","actor","occurred_at")
    list_filter = ("event_type",)
    readonly_fields = ("instance","event_type","stage_instance","actor","context","message","occurred_at")

for model in [WorkflowRoutePath, WorkflowStageInstance, WorkflowStageApprover,
              WorkflowStageAction, ApprovalDelegation]:
    admin.site.register(model)
