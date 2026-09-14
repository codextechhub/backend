"""The approval opt-in gate for finance documents.  # Decide whether a finance document must go through workflow.

Finance approvals are **opt-in by template** (design §7): a document type is
approval-gated *iff* a :class:`~vs_workflow.models.WorkflowTemplate` exists for it
at the document's ``(tenant, branch)`` scope, with the same branch → tenant →
platform cascade the engine's ``submit_for_approval`` uses. When no template
exists, the direct-post path behaves exactly as it did before - so approvals can
be switched on one document type and one tenant at a time, with zero migration.  # Keep the gate template-driven.

**A template existing is not the same question as a stage running.** The gate
answered on existence alone, which is right only while every ladder's first stage
is unconditional. Concessions and credit notes are meant to be gated only above a
threshold, so existence answered "approval required" for a ₦2,000 goodwill waiver
at every amount: the direct post was refused and no threshold was ever consulted.
The gate now asks the engine's own question - *would any stage of the resolved
template actually apply to this document* - via
:func:`vs_workflow.services.resolution.template_requires_approval`.

That is the half of the fix that generalises: any future ladder whose stages are
all conditional inherited the same bug, and journals, payouts and procurement
escaped it only because their first stage happens to be unconditional. The other
half is in :func:`_stages_payload` below, which now puts the threshold on the
first stage as well - while it was unconditional *something* always applied, so
no gate implementation could have let a small waiver through.

:func:`approval_required` is the single place that decision is made; both the
submit endpoint and the direct-post view read it so they can never disagree.  # Single source of truth.
:class:`ApprovalGate` is the same answer for many documents at one query per
distinct scope, for list endpoints that would otherwise ask per row.
"""
from __future__ import annotations


# Cache template resolution across many documents on one request.
class ApprovalGate:
    """Answers :func:`approval_required` for many documents without a query per row.

    The template a document resolves to varies only by
    ``(document_type, tenant, branch, code)``, so it is looked up once per
    distinct scope and reused; the stage list and route flag are cached with it.
    Whether a *particular* document clears the ladder's inclusion conditions is
    then decided in memory, because it depends on the document's own amount.

    Build one per request and throw it away. It deliberately holds no
    invalidation: a template published mid-request would not be seen, which is
    the right trade for a read that would otherwise cost 1000 queries.
    """

    def __init__(self):
        self._scopes = {}  # (document_type, tenant_id, branch_id, code) -> resolved or None

    # Resolve and memoise the template that routes this document's scope.
    def _resolved(self, document, document_type):
        from vs_workflow.exceptions import WorkflowError
        from vs_workflow.handlers import get_handler
        from vs_workflow.models import WorkflowRoutePath
        from vs_workflow.services.resolution import document_scope, resolve_template

        tenant, branch = document_scope(document)
        try:
            # The engine resolves by code, so the gate must too: a tenant whose
            # ladder sits under a different code is not gated by the one the
            # engine would never load.
            code = get_handler(document_type).resolve_default_template_code(document)
        except WorkflowError:
            # No handler registered - the type cannot be submitted at all, so
            # match on any code rather than inventing one.
            code = None

        key = (document_type, getattr(tenant, "pk", None),
               getattr(branch, "pk", None), code)
        if key not in self._scopes:
            template = resolve_template(
                document_type, tenant=tenant, branch=branch, code=code)
            if template is None:
                self._scopes[key] = None
            else:
                self._scopes[key] = (
                    template,
                    list(template.stages.order_by("order")),
                    WorkflowRoutePath.objects.filter(template=template).exists(),
                )
        return self._scopes[key]

    # Decide the gate for one document.
    def required(self, document) -> bool:
        """``True`` iff ``document`` must go through workflow approval."""
        from vs_workflow.services.resolution import template_requires_approval

        document_type = getattr(document, "workflow_document_type", None)  # Read the document type if the model exposes one.
        if not document_type:  # Documents without a workflow type never require approval.
            return False

        resolved = self._resolved(document, document_type)
        if resolved is None:  # No matching template means direct posting stays allowed.
            return False
        template, stages, has_routes = resolved
        return template_requires_approval(
            template, document, stages=stages, has_routes=has_routes)


