"""Workflow handler for role permission changes.

Registered from :meth:`VsRbacConfig.ready` so the engine knows what to do when a
role change instance is approved, rejected, withdrawn or reversed.

**Why a role change is routed at all.** Every permission that bills a family or
moves money is marked restricted, and a restricted key cannot be granted by
editing a role: the server asks for a request instead. Something has to decide
those requests. The engine is what decides every other consequential act in the
product - a refund, a write-off, a purchase order, a payout batch - and a role
change belongs with them rather than beside them with rules of its own.

**Why the requester may decide their own.** A role change is raised by whoever
administers roles, and in most schools that is one person: the head teacher, who
holds the only key that can approve one as well. The engine's usual answer -
exclude the requester - leaves her stage with nobody on it, and both ways out of
that are worse than letting her act. See
:attr:`~vs_workflow.handlers.base.BaseWorkflowHandler.allows_requester_self_approval`
for the whole argument. Where a school does have a second administrator, the
ladder resolves them both and the ordinary two-person flow happens by itself.

**Nothing is granted before approval.** Unlike platform user creation, which
writes the account up front and finalises the invitation on approval, a role
change writes nothing at submission: the delta lives on the request, and
:func:`~vs_rbac.services.apply_role_change_request` replaces the role's grants
inside :meth:`on_approved`. So a rejected request leaves no permissions to take
back, and a reversal has nothing to undo unless the approval already ran.
"""
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler

#: The document type, and the two central templates that route it.
#:
#: Two rather than one because the same model serves a school changing its own
#: roles and CodeX changing the platform's, and the role that approves is not
#: the same in both. Both are tenant-less, so one row each serves every tenant
#: rather than a copy per school.
DOCUMENT_TYPE = "rbac.role_change"
SCHOOL_TEMPLATE_CODE = "role-change"
PLATFORM_TEMPLATE_CODE = "role-change-platform"


