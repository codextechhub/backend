"""Submit a document for workflow approval."""

from typing import Optional

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from vs_workflow.constants import AuditEventType, WorkflowInstanceStatus
from vs_workflow.exceptions import (
    CrossTenantDocumentError, InvalidInstanceStateError, TemplateNotFoundError,
)
from vs_workflow.handlers import get_handler
from vs_workflow.models import WorkflowInstance
from vs_workflow.services import audit as audit_service
from vs_workflow.services import routing as routing_service
from vs_workflow.services.resolution import document_scope, resolve_template


# Create an approval instance and activate its first approvable stage.
def submit_for_approval(document, requested_by, *,
                         template_code: Optional[str] = None,
                         confirm_without_approval: bool = False,
                         confirmation_reason: str = "") -> WorkflowInstance:
    """Create a WorkflowInstance for document and activate its first stage.

    Template resolution uses a three-level cascade - branch-specific →
    school-wide → platform-wide - so a platform template acts as a fallback
    without forcing admins to duplicate it at every school and branch. Only
    active templates take part: a tenant that adjusted a shared template and
    then asked for the platform version back has its own switched off, and this
    cascade is where that decision takes effect.

    **A template with no stages refuses rather than approving instantly.** With
    no stages, ``_pick_next_stage`` returns None on the first hop and the
    instance terminates APPROVED before anybody has seen it - and a terminal
    approved instance is exactly what a payout batch presents as its authority
    to move money. So an empty template is treated here the way the finance
    posting path treats it: not as "no approval needed" but as "nobody has said
    what approving this looks like", which is a refusal until somebody says in
    as many words that this document goes out unreviewed. ``confirm_without_approval``
    is that sentence, and it is recorded against the caller's name.

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
    # Scope and cascade both live in services.resolution, because the domain
    # gates that decide whether a document may skip approval entirely have to
    # reach the same template this does - see vs_finance.approvals.
    tenant, branch = document_scope(document, default_tenant=requested_by.tenant)
    _assert_own_tenant(tenant, requested_by)
    template = resolve_template(document_type, tenant=tenant, branch=branch, code=code)

    if template is None:
        raise TemplateNotFoundError(
            f"No template '{code}' for document_type '{document_type}'",
            code=code, document_type=document_type,
        )

    # A template with no stages. The router refuses one outright
    # (``TemplateInvalidError: Template has no stages``), which is a true
    # statement about the configuration and a dead end for the person holding
    # the document: nothing they can do makes it move. So the refusal is made
    # answerable here instead - a named code, and a way through that is recorded
    # rather than forbidden.
    # Retired steps are history, not configuration. A step a real document ran
    # through is kept as the record of how that document was approved, so a
    # template can hold rows and still have nothing that will run. Counting them
    # here would pass this gate and leave the router skipping each one in turn
    # until it ran out and terminated the instance APPROVED, which is the exact
    # outcome this refusal exists to prevent.
    unconfigured = not template.stages.filter(retired_at__isnull=True).exists()
    if unconfigured:
        from vs_workflow.exceptions import ApprovalNotConfiguredError
        from vs_workflow.services.resolution import record_unapproved_post

        if not confirm_without_approval:
            raise ApprovalNotConfiguredError(
                "No approval steps have been set up for this document, so "
                "nobody will review it. Confirm you want to send it anyway, or "
                "set up the approval steps first."
            )
        record_unapproved_post(
            document, actor_user=requested_by, reason=confirmation_reason,
            tenant=tenant,
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
        if unconfigured:
            # Straight to approved, without the router: it would raise on a
            # template with no stages, and there is nothing for it to route to.
            # The instance is still written, so the document has the same
            # terminal record it would have had if a ladder had passed it, and
            # the audit entry above says a person chose this.
            routing_service.terminate_approved(instance)
        else:
            routing_service.advance_instance(instance, current_attempt=1)
        return instance


# Refuse a submission that would file into a tenant the submitter does not belong to.
def _assert_own_tenant(tenant, requested_by) -> None:
    """Guard the one invariant every submit path shares.

    ``document_scope`` derives the owning tenant from the *document*, never from
    the caller, which is exactly right for choosing the template cascade and
    exactly wrong as an authorisation answer. Any caller that hands over a
    document it did not scope to the requester first would otherwise create the
    instance inside the document's tenant, notify that tenant's approvers, and
    run the handler's ``on_submitted`` against their record.

    The check lives here rather than in each caller because all four submit
    paths (finance's direct calls, procurement's wrapper, payments, user
    creation) funnel through this function, and the next module to be added
    will too. A view-level guard protects only the view that remembers it.

    Scope, not permission: whether the requester may submit *this kind of*
    document is the module's own ``rbac_permission``, and whether they may see
    this particular row is the module's own queryset. This answers only "is it
    even their tenant's document", which no module can accidentally omit.

    Branch is deliberately not checked. A tenant-level finance officer
    legitimately submits a branch document, so branch scope belongs to the
    module's queryset, where the caller's grants are known.
    """
    from vs_rbac.permissions import is_vision_super_admin

    caller_tenant = getattr(requested_by, "tenant", None)
    if tenant is not None and caller_tenant is not None and caller_tenant.pk == tenant.pk:
        return
    if tenant is None:
        # No document in the registry resolves to a null tenant today: User,
        # LedgerEntity and Tenant all carry non-nullable owners. Reaching here
        # means a document type was added whose scope the engine cannot
        # establish, so refuse rather than let it resolve platform-wide.
        if getattr(requested_by, "is_superuser", False) or is_vision_super_admin(requested_by):
            return
    raise CrossTenantDocumentError()
