"""Workflow handler for *_USER_CREATION document type.

Registered automatically via VsUserConfig.ready() so the workflow engine
knows what to do when a user-creation instance is approved or rejected.
"""
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler


@register_handler("PLATFORM_USER_CREATION")
class UserCreationWorkflowHandler(BaseWorkflowHandler):
    document_type = "PLATFORM_USER_CREATION"

    def resolve_default_template_code(self, document) -> str:
        return "p-user-creation"

    def get_document_summary(self, document) -> dict:
        def display(field):
            getter = getattr(document, f"get_{field}_display", None)
            return getter() if callable(getter) else (getattr(document, field, "") or "")

        full_name = (getattr(document, "full_name", "") or "").strip()
        email = getattr(document, "email", "") or ""
        return {
            "title": full_name or email or "New platform user",
            "subtitle": "Platform user creation",
            "fields": [
                {"label": "Email", "value": email or "—"},
                {"label": "User type", "value": display("user_type") or "—"},
                {"label": "Role", "value": getattr(document, "role", "") or "—"},
                {"label": "Status", "value": display("status") or "—"},
                {"label": "Phone", "value": getattr(document, "phone", "") or "—"},
            ],
        }

    def validate_document(self, document, requested_by) -> None:
        from vs_user.models import User
        from vs_workflow.exceptions import WorkflowError
        if document.user_type != User.UserType.CX_STAFF:
            raise WorkflowError(
                "Workflow approval is only required for platform (CX_STAFF) user creation.",
                error_code="INVALID_DOCUMENT_STATE",
            )
        if document.status != User.Status.PENDING_APPROVAL:
            raise WorkflowError(
                "User must be in PENDING_APPROVAL status to submit for creation approval.",
                error_code="INVALID_DOCUMENT_STATE",
            )

    def on_approved(self, instance, context: dict) -> None:
        from vs_user.models import User
        from vs_user.services.user import UserCreationService
        try:
            user = User.objects.get(pk=instance.document_object_id)
        except User.DoesNotExist:
            return
        UserCreationService.finalize_invitation(
            user=user, requested_by=instance.requested_by,
        )

    def on_rejected(self, instance, context: dict) -> None:
        from vs_user.models import User, PositionAssignment
        from vs_user.services.organogram import OrganogramService
        try:
            user = User.objects.get(pk=instance.document_object_id)
        except User.DoesNotExist:
            return
        user.status = User.Status.REJECTED
        user.is_active = False
        user.save(update_fields=["status", "is_active", "updated_at"])

        # Vacate any seat reserved for this hire at creation time — a rejected
        # hire must not keep occupying an organogram position.
        for assignment in PositionAssignment.objects.filter(user=user, end_date__isnull=True):
            OrganogramService.end_assignment(assignment)

    def on_withdrawn(self, instance, context: dict) -> None:
        self.on_rejected(instance, context)

    def on_cancelled(self, instance, context: dict) -> None:
        self.on_rejected(instance, context)