@register_handler(DOCUMENT_TYPE)
class RoleChangeWorkflowHandler(BaseWorkflowHandler):
    document_type = DOCUMENT_TYPE
    allows_requester_self_approval = True
    #: An unstaffed stage must not be released past. The generic release exists
    #: for documents where a stalled ladder blocks ordinary work; here the
    #: approval IS the safety boundary, and releasing without it is the same
    #: outcome as granting a restricted permission with nobody looking.
    allows_continue_without_approval = False

    def resolve_default_template_code(self, document) -> str:
        tenant = getattr(document, "tenant", None)
        if tenant is not None and getattr(tenant, "kind", "") == "PLATFORM":
            return PLATFORM_TEMPLATE_CODE
        return SCHOOL_TEMPLATE_CODE

    def validate_document(self, document, requested_by) -> None:
        from vs_rbac.models import TenantRoleChangeRequest
        from vs_workflow.exceptions import InvalidInstanceStateError

        if document.status != TenantRoleChangeRequest.Status.PENDING:
            raise InvalidInstanceStateError(
                f"This request has already been decided ({document.status}).",
            )
        if not document.delta_items.exists():
            raise InvalidInstanceStateError(
                "A role change request must name at least one permission.",
            )

    def get_document_summary(self, document) -> dict:
        """What the approver reads before deciding.

        **Permissions are described, not keyed.** ``finance.payout.approve`` is
        not a sentence a head teacher can weigh, and weighing it is exactly what
        the approver is being asked to do. The catalogue's own description is
        what the delta item carries, for this reason.

        **The restricted ones are marked in place rather than listed twice.** A
        separate "needs approval because" line repeated the change itself
        whenever there was only one, which is the common case: the reader saw the
        same sentence under two headings and had to work out that they were the
        same fact. The marker rides on the line it describes instead.
        """
        items = list(document.delta_items.select_related("permission").all())

        def describe(item):
            permission = item.permission
            wording = permission.description or permission.key
            verb = "Add" if item.operation == "ADD" else "Remove"
            # Only an addition needs approving. Taking a restricted permission
            # away is not the act the restriction guards.
            restricted = item.operation == "ADD" and permission.is_restricted
            return f"{verb} {wording}" + (" (needs approval)" if restricted else "")

        return {
            "title": f"{document.target_role.name}: {len(items)} permission change(s)",
            "subtitle": "Role permission change",
            "fields": [
                {"label": "Role", "value": document.target_role.name},
                {"label": "Raised by", "value": _display_name(document.requested_by)},
                {"label": "Reason", "value": document.justification or "-"},
                *({"label": "Change", "value": describe(item)} for item in items),
            ],
        }

    def on_approved(self, instance, context: dict) -> None:
        """Apply the delta. This is the only place a request's grants are written."""
        from vs_rbac.models import TenantRoleChangeRequest
        from vs_rbac.services import apply_role_change_request

        request = TenantRoleChangeRequest.objects.filter(
            pk=instance.document_object_id,
        ).first()
        if request is None:
            return
        # The reviewer of record is whoever cast the deciding vote. The engine
        # passes no actor on this callback - it has already written the vote -
        # so it is read from the action rows, which are the authority on who
        # approved anyway. A ladder that completed with no vote at all (every
        # stage skipped) leaves nobody to name, and the requester stands.
        reviewer = _deciding_approver(instance) or request.requested_by
        apply_role_change_request(
            obj=request,
            reviewer=reviewer,
            notes=(context or {}).get("comment", ""),
        )

    def on_rejected(self, instance, context: dict) -> None:
        self._close(instance, context)

    def on_withdrawn(self, instance, context: dict) -> None:
        self._close(instance, context)

    def on_cancelled(self, instance, context: dict) -> None:
        self._close(instance, context)

    def _close(self, instance, context: dict) -> None:
        """Mark the request decided so it leaves the queue.

        Withdrawal and cancellation are recorded as denials rather than given
        statuses of their own. What a reader of the roles screen needs to know is
        that the role did not change and the request is closed; how it closed is
        the engine's record to keep, and it keeps it in full.
        """
        from vs_rbac.models import TenantRoleChangeRequest

        request = TenantRoleChangeRequest.objects.filter(
            pk=instance.document_object_id,
            status=TenantRoleChangeRequest.Status.PENDING,
        ).first()
        if request is None:
            return
        request.mark_denied(
            reviewer=_actor(context),
            notes=(context or {}).get("comment", "") or "Closed without approval.",
        )
        request.save(update_fields=[
            "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
        ])

    def validate_reversal(self, instance, context: dict) -> None:
        """Refuse once the grants are in people's hands.

        Approval replaces the role's permissions, and every holder of that role
        has them from that moment - a session already open picks them up on its
        next request. Undoing the engine's record would leave those grants in
        place while the approval that produced them reads as never given, and
        nothing about anyone's access would change. Taking the permissions back
        is a separate decision, taken by editing the role, where it is recorded
        as what it is.

        A reversal on a stage the ladder has not finished changes nothing outside
        the engine and is allowed, as is one whose request row is gone.
        """
        from vs_rbac.models import TenantRoleChangeRequest
        from vs_workflow.exceptions import ReversalNotAllowedError

        request = TenantRoleChangeRequest.objects.filter(
            pk=instance.document_object_id,
        ).first()
        if request is None or request.status == TenantRoleChangeRequest.Status.PENDING:
            return None
        raise ReversalNotAllowedError(
            "These permissions have already been granted, so the approval "
            "cannot be undone. Edit the role to take them back instead.",
            request_status=request.status,
        )


def _deciding_approver(instance):
    """The approver whose vote completed the ladder, or None.

    Read the way ``routing._terminate_approved`` reads it for the notification
    it sends, so the person the audit names and the person the email names are
    the same one. Reversed votes are excluded: a vote that was taken back did
    not approve anything.
    """
    from vs_workflow.models import WorkflowStageAction

    action = (
        WorkflowStageAction.objects
        .filter(
            stage_instance__instance=instance,
            action="APPROVED",
            reversed_at__isnull=True,
            is_reversal_of__isnull=True,
        )
        .select_related("actor")
        .order_by("-acted_at")
        .first()
    )
    return action.actor if action else None


def _actor(context: dict):
    """The user the engine says acted, or None.

    Present on the withdraw and cancel callbacks, absent on approve and reject.
    """
    from vs_user.models import User

    actor_id = (context or {}).get("actor_id")
    return User.objects.filter(pk=actor_id).first() if actor_id else None


def _display_name(user) -> str:
    if user is None:
        return "-"
    full = f"{user.first_name} {user.last_name}".strip()
    return full or user.email
