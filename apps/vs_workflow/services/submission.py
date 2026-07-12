"""Submit a document for workflow approval."""

from typing import Optional

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from vs_workflow.constants import AuditEventType, WorkflowInstanceStatus
from vs_workflow.exceptions import InvalidInstanceStateError, TemplateNotFoundError
from vs_workflow.handlers import get_handler
from vs_workflow.models import WorkflowInstance, WorkflowTemplate
from vs_workflow.services import audit as audit_service
from vs_workflow.services import routing as routing_service


# Create an approval instance and activate its first approvable stage.
def submit_for_approval(document, requested_by, *,
                         template_code: Optional[str] = None) -> WorkflowInstance:
    """Create a WorkflowInstance for document and activate its first stage.

    Template resolution uses a three-level cascade — branch-specific →
    school-wide → platform-wide — so a platform template acts as a fallback
    without forcing admins to duplicate it at every school and branch.
    Calling code must ensure the document declares workflow_document_type and
    that a matching handler is registered, otherwise InvalidInstanceStateError
    / UnknownDocumentTypeError are raised before anything is written.
    """
    document_type = getattr(document, "workflow_document_type", None)
    if not document_type:
        raise InvalidInstanceStateError(
            "Document must declare workflow_document_type attribute.")

    handler = get_handler(document_type)
    # The document handler owns domain-specific submit guards.
    handler.validate_document(document, requested_by)

    code = template_code or handler.resolve_default_template_code(document)
    tenant = getattr(document, "tenant", requested_by.tenant)
    branch = getattr(document, "branch", None)

    # Cascade: branch-specific → school-wide → platform-wide.
    scopes = [{"tenant": tenant, "branch": branch}]
    if branch is not None:
        scopes.append({"tenant": tenant, "branch": None})
    if tenant is not None or branch is not None:
        scopes.append({"tenant": None, "branch": None})

    template = None
    for scope in scopes:
        try:
            template = WorkflowTemplate.objects.get(
                document_type=document_type, code=code, **scope,
            )
            break
        except WorkflowTemplate.DoesNotExist:
            continue

    if template is None:
        raise TemplateNotFoundError(
            f"No template '{code}' for document_type '{document_type}'",
            code=code, document_type=document_type,
        )

    try:
        # Summary is best-effort display metadata; approval should not fail on it.
        document_summary = handler.get_document_summary(document) or {}
        if not isinstance(document_summary, dict):
            document_summary = {}
    except Exception:
        document_summary = {}

    with transaction.atomic():
        # Instance creation, audit, document callback, and first routing commit together.
        ct = ContentType.objects.get_for_model(type(document))
        instance = WorkflowInstance.objects.create(
            tenant=tenant, branch=branch, template=template,
            document_content_type=ct, document_object_id=str(document.pk),
            document_type=document_type, status=WorkflowInstanceStatus.SUBMITTED,
            requested_by=requested_by, submitted_at=timezone.now(),
            document_summary=document_summary,
        )
        audit_service.write(instance, AuditEventType.INSTANCE_SUBMITTED, actor=requested_by,
                            context={"template": template.code})
        handler.on_submitted(instance, {"template": template.code})
        routing_service.advance_instance(instance, current_attempt=1)
        return instance
