"""Who can approve spend here, and where nobody can.

An approval ladder is only as real as the people who hold its roles. A tenant can
have perfectly good rules and still be unable to buy anything, because the stage that
runs at one site resolves to nobody: the document then parks (see
:mod:`vs_procurement.approval_parking`) and the administrator has to work out *why* from
the outside. This module answers that question directly, per branch and per stage, and
names the gaps as gaps.

It is a read-only projection of two things the engine already owns:

* **the rules** - the template ``vs_workflow`` would resolve for a document in that
  scope, through the engine's own branch → tenant → platform cascade, so the screen
  reports the ladder that would actually run rather than the seeded defaults;
* **the people** - resolved through
  :func:`vs_workflow.services.approvers.role_holder_ids`, the engine's own
  eligibility lookup. It is the same call routing makes, so this report and live
  routing can never disagree about who is eligible.

Nothing here writes, and nothing here re-implements scope resolution. Two costs are
deliberately bounded: templates are loaded once for the whole tenant and cascaded in
Python, and each ``(role key, scope, branch)`` holder lookup is memoised across
document types, so a four-document, two-stage ladder over N branches costs at most
``2 x (N + 1)`` RBAC queries, not ``8 x (N + 1)``.

One caveat is stated rather than hidden: eligibility is resolved *now*, and the engine
freezes its own snapshot when a stage activates. A person listed here approves future
work; documents already parked become reachable through the parking repair, not through
this report.
"""
from __future__ import annotations

from vs_workflow.constants import ApproverScope, ApproverSource
from vs_workflow.services.approvers import role_holder_ids, stage_role_key

from .constants import (
    PROCUREMENT_APPROVAL_TYPES,
    WF_DEFAULT_TEMPLATE_CODE,
)


#: Human labels for the four approvable document types, so the screen never has to
#: display a ``document_type`` token. Domain-neutral on purpose.
DOCUMENT_TYPE_LABELS = {
    "procurement.requisition": "Requisition",
    "procurement.purchase_order": "Purchase order",
    "procurement.vendor_invoice": "Vendor invoice",
    "procurement.vendor_payment": "Vendor payment",
}

#: Where the rules being reported came from, in cascade order.
RULES_SOURCE_BRANCH = "BRANCH"
RULES_SOURCE_TENANT = "TENANT"
RULES_SOURCE_PLATFORM = "PLATFORM"


def _person(user) -> dict:
    """Identify one approver by display name only - never an email or account field."""
    return {
        "id": user.pk,
        "name": (
            (getattr(user, "full_name", "") or "").strip()
            or user.get_full_name()
            or "Unknown user"
        ),
    }


def _load_templates(tenant):
    """Every template that could win the cascade for this tenant, in one query.

    Loaded with stages prefetched and keyed by ``(tenant_id, branch_id, document_type)``
    so the cascade below is pure Python. Retired stages are dropped: the engine skips
    them in all future routing, so reporting them would describe a ladder that no longer
    runs.
    """
    from django.db.models import Prefetch, Q
    from vs_workflow.models import WorkflowStage, WorkflowTemplate

    rows = (
        # all_objects deliberately: the explicit tenant filter below is the boundary,
        # and it must not depend on ambient request-local tenant state (this service is
        # also reachable from a management command, where there is none).
        WorkflowTemplate.all_objects
        .filter(
            document_type__in=PROCUREMENT_APPROVAL_TYPES,
            code=WF_DEFAULT_TEMPLATE_CODE,
        )
        .filter(Q(tenant=tenant) | Q(tenant__isnull=True))
        .prefetch_related(Prefetch(
            "stages",
            queryset=WorkflowStage.objects.filter(retired_at__isnull=True).order_by("order", "id"),
            to_attr="active_stages",
        ))
    )
    return {
        (template.tenant_id, template.branch_id, template.document_type): template
        for template in rows
    }


def _resolve_template(templates, tenant, branch, document_type):
    """The template ``vs_workflow`` would use, and where it came from.

    Mirrors ``vs_workflow.services.submission``'s branch → tenant → platform cascade
    exactly; returning the source lets the screen say "this site inherits the tenant's
    rules" instead of implying every scope has its own.
    """
    tenant_id = getattr(tenant, "pk", None)
    branch_id = getattr(branch, "pk", None)
    candidates = []
    if branch_id is not None:
        candidates.append(((tenant_id, branch_id, document_type), RULES_SOURCE_BRANCH))
    candidates.append(((tenant_id, None, document_type), RULES_SOURCE_TENANT))
    candidates.append(((None, None, document_type), RULES_SOURCE_PLATFORM))
    for key, source in candidates:
        template = templates.get(key)
        if template is not None:
            return template, source
    return None, None


