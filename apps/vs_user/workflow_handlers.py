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
                {"label": "Email", "value": email or "-"},
                # The "User type" row is gone with the column. It said "CX
                # Staff" on every card this handler ever rendered - the handler
                # only accepts platform users - so it told an approver nothing
                # the title did not. Role is what distinguishes one card here
                # from the next, and it is already the line below.
                {"label": "Role", "value": getattr(document, "role", "") or "-"},
                {"label": "Status", "value": display("status") or "-"},
                {"label": "Phone", "value": getattr(document, "phone", "") or "-"},
            ],
        }

    def validate_document(self, document, requested_by) -> None:
        from vs_user.models import User
        from vs_workflow.exceptions import WorkflowError
        if not document.is_platform_user:
            raise WorkflowError(
                "Workflow approval is only required for platform user creation.",
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
        from vs_rbac.models import TenantUserRoleAssignment
        from vs_user.models import User, PositionAssignment
        from vs_user.services.organogram import OrganogramService
        try:
            user = User.objects.get(pk=instance.document_object_id)
        except User.DoesNotExist:
            return
        user.status = User.Status.REJECTED
        # is_active is derived from status in User._sync_is_active and is not
        # set by hand here; it stays in update_fields because save() writes it.
        user.save(update_fields=["status", "is_active", "updated_at"])

        # Vacate any seat reserved for this hire at creation time - a rejected
        # hire must not keep occupying an organogram position.
        for assignment in PositionAssignment.objects.filter(user=user, end_date__isnull=True):
            OrganogramService.end_assignment(assignment)

        # ...and withdraw the role grant that came with the seat.
        #
        # ``UserCreationService.create_pending`` writes a TenantUserRoleAssignment
        # at creation time, BEFORE the approver has seen the request - it has to,
        # because the role is what the approval card shows them. Rejecting the
        # hire used to vacate the position and leave that grant ACTIVE, so the
        # permissions the approver declined to hand over stayed attached to the
        # account, and ``get_effective_permissions`` still returned them.
        #
        # Sign-in is closed to a REJECTED account now, but that is not a reason
        # to leave the grant: it makes closed sign-in the only thing standing
        # between a refused hire and a live set of keys, and every route that
        # could ever revive the account - a status correction, a restore from
        # backup, a future re-approval, an impersonation session - would find
        # them already there. Rejection should vacate the role for the same
        # reason it vacates the seat.
        #
        # Revoked, not deleted, so the audit answer is unchanged: the row stays
        # with ``revoked_at``, ``revoked_by`` and a reason, and the history of
        # what was asked for and refused is still readable.
        # The engine hands the rejecting approver's id to the withdraw and
        # cancel paths but not to the reject path (routing._terminate_rejected
        # passes only the comment), so this is best-effort and the column is
        # nullable. Recording nobody is better than recording the requester,
        # who is the one person here who did not do the revoking.
        revoked_by = None
        if actor_id := (context or {}).get("actor_id"):
            revoked_by = User.objects.filter(pk=actor_id).first()

        for grant in TenantUserRoleAssignment.objects.filter(
            user=user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ):
            grant.revoke(
                by_user=revoked_by,
                reason="User creation was not approved.",
            )
            grant.save(update_fields=[
                "assignment_status", "revoked_at", "revoked_by",
                "reason_note", "updated_at",
            ])

    def on_withdrawn(self, instance, context: dict) -> None:
        self.on_rejected(instance, context)

    def on_cancelled(self, instance, context: dict) -> None:
        self.on_rejected(instance, context)