# Handle the approval required workflow.
def approval_required(document) -> bool:
    """Return ``True`` iff ``document`` must go through workflow approval.

    True when a published :class:`~vs_workflow.models.WorkflowTemplate` resolves for
    the document's ``workflow_document_type`` at its ``(tenant, branch)`` scope -
    matched with the same branch-specific → tenant-wide → platform-wide cascade as
    :func:`vs_workflow.services.submission.submit_for_approval`, both going through
    :func:`vs_workflow.services.resolution.resolve_template` so the gate and the
    engine cannot resolve different templates - **and** at least one stage of that
    template would actually activate for this document. ``False`` when the document
    declares no ``workflow_document_type``, no matching template is published, or
    every stage of the resolved template is one this document skips.

    Resolving many documents? Use :class:`ApprovalGate`, which is this answer at
    one query per distinct scope instead of per document.
    """
    return ApprovalGate().required(document)


# --------------------------------------------------------------------------- #
# Default ladder provisioning                                                  #
# --------------------------------------------------------------------------- #

#: Doc-type token → (human label, template name, threshold-gated?) for the seeds.
#:
#: Refunds and write-offs are always gated: one moves cash out, the other concedes
#: income, and neither has a size at which a second pair of eyes stops being worth it.
#: Concessions and credit notes are gated only above a threshold, because a ₦2,000
#: goodwill allowance should not need a meeting and a ₦400,000 waiver should.
_ADJUSTMENT_TEMPLATES = {
    "finance.refund": ("refund", "Refund approval", False),
    "finance.write_off": ("write-off", "Write-off approval", False),
    "finance.concession": ("concession", "Concession approval", True),
    "finance.credit_note": ("credit note", "Credit-note approval", True),
}


#: The submit keys a published adjustment ladder makes load-bearing, and the
#: sensitivity each is registered at. Mirrors ``seed_finance_permissions``.
_ADJUSTMENT_SUBMIT_KEYS = {
    "concession": ("concessions", "submit", "SENSITIVE"),
    "creditnote": ("credit/debit notes", "submit", "SENSITIVE"),
}


def ensure_adjustment_submit_permissions():
    """Register the submit keys the ladders make load-bearing, and grant them.

    Publishing a ladder closes the direct-post route: ``/post/`` refuses while the
    gate applies, so ``finance.concession.submit`` becomes the *only* way a large
    waiver reaches the ledger. Those two keys were added with the gate, which means
    they exist only once ``seed_finance_permissions`` has run - and that is a separate
    command from the one that publishes the ladders.

    A deploy that ran one and not the other left a gated concession with **no route at
    all**: posting refused by the server, submitting hidden because the key was never
    registered or granted. Doing it here ties the two together at the only place that
    turns the gate on, so the ordering cannot be got wrong again.

    Idempotent and cheap: every write is a ``get_or_create`` on rows that almost always
    already exist. Grants go to the platform admin roles, the same ones the seed
    command grants to; a tenant's own roles receive keys through their own
    administration, not from here.
    """
    from vs_rbac.models import (
        Permission, PermissionAction, PermissionModule, PermissionResource,
        PermissionScope, TenantRolePermission, TenantRoleTemplate,
    )
    from vs_tenants.models import Tenant

    module, _ = PermissionModule.objects.get_or_create(
        name="finance",
        defaults={"description": "General ledger, receivables, banking, payroll, "
                                 "tax and reporting.", "is_active": True},
    )
    action, _ = PermissionAction.objects.get_or_create(
        name="submit",
        defaults={"description": "Submit a record for review or approval by another "
                                 "party.", "is_active": True},
    )

    permissions = []
    for resource_name, resource_label, action_name, sensitivity in (
        (name, label, verb, level)
        for name, (label, verb, level) in _ADJUSTMENT_SUBMIT_KEYS.items()
    ):
        resource, _ = PermissionResource.objects.get_or_create(
            module=module, name=resource_name,
            defaults={"description": f"{resource_label.capitalize()} (finance).",
                      "is_active": True},
        )
        key = f"finance.{resource_name}.{action_name}"
        permission = Permission.objects.filter(key=key).first()
        if permission is None:
            permission = Permission(
                module=module, resource=resource, action=action,
                description=f"Submit {resource_label}.",
                sensitivity_level=sensitivity, is_restricted=True, is_active=True,
                scope=PermissionScope.TENANT,
            )
            permission.save()
        permissions.append(permission)

    platform = Tenant.objects.filter(
        slug="codex", kind=Tenant.Kind.PLATFORM).first()
    if platform is None:
        # Nothing to grant to yet. The keys exist, which is the half that matters;
        # create_superuser and the seed command own the roles.
        return permissions

    for role_key in ("xvs_super_admin", "xvs_platform_admin"):
        role = TenantRoleTemplate.objects.filter(
            tenant=platform, key=role_key).first()
        if role is None:
            continue
        for permission in permissions:
            TenantRolePermission.objects.get_or_create(
                role=role, permission=permission,
                defaults={"granted": True, "granted_by": None},
            )
    return permissions