class _HolderCache:
    """Memoised "who holds this key in this scope" over one report.

    The same role key appears on the same stage of all four document ladders, so
    without this the report would ask RBAC the identical question four times per branch.
    """

    def __init__(self, tenant):
        self._tenant = tenant
        self._holders: dict = {}

    def holders(self, *, role_key: str, scope: str, branch):
        if not role_key:
            return []
        # Branch only narrows the lookup for a branch-scoped stage; every other
        # scope counts tenant-wide holders, which is what the engine does.
        branch_arg = branch if scope == ApproverScope.BRANCH else None
        key = (role_key, scope, getattr(branch_arg, "pk", None))
        if key not in self._holders:
            from django.contrib.auth import get_user_model
            ids = role_holder_ids(
                role_key=role_key, tenant=self._tenant, branch=branch_arg,
            )
            self._holders[key] = [
                _person(user)
                for user in get_user_model().objects
                .filter(pk__in=ids)
                .order_by("first_name", "last_name", "pk")
            ]
        return self._holders[key]


def _stage_row(stage, *, branch, cache, rules_source) -> dict:
    """Project one stage into "who can approve this here", or the gap where nobody can."""
    by_organogram = stage.approver_source == ApproverSource.ORGANOGRAM
    # An organogram stage resolves relative to whoever raises the document, so there is
    # no fixed list of people to report. Say that, rather than reporting a false gap.
    # Only a role-sourced stage has a fixed list of people to report. Groups and
    # document-driven rules resolve per document, like the organogram does.
    by_role = stage.approver_source == ApproverSource.ROLE
    approvers = cache.holders(
        role_key=stage_role_key(stage),
        scope=stage.approver_scope, branch=branch,
    ) if by_role else []
    return {
        "stage_code": stage.code,
        "stage_label": stage.label,
        "role_key": stage_role_key(stage) if by_role else "",
        "approver_scope": stage.approver_scope,
        "resolved_per_requester": not by_role,
        "rules_source": rules_source,
        "approvers": approvers,
        "approver_count": len(approvers),
        # The whole point of the screen: a stage with nobody behind it blocks every
        # document that reaches it, and only an administrator can fix that.
        "has_approver": bool(approvers) or not by_role,
    }


def approval_coverage(tenant, *, branches=None, include_entity_level=True) -> dict:
    """Report, per branch and per stage, who can approve procurement spend.

    ``branches`` restricts the report to a subset and ``include_entity_level`` drops
    the no-branch scope, which together let a branch-bound administrator see their own
    site and nothing else. By default every branch in the tenant is reported plus the
    entity-level scope, which is where documents raised for the whole entity are
    approved. A tenant with no branches reports the entity-level scope alone, so the
    dimension recedes rather than showing an empty column.

    ``document_type`` rows that resolve to no template at all are reported with
    ``configured=False`` - procurement cannot be submitted there, which is a different
    (and louder) problem than having rules with nobody behind them.
    """
    templates = _load_templates(tenant)
    cache = _HolderCache(tenant)

    if branches is None:
        from vs_schools.models import Branch

        # all_objects deliberately: the explicit tenant filter is the boundary, and it
        # must not depend on ambient request-local tenant state.
        branches = list(
            Branch.all_objects.filter(school__tenant=tenant).order_by("code", "pk")
        )
    else:
        branches = list(branches)

    scopes = []
    # None first: entity-level documents are approved by tenant-wide holders, and that
    # scope exists in every tenant, branches or not.
    for branch in ([None, *branches] if include_entity_level else branches):
        document_rows = []
        for document_type in PROCUREMENT_APPROVAL_TYPES:
            template, rules_source = _resolve_template(
                templates, tenant, branch, document_type,
            )
            stages = [] if template is None else [
                _stage_row(stage, branch=branch, cache=cache, rules_source=rules_source)
                for stage in template.active_stages
            ]
            document_rows.append({
                "document_type": document_type,
                "document_type_label": DOCUMENT_TYPE_LABELS[document_type],
                "configured": template is not None,
                "rules_source": rules_source,
                "stages": stages,
            })
        gaps = [
            {
                "document_type": row["document_type"],
                "document_type_label": row["document_type_label"],
                "stage_code": stage["stage_code"],
                "stage_label": stage["stage_label"],
                "role_key": stage["role_key"],
            }
            for row in document_rows for stage in row["stages"]
            if not stage["has_approver"]
        ]
        unconfigured = [row["document_type"] for row in document_rows if not row["configured"]]
        scopes.append({
            "branch_id": getattr(branch, "pk", None),
            "branch_name": getattr(branch, "name", "") or "",
            "is_entity_level": branch is None,
            "documents": document_rows,
            "gaps": gaps,
            "gap_count": len(gaps),
            "unconfigured_document_types": unconfigured,
        })

    return {
        "scopes": scopes,
        "has_gaps": any(scope["gap_count"] for scope in scopes),
        "total_gap_count": sum(scope["gap_count"] for scope in scopes),
    }
