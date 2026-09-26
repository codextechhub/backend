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
from vs_workflow.constants import DocumentAudience
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler
from vs_workflow.presentation import changes_section, document_details, fields_section

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
    # Schools and the platform both administer roles.
    audience = DocumentAudience.ALL
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
        """Identify the role change without flattening its permission list."""
        return {
            "title": document.target_role.name,
            "subtitle": "Role permission change",
            "fields": [
                {"label": "Raised by", "value": _display_name(document.requested_by)},
            ],
        }

    def get_document_details(self, document) -> dict:
        """Describe the request and every permission delta in decision order.

        Permission descriptions are used instead of internal keys wherever the
        catalogue provides them. Restricted additions retain an explicit marker
        because granting them is the action this approval safety boundary exists
        to control. Removing a restricted permission is not marked as risky.
        """
        items = list(document.delta_items.select_related("permission").all())
        return document_details(
            fields_section("Request details", [
                ("Reason", document.justification),
            ]),
            changes_section("Permission changes", [
                {
                    "operation": item.operation,
                    "label": item.permission.description or item.permission.key,
                    "restricted": (
                        item.operation == "ADD" and item.permission.is_restricted
                    ),
                }
                for item in items
            ]),
        )

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


GRANT_DOCUMENT_TYPE = "rbac.role_grant"
GRANT_SCHOOL_TEMPLATE_CODE = "role-grant"
GRANT_PLATFORM_TEMPLATE_CODE = "role-grant-platform"


@register_handler(GRANT_DOCUMENT_TYPE)
class RoleGrantWorkflowHandler(BaseWorkflowHandler):
    """Decides a role grant whose restricted keys the granter does not hold.

    The same authority as a role change, for the same reason: both hand
    somebody a restricted permission, and the ladder that decides one should
    decide the other. The requester may decide their own only when nobody else
    is on the stage, the case the module docstring argues for; where a second
    administrator exists, the grant waits for them. When the requester does
    decide, :func:`~vs_rbac.services.apply_role_grant_request` records it.

    Nothing is written before approval. The grant exists only once
    :meth:`on_approved` has run, so a rejection leaves nothing to take back.
    """

    document_type = GRANT_DOCUMENT_TYPE
    audience = DocumentAudience.ALL
    allows_requester_self_approval = True
    #: A school with a second administrator gets that person's decision: the
    #: requester is on the stage only when nobody else is.
    self_approval_only_when_alone = True
    allows_continue_without_approval = False

    def resolve_default_template_code(self, document) -> str:
        tenant = getattr(document, "tenant", None)
        if tenant is not None and getattr(tenant, "kind", "") == "PLATFORM":
            return GRANT_PLATFORM_TEMPLATE_CODE
        return GRANT_SCHOOL_TEMPLATE_CODE

    def validate_document(self, document, requested_by) -> None:
        from vs_rbac.models import TenantRoleGrantRequest
        from vs_workflow.exceptions import InvalidInstanceStateError

        if document.status != TenantRoleGrantRequest.Status.PENDING:
            raise InvalidInstanceStateError(
                f"This request has already been decided ({document.status}).",
            )

    def get_document_summary(self, document) -> dict:
        return {
            "title": f"{document.role.name} for {_display_name(document.user)}",
            "subtitle": "Role grant",
            "fields": [
                {"label": "Raised by", "value": _display_name(document.requested_by)},
                {"label": "Reach", "value": _reach_label(document)},
            ],
        }

    def get_document_details(self, document) -> dict:
        """Name the person, the role, its reach and what it hands them.

        Only the restricted permissions are listed: they are why the grant is
        here, and a role's full catalogue would bury them.
        """
        from vs_rbac.validators import role_restricted_permission_keys
        from vs_rbac.models import Permission

        restricted = sorted(role_restricted_permission_keys(document.role))
        described = dict(
            Permission.objects.filter(key__in=restricted).values_list("key", "description")
        )
        rows = [
            ("Person", _display_name(document.user)),
            ("Role", document.role.name),
            ("Reach", _reach_label(document)),
        ]
        if document.replaces_id:
            rows.append(("Replaces", document.replaces.role.name))
        if document.reason_note:
            rows.append(("Reason", document.reason_note))
        return document_details(
            fields_section("Request details", rows),
            changes_section("Restricted permissions it grants", [
                {
                    "operation": "ADD",
                    "label": described.get(key) or key,
                    "restricted": True,
                }
                for key in restricted
            ]),
        )

    def on_approved(self, instance, context: dict) -> None:
        """Write the grant. This is the only place a request's grant is written."""
        from vs_rbac.models import TenantRoleGrantRequest
        from vs_rbac.services import apply_role_grant_request

        request = TenantRoleGrantRequest.objects.filter(
            pk=instance.document_object_id,
        ).first()
        if request is None:
            return
        reviewer = _deciding_approver(instance) or request.requested_by
        apply_role_grant_request(
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
        """Mark the request decided so it leaves the person's profile."""
        from vs_rbac.models import TenantRoleGrantRequest

        request = TenantRoleGrantRequest.objects.filter(
            pk=instance.document_object_id,
            status=TenantRoleGrantRequest.Status.PENDING,
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

    def reversal_block_reason(self, document):
        """Refuse once the grant is written; the person already holds the role.

        Taking it back is withdrawing the grant from their profile, where it is
        recorded with a reason. A request still pending, or one that closed
        without writing anything, has nothing outside the engine to undo.
        """
        from vs_rbac.models import TenantRoleGrantRequest

        if document is None or document.status == TenantRoleGrantRequest.Status.PENDING:
            return None
        if document.assignment_id is None:
            return None
        return (
            "This role has already been granted, so the approval cannot be "
            "undone. Withdraw the role from their profile instead."
        )


def _reach_label(document) -> str:
    """Where the grant reaches, in the words the Access tab uses."""
    if document.branch_id:
        return document.branch.name
    if document.role.branch_ids:
        return "The role's own branches"
    return "School-wide"


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