def _adjustment_models():
    """The finance documents these ladders route, keyed by their workflow type."""
    from .models import Concession, CreditNote, Refund, WriteOffRequest

    return {m.workflow_document_type: m for m in
            (Refund, WriteOffRequest, Concession, CreditNote)}


def _stages_payload(*, amount_field, threshold, gated, approver_group_code,
                    senior_group_code):
    """The stage list for one adjustment ladder.

    An always-gated type gets a single always-on stage. A threshold-gated type gets
    **both** stages conditioned on ``threshold``, which is the engine's own mechanism
    and the same shape procurement uses for high-value spend.

    The first stage carries the condition too, and that is the whole point of the
    threshold. Leaving it unconditional means *something* always applies and every
    concession is gated at every amount: a ₦2,000 goodwill allowance refused at
    ``/post/`` exactly as a ₦400,000 waiver is, which is the opposite of what this
    constant is for. Below ``threshold`` no stage applies, ``approval_required``
    answers False, and the allowance posts directly. At or above it the full
    ladder runs: the adjustment approver, then the senior one.

    ``skip_if_no_approvers=False`` on every stage: an adjustment must never approve
    itself because nobody happens to hold the role. An unstaffed stage parks the
    document and names the role to fill, and the engine's repair releases it the
    moment somebody is appointed.
    """
    stages = [{
        "code": "approver",
        "label": "Adjustment approval",
        "kind": "APPROVAL",
        "order": 10,
        "approver_source": "WORKFLOW_GROUP",
        "approver_group_code": approver_group_code,
        # Receivable adjustments are entity-scoped; a customer is not a branch.
        "approver_scope": "SCHOOL",
        "advance_rule": "ANY",
        "on_rejection": "TERMINAL",
        "skip_if_no_approvers": False,
    }]
    if not gated:
        return stages
    stages[0]["inclusion_condition"] = {
        "op": "gte", "field": amount_field, "value": int(threshold),
    }
    stages.append({
        "code": "senior",
        "label": "Senior adjustment approval",
        "kind": "APPROVAL",
        "order": 20,
        "approver_source": "WORKFLOW_GROUP",
        "approver_group_code": senior_group_code,
        "approver_scope": "SCHOOL",
        "advance_rule": "ANY",
        "on_rejection": "TERMINAL",
        "skip_if_no_approvers": False,
        "inclusion_condition": {
            "op": "gte", "field": amount_field, "value": int(threshold),
        },
    })
    return stages


