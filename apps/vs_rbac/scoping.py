"""Which branches one caller is entitled to work in.

Two questions hang off a branch-scoped role grant, and they must have one
answer between them:

* **access** - "may this person open this screen at all?", asked by
  :class:`vs_rbac.permissions.HasRBACPermission` through
  :func:`vs_rbac.evaluator.has_permission`;
* **visibility** - "whose rows do they then see?", asked by every list, detail,
  aggregate and report.

Before this module they came from unrelated places: access from role grants
(which ignored the branch column entirely), visibility from the single
``vs_user.User.branch`` field. Two mechanisms doing related jobs are free to
disagree, and they did - a grant of "Bursar at Ikeja" conferred nothing, while
a whole-tenant grant plus a ``User.branch`` conferred everything and showed one
site. :func:`visible_branch_ids` is the one answer both now rest on.

The rule
--------

Read in order, first match wins::

    any active whole-tenant grant   -> the whole tenant
    else any active branch grant    -> exactly those branches, while in service
    else                            -> fall back to ``User.branch``

A whole-tenant grant dominating is not a detail: it is what "whole tenant"
means, and it is how everybody working today holds their access. Only the
middle arm is new, and the only people it can reach are those whose grants are
*all* branch-pinned - who, before this, could not open the screen at all. Nobody
who works today is narrowed by it.

The branch arm may legitimately resolve to *nothing* (every granted branch has
since been suspended or closed). That is an empty set, not a missing answer, and
it must never fall through to the ``User.branch`` arm: withdrawing a site is
supposed to withdraw the access it carried, not silently widen it.

``User.branch`` therefore keeps its job as the caller's home posting and default
narrowing - it is still read by account validation, JWT claims, invitation and
configuration lookups - but it is no longer the authority on scope. It cannot
express "Ikeja and Lekki but not Yaba"; a set of grants can, which is why the
answer is a set.
"""
from __future__ import annotations

from typing import FrozenSet, Optional

from .models import TenantUserRoleAssignment

#: What a caller sees when nothing narrows them: the whole tenant. Spelled as
#: ``None`` rather than "every branch id" so a tenant with no branches at all
#: filters on nothing whatsoever and keeps byte-identical responses.
WHOLE_TENANT = None


def _grant_scope(user, tenant) -> Optional[FrozenSet[int]]:
    """The narrowing the caller's role grants imply, or ``None`` for no narrowing.

    Returns ``None`` when the grants say nothing about branches (a whole-tenant
    grant, or no grants at all - access may still come from a personal
    override). Returns a frozenset, possibly empty, when every grant is
    branch-pinned.

    One query: the branch ids of the caller's active grants, with ``None``
    present in the result iff they hold a whole-tenant one.
    """
    if getattr(user, "tenant_id", None) != tenant.pk:
        # Branch grants only exist inside the caller's own tenant, so this
        # function has nothing to say about another one. Cross-tenant access is
        # refused by entity scoping, which this change does not touch.
        return None

    from vs_tenants.models import Branch

    # Deliberately *not* filtered by branch liveness in SQL. "This person holds
    # no grants" and "every branch this person was granted has since been
    # withdrawn" are different answers - the first falls back to their home
    # posting, the second must show nothing - and a filter that drops the
    # withdrawn rows makes the two indistinguishable. The status comes back with
    # the row instead, so this is still one query.
    rows = set(
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            user=user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            role__status="ACTIVE",
        ).values_list("branch_id", "branch__status")
    )
    if not rows:
        return WHOLE_TENANT  # No grants: overrides may still admit them.
    if any(branch_id is None for branch_id, _ in rows):
        return WHOLE_TENANT  # A whole-tenant grant means the whole tenant.
    # ``IN_SERVICE_STATES`` is the same constant the permission gate filters on
    # in ``evaluator._assignment_branch_q``, so a branch that stops conferring
    # access stops conferring visibility in the same breath.
    return frozenset(
        branch_id for branch_id, status in rows
        if status in Branch.IN_SERVICE_STATES
    )


def visible_branch_ids(user, tenant=None) -> Optional[FrozenSet[int]]:
    """The branch ids *user* may work in, or :data:`WHOLE_TENANT` for no narrowing.

    Memoised on the user instance for the life of the request, keyed by tenant:
    this is on the hot path of every list, every detail read and every aggregate,
    and ``request.user`` is rebuilt per request, so the cache can never go stale
    across one. Cost is one query per request per tenant, and none at all for a
    caller whose grants are all whole-tenant after the first call.

    An empty frozenset is a real answer meaning "sees nothing", and is
    deliberately distinguishable from :data:`WHOLE_TENANT`.
    """
    tenant = tenant or getattr(user, "tenant", None)
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return WHOLE_TENANT

    cache = getattr(user, "_rbac_visible_branches", None)
    if cache is not None and tenant.pk in cache:
        return cache[tenant.pk]

    scope = _grant_scope(user, tenant)
    if scope is None:
        # No branch-pinned grants to speak for this caller, so their home
        # posting still decides - exactly as it did before branch grants worked.
        # ``branch_id`` rather than ``branch``: the id is already on the row, and
        # dereferencing the relation would fetch the whole Branch on the hot path
        # of every read just to read its primary key back.
        own_id = getattr(user, "branch_id", None)
        scope = WHOLE_TENANT if own_id is None else frozenset({own_id})

    if cache is None:
        cache = {}
        try:
            user._rbac_visible_branches = cache
        except AttributeError:  # pragma: no cover - defensive, mirrors evaluator
            return scope
    cache[tenant.pk] = scope
    return scope
