"""Template resolution and the "would any stage actually run" predicate.

Two callers must agree about a document that has not been submitted yet:

* :func:`vs_workflow.services.submission.submit_for_approval` - which template
  routes this document, and what happens when it does;
* a domain gate such as :func:`vs_finance.approvals.approval_required` - may
  this document take the direct-post path instead?

They used to answer from two hand-copied cascades, and a gate that only asked
*"does a template exist"* while the engine asked *"which stage would activate"*
disagreed the moment a ladder's stages were all conditional: the gate refused
the direct post for a document the engine would route past every approval stage
on, leaving no route to the ledger at all. Both questions are answered here now,
so the two cannot drift again.

Nothing in this module writes; it is safe to call on a read path.
"""
from __future__ import annotations

from typing import Optional, Tuple

from vs_workflow.conditions import evaluate_condition
from vs_workflow.constants import StageKind
from vs_workflow.exceptions import WorkflowError
from vs_workflow.models import WorkflowRoutePath, WorkflowTemplate

#: Cycle guard, mirroring ``advance_instance``'s own MAX_HOPS.
_MAX_HOPS = 50


# Work out which (tenant, branch) a document approves under.
def document_scope(document, *, default_tenant=None) -> Tuple[object, object]:
    """Return the ``(tenant, branch)`` a document's approval resolves against.

    A direct ``tenant`` attribute wins **including when it is explicitly None**:
    a platform-scoped document must gate on the platform template only, and
    falling back for it would let a tenant template capture it. Finance-style
    documents carry no tenant of their own and scope through their ledger
    entity's owning tenant. ``default_tenant`` covers documents with neither -
    submission passes the requester's home tenant, a read-side gate passes
    nothing and resolves platform-wide.
    """
    if hasattr(document, "tenant"):
        tenant = document.tenant
    elif getattr(document, "entity", None) is not None:
        tenant = document.entity.tenant
    else:
        tenant = default_tenant
    return tenant, getattr(document, "branch", None)


# Walk the branch to tenant to platform cascade for one document type.
def resolve_template(document_type: str, *, tenant, branch,
                     code: Optional[str] = None) -> Optional[WorkflowTemplate]:
    """The template that would route this document type at this scope, or None.

    Cascade: branch-specific → tenant-wide → platform-wide, so a platform
    template acts as a fallback without every school having to duplicate it.

    ``is_active`` is part of the lookup rather than a check afterwards: a tenant
    that switched its own version off must fall *through* to the next scope,
    which is the platform template. Filtering after the fact would find the
    inactive one and stop. ``code=None`` matches any code and is only for
    callers with no handler to ask - the engine always passes one.
    """
    scopes = [{"tenant": tenant, "branch": branch}]
    if branch is not None:
        scopes.append({"tenant": tenant, "branch": None})
    if tenant is not None or branch is not None:
        scopes.append({"tenant": None, "branch": None})

    filters = {"document_type": document_type, "is_active": True}
    if code is not None:
        filters["code"] = code

    for scope in scopes:
        # Ordered so a codeless lookup that matches more than one template picks
        # the same one every time rather than whatever the database offers.
        template = (WorkflowTemplate.objects.filter(**filters, **scope)
                    .order_by("code", "id").first())
        if template is not None:
            return template
    return None


# Decide whether any stage of a template would actually stop this document.
def template_requires_approval(template, document, *, stages=None,
                               has_routes=None) -> bool:
    """True when routing ``document`` through ``template`` would activate an
    approval stage.

    This is the read-only twin of ``advance_instance``: it walks from ENTRY with
    the same route/linear resolution and applies the same three skips - retired
    stages, BRANCH nodes, and APPROVAL stages whose ``inclusion_condition`` the
    document fails - stopping at the first stage that would genuinely activate.
    A ladder whose only conditional stage excludes this document therefore
    answers False, and the document is free to take the direct path.

    **What it deliberately does not model.** Whether a stage has any eligible
    approver is unknowable before an instance exists (``resolve_approvers``
    needs one), so a stage that would auto-skip itself for want of a holder
    still counts as requiring approval. That is the safe direction: finance
    ladders set ``skip_if_no_approvers=False`` precisely so an unstaffed stage
    parks the document rather than approving it.

    ``stages`` and ``has_routes`` may be passed by a caller resolving many
    documents against one template, which is what keeps a list view at one
    query per scope rather than one per row.

    Fails **closed**. A template we cannot evaluate - a malformed condition, an
    undecidable route - answers True, because the alternative is that a broken
    template silently becomes an approval bypass. Submitting the same document
    surfaces the underlying template error to whoever can fix it.
    """
    from vs_workflow.services.routing import readonly_next_stage

    if stages is None:
        stages = list(template.stages.order_by("order"))
    if not stages:
        # A template with no stages approves nothing; the engine would route
        # straight to APPROVED. Blocking the direct post here would leave the
        # document with no route to anywhere, so let it post as it did before
        # anybody published the template.
        return False
    if has_routes is None:
        has_routes = WorkflowRoutePath.objects.filter(template=template).exists()

    cursor = None
    try:
        for _ in range(_MAX_HOPS):
            nxt = readonly_next_stage(template, document, cursor, stages, has_routes)
            if nxt is None:
                return False  # Ran off the end without meeting an approver.
            if nxt.retired_at is not None or nxt.kind == StageKind.BRANCH:
                cursor = nxt
                continue
            if nxt.kind == StageKind.APPROVAL and nxt.inclusion_condition:
                matches, _trace = evaluate_condition(nxt.inclusion_condition, document)
                if not matches:
                    cursor = nxt
                    continue
            return True  # This stage would activate and wait for a decision.
    except WorkflowError:
        return True  # Undecidable template: gate it rather than bypass it.
    return True  # Hop limit hit - a cycle we will not resolve here.
