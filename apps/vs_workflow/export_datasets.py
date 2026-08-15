"""Workflow datasets published to the Export Centre.

Registered from :meth:`vs_workflow.apps.VsWorkflowConfig.ready`. Tenant-scoped: an
approval belongs to the organisation, and one instance may govern a document in any
module, so there is no single set of books to attach it to.

This is the dataset that answers "what is sitting unapproved, and with whom" without
anybody having to open the workflow console.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_DATE_RANGE,
    FILTER_TEXT,
    KIND_DATETIME,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    register,
)


# Build the tenant-scoped base queryset for approval instances.
def _instances(scope):
    from .models import WorkflowInstance

    return WorkflowInstance.objects.filter(tenant=scope.tenant)


# Register every workflow dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="workflow.approvals",
        module="Workflow",
        name="Approval requests",
        description=(
            "Every document that entered an approval workflow, what stage it reached "
            "and how long it has been waiting. Spans all modules."
        ),
        base=_instances,
        scope=DatasetScope.TENANT,
        permission="workflow.instance.view",
        row_cap=200_000,
        default_columns=("instance_id", "document_type", "status", "submitted_at"),
        fields=(
            Field("instance_id", "Request", "Request", KIND_TEXT, source="id", locked=True),
            Field("document_type", "Document type", "Request", KIND_TEXT),
            Field("document_reference", "Document", "Request", KIND_TEXT,
                  source="document_object_id"),
            Field("template_name", "Workflow", "Request", KIND_TEXT, source="template__name"),
            Field("status", "Status", "Request", KIND_TEXT),
            Field("current_stage", "Current stage", "Request", KIND_TEXT,
                  source="current_stage__label"),
            Field("requested_by", "Requested by", "People", KIND_TEXT,
                  source="requested_by__email"),
            Field("submitted_at", "Submitted", "Timeline", KIND_DATETIME),
            Field("completed_at", "Completed", "Timeline", KIND_DATETIME),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("submitted_at", "Submitted", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_TEXT),
            FilterDef("document_type", "Document type", FILTER_TEXT),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the all-instances list screen's filters into export filters.
#
# The rare happy case: every filter the screen offers has an exact counterpart on
# the dataset, so a quick export from this table matches it row for row.
def _translate_instances(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "value": value})
    if value := params.get("document_type"):
        filters.append({"id": "document_type", "value": value})
    return filters, unmapped


# Register the workflow screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="workflow.instances",
        handles=(
            "status", "document_type",
        ),
        label="Workflow - Approval requests",
        dataset_key="workflow.approvals",
        translate=_translate_instances,
    ))