def ensure_tenant_approval_templates(
    tenant,
    *,
    with_default_stages: bool = True,
    threshold: int | None = None,
    approver_group_code: str | None = None,
    senior_group_code: str | None = None,
    created_by=None,
) -> list:
    """Give one tenant its own adjustment-approval rules. Returns ``[(template, created)]``.

    Publishes one route per adjustment document type, scoped to the tenant
    (``tenant=<tenant>, branch=None``) so nothing outside this tenant can reach it.

    Two properties are deliberate and tested:

    * **Idempotent, and never destructive.** A document type whose tenant-scoped
      template carries a live step is left exactly as an administrator configured it
      and reported with ``created=False``, so this is safe to re-run and safe to call
      again for a second entity in the same tenant. A route holding no live step is
      the exception, and only when the default stages are being asked for: it is the
      placeholder published with the books, it carries nothing anybody chose, and the
      steps are written into it rather than the tenant being told it already has rules
      it cannot see.
    * **Seeded blocked, not seeded open.** Where stages are published they arrive with
      nobody appointed: each resolves its approvers through a named group the tenant
      has put nobody in, so the first adjustment submitted parks and names the group to
      fill rather than approving itself.

    ``with_default_stages`` chooses between the two things a tenant-scoped row is for:

    * **True** publishes the ladders and the approver groups their stages name. This is
      for a tenant that has *asked* for the default rules, which is what the seed
      command is.
    * **False** publishes the same rows carrying no stages, and creates no groups. This
      is for a tenant that has asked for nothing yet, where a ladder would be a guess at
      who approves its adjustments and at the amounts, and a group would be structure on
      its screens that nobody requested.

    The empty row is not the same as no row, and for adjustments that difference is the
    whole gate. With no row at all, ``approval_required`` answers False and a refund or
    a write-off posts straight to the ledger with nobody told. With the empty row, the
    post is approval-undecided rather than approval-free: it is refused until somebody
    confirms it in as many words, and the confirmation is recorded against them (see
    :func:`guard_direct_post`). The row also keeps the tenant off any shared platform
    route, so a change to a shared row can never begin governing this tenant's
    adjustments.

    The switch is a parameter rather than a sibling function because the two differ only
    in what each published row carries. The steps around that, resolving which document
    types exist already and leaving each of those untouched, are the non-destructive
    promise above, and a sibling would restate them and be free to drift from them.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    from .constants import (
        WF_ADJUSTMENT_APPROVER_GROUP,
        WF_ADJUSTMENT_THRESHOLD,
        WF_DEFAULT_TEMPLATE_CODE,
        WF_SENIOR_ADJUSTMENT_APPROVER_GROUP,
    )

    if tenant is None:
        raise ValueError("A tenant is required to seed its adjustment-approval rules.")

    threshold = WF_ADJUSTMENT_THRESHOLD if threshold is None else threshold
    approver_group_code = approver_group_code or WF_ADJUSTMENT_APPROVER_GROUP
    senior_group_code = senior_group_code or WF_SENIOR_ADJUSTMENT_APPROVER_GROUP

    models = _adjustment_models()
    document_types = list(_ADJUSTMENT_TEMPLATES)
    # all_objects deliberately: the explicit tenant filter is the boundary, and a row
    # hidden by ambient request-local scoping would be re-published over, which is the
    # destructive outcome this function promises never to cause.
    existing = {
        t.document_type: t
        for t in WorkflowTemplate.all_objects.filter(
            tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
            document_type__in=document_types,
        )
    }
    # An empty route is a placeholder, not a decision, so asking for the default
    # stages fills it in. Skipping it instead would make this a no-op for every
    # tenant whose books published the placeholder, and the tenant would be told it
    # already had rules while no adjustment could ever route through them. Retired
    # steps are history rather than configuration, so a route holding only those is
    # empty for this purpose exactly as the engine treats it as empty for routing.
    if with_default_stages:
        existing = {
            document_type: template
            for document_type, template in existing.items()
            if template.stages.filter(retired_at__isnull=True).exists()
        }

    # A tenant-scoped WORKFLOW_GROUP stage will not publish against a group the
    # tenant does not have. Created empty: a group nobody is in resolves to
    # nobody, so the ladder parks its first document rather than approving it. A
    # route published with no stages names no group, so it needs none.
    if with_default_stages:
        ensure_approver_group(
            tenant, approver_group_code,
            description="Approves receivable adjustments. Empty until the tenant "
                        "adds people, roles or positions to it, so adjustments park "
                        "until then.",
        )
        ensure_approver_group(
            tenant, senior_group_code,
            description="Approves high-value concessions and credit notes. Empty "
                        "until the tenant puts somebody in it.",
        )

    results = []
    for document_type, (label, name, gated) in _ADJUSTMENT_TEMPLATES.items():
        if document_type in existing:
            results.append((existing[document_type], False))
            continue
        results.append((
            publish_template(
                tenant=tenant, branch=None, document_type=document_type,
                code=WF_DEFAULT_TEMPLATE_CODE, name=name,
                description=(
                    f"Approval rule for a {label}."
                    if with_default_stages else
                    f"Approval route for a {label}, held by this tenant so its "
                    "adjustments are never governed by shared platform rules. The "
                    "steps are the tenant's own to add."
                ),
                created_by=created_by,
                stages_payload=_stages_payload(
                    amount_field=models[document_type].workflow_amount_field,
                    threshold=threshold, gated=gated,
                    approver_group_code=approver_group_code,
                    senior_group_code=senior_group_code,
                ) if with_default_stages else [],
            ),
            True,
        ))
    # The gate is on for this tenant only where steps were published, so the key
    # that lets somebody through it is registered only there too. See the
    # function's docstring for why this lives here.
    if with_default_stages:
        ensure_adjustment_submit_permissions()
    return results


def ensure_tenant_expense_claim_template(
    tenant,
    *,
    with_default_stages: bool = True,
    approver_group_code: str | None = None,
    created_by=None,
):
    """Publish the tenant's expense-claim approval route. Returns ``(template, created)``.

    ``with_default_stages`` chooses what the route carries, exactly as it does in
    :func:`ensure_tenant_approval_templates`:

    * **True** publishes the approving step and the group it names. The group arrives
      empty, so a claim parks visibly until an administrator puts somebody in it, and
      normal stage activation then creates their approval notices.
    * **False** publishes the route carrying no step, and creates no group. Who approves
      a staff reimbursement is the tenant's own answer, read from the organogram it
      builds, so nothing is invented at provisioning time.

    A route holding a live step is never republished over, from either direction: it is
    somebody's decision. A route holding none is the placeholder published with the
    books, so asking for the default step fills it in rather than reporting that rules
    already exist which no claim could route through.
    """
    from vs_workflow.models import WorkflowTemplate
    from vs_workflow.services.groups import ensure_approver_group
    from vs_workflow.services.templates import publish_template

    from .constants import (
        WF_DEFAULT_TEMPLATE_CODE,
        WF_EXPENSE_CLAIM_APPROVER_GROUP,
    )

    if tenant is None:
        raise ValueError("A tenant is required to seed its expense-claim approval rule.")

    approver_group_code = approver_group_code or WF_EXPENSE_CLAIM_APPROVER_GROUP
    existing = WorkflowTemplate.all_objects.filter(
        tenant=tenant,
        branch=None,
        document_type="finance.expense_claim",
        code=WF_DEFAULT_TEMPLATE_CODE,
    ).first()
    if existing is not None:
        # A live step is a decision and stays put. An empty route is the
        # placeholder the books published, so a deliberate ask fills it in.
        if existing.stages.filter(retired_at__isnull=True).exists() or not with_default_stages:
            return existing, False

    if with_default_stages:
        ensure_approver_group(
            tenant,
            approver_group_code,
            description="Approves staff expense claims. Empty until the tenant puts "
                        "somebody in it, so claims park until then.",
        )
    template = publish_template(
        tenant=tenant,
        branch=None,
        document_type="finance.expense_claim",
        code=WF_DEFAULT_TEMPLATE_CODE,
        name="Expense claim approval",
        description=(
            "Approval rule for a staff expense claim before it reaches the ledger."
            if with_default_stages else
            "Approval route for a staff expense claim, held by this tenant so its "
            "claims are never governed by shared platform rules. The steps are the "
            "tenant's own to add."
        ),
        created_by=created_by,
        stages_payload=[{
            "code": "finance-approval",
            "label": "Expense claim approval",
            "kind": "APPROVAL",
            "order": 10,
            "approver_source": "WORKFLOW_GROUP",
            "approver_group_code": approver_group_code,
            "approver_scope": "SCHOOL",
            "advance_rule": "ANY",
            "on_rejection": "RETURN_TO_REQUESTER",
            "skip_if_no_approvers": False,
        }] if with_default_stages else [],
    )
    return template, True


# --------------------------------------------------------------------------- #
# Direct-post guard                                                            #
# --------------------------------------------------------------------------- #
def guard_direct_post(document, request, *, noun="document"):
    """Refuse a direct post that should be approved, and confirm one that cannot be.

    Two different refusals, and collapsing them is what let money out.

    A document whose ladder would stop it must be submitted, not posted: that is
    the ordinary gate and it has always been here.

    A document whose tenant holds a template with **no stages** is the case this
    adds. It is not approval-free; it is approval-undecided. A tenant is given a
    template of its own so it can choose its stages, and until it does, that
    empty template stands in front of the shared platform ladder that would
    otherwise have caught the document. Posting anyway is a legitimate thing for
    a tenant to do - a requester should not be stuck because nobody has built the
    ladder yet - but it is a decision somebody makes, not a default the system
    takes for them. So it needs ``confirm_without_approval`` in the body, and it
    is written to the audit trail with the person's name against it.

    :raises ValidationError: gated, or unconfirmed with no ladder configured.
    """
    from rest_framework.exceptions import ValidationError

    from vs_workflow.services.resolution import (
        approval_unconfigured, record_unapproved_post,
    )

    if approval_required(document):
        raise ValidationError({
            "detail": f"This {noun} is approval-gated; submit it for approval "
                      f"instead of posting directly.",
        })
    confirm_unconfigured_post(document, request, noun=noun)


def confirm_unconfigured_post(document, request, *, noun="document"):
    """The confirmation half of :func:`guard_direct_post`, on its own.

    Endpoints that *route* rather than refuse - submit when a ladder would stop
    the document, post it otherwise - reach the direct path without ever calling
    the guard, and would skip the confirmation with it. They call this on their
    posting branch instead.
    """
    from rest_framework.exceptions import ValidationError

    from vs_workflow.services.resolution import (
        approval_unconfigured, record_unapproved_post,
    )

    if not approval_unconfigured(document):
        return

    body = request.data or {}
    if not body.get("confirm_without_approval"):
        from vs_workflow.exceptions import ApprovalNotConfiguredError

        raise ApprovalNotConfiguredError(
            f"No approval steps have been set up for this {noun}, so nobody "
            f"will review it. Confirm you want to post it without approval, or "
            f"set up the approval steps first."
        )

    record_unapproved_post(
        document,
        actor_user=getattr(request, "user", None),
        reason=str(body.get("reason") or "").strip(),
        tenant=getattr(request, "tenant", None),
    )
