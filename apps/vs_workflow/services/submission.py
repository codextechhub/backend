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


def submit_for_approval(document, requested_by, *,
                         template_code: Optional[str] = None) -> WorkflowInstance:
    document_type = getattr(document, "workflow_document_type", None)
    if not document_type:
        raise InvalidInstanceStateError(
            "Document must declare workflow_document_type attribute.")

    handler = get_handler(document_type)
    handler.validate_document(document, requested_by)

    code = template_code or handler.resolve_default_template_code(document)
    school = getattr(document, "school", None)
    branch = getattr(document, "branch", None)

    # Cascade: branch-specific → school-wide → platform-wide.
    scopes = [{"school": school, "branch": branch}]
    if branch is not None:
        scopes.append({"school": school, "branch": None})
    if school is not None or branch is not None:
        scopes.append({"school": None, "branch": None})

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

    with transaction.atomic():
        ct = ContentType.objects.get_for_model(type(document))
        instance = WorkflowInstance.objects.create(
            school=school, branch=branch, template=template,
            document_content_type=ct, document_object_id=str(document.pk),
            document_type=document_type, status=WorkflowInstanceStatus.SUBMITTED,
            requested_by=requested_by, submitted_at=timezone.now(),
        )
        audit_service.write(instance, AuditEventType.INSTANCE_SUBMITTED, actor=requested_by,
                            context={"template": template.code})
        handler.on_submitted(instance, {"template": template.code})
        routing_service.advance_instance(instance, current_attempt=1)
        return instance
