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


# --------------------------------------------------------------------------- #
# Turning the answer into a filter                                            #
# --------------------------------------------------------------------------- #
#
# :func:`visible_branch_ids` answers "which branches?" and stops there. Every
# caller then has to render that answer against its own model, and rendering it
# is where the two ways of getting it wrong live:
#
#   * forgetting to render it at all - the gate holds, the narrowing never
#     happens, and a "Bursar at Ikeja" reads Lekki's and Yaba's rows;
#   * rendering it as ``branch_id IN (...)`` and nothing else - which silently
#     drops every row whose branch is NULL.
#
# The second is the dangerous one, because a NULL branch does not mean "this row
# has no branch yet". It means **shared across the whole tenant**: a fee
# structure the school publishes for every branch, a vendor every branch buys
# from, a journal that belongs to the books rather than to a site. Hiding those
# from a branch-pinned caller looks like missing data, not like a permission
# error, so nobody reports it as a security bug and nobody reports it as a bug
# at all. Hence :class:`BranchScope`, whose default is inclusive, and whose
# exclusive form has to be asked for by name.


class BranchScope:
    """One caller's branch narrowing, rendered against any relation path.

    Built once per request by :func:`branch_scope` and then re-rendered as often
    as needed: a list filters one model and wants a single ``Q``, while a report
    service aggregates several models that reach ``branch`` by different routes
    (``payment__branch``, ``grn__branch``) and wants the same answer per path.
    Handing such a service a pre-built ``Q`` forces it to re-derive the rule for
    every other path, which is exactly how two screens of the same module come to
    disagree about what a caller can see.

    ``include_shared`` picks between the two readings of a NULL branch:

    ``True`` (the default, "inclusive")
        A row with no branch is shared across the tenant and stays visible to a
        branch-pinned caller. This is what the column means for ledger entities,
        academic structure, master data and anything a school publishes once for
        every branch, and it is the right default: getting it wrong in this
        direction only ever shows a caller something they were entitled to
        anyway.

    ``False`` ("exclusive")
        Only the caller's own branches. Correct where a NULL branch means "the
        institution as a whole" and is a scope in its own right that a
        site-pinned person is deliberately not in - which is how
        :mod:`vs_procurement` reads it for spend documents, and how ``M11``
        specifies ``Student.branch`` (declared non-null, so the question cannot
        arise there at all).

    A whole-tenant caller is not narrowed in either mode, and :meth:`filter` then
    returns the queryset untouched rather than adding a tautological term, so a
    tenant that has never used a branch-pinned grant keeps byte-identical SQL.
    """

    __slots__ = ("branch_ids", "include_shared")

    def __init__(self, branch_ids: Optional[FrozenSet[int]], *, include_shared: bool = True):
        self.branch_ids = branch_ids
        self.include_shared = include_shared

    @property
    def is_narrowed(self) -> bool:
        """True when this caller sees less than the whole tenant.

        The one thing that should turn a branch column, switcher or facet on in a
        response: where a caller is unbound - or the school has one branch and the
        dimension ought to recede - it stays False and the payload is unchanged.
        """
        return self.branch_ids is not None

    def q(self, prefix: str = "", *, field: str = "branch"):
        """The narrowing as a ``Q``, with ``prefix`` naming the route to ``branch``.

        An unbound caller renders to an empty ``Q()``, which is the identity for
        ``&`` and for ``filter()`` - so callers may AND this in unconditionally.
        """
        from django.db.models import Q

        if self.branch_ids is None:
            return Q()
        own = Q(**{f"{prefix}{field}_id__in": tuple(sorted(self.branch_ids))})
        if not self.include_shared:
            # An empty set renders as ``IN ()``, which matches nothing - the right
            # answer for a caller whose every granted branch has been withdrawn.
            return own
        return own | Q(**{f"{prefix}{field}_id__isnull": True})

    def filter(self, qs, prefix: str = "", *, field: str = "branch"):
        """Narrow *qs* to this caller, or return it untouched when unbound.

        Deliberately not ``qs.filter(self.q(prefix))``: filtering on an empty
        ``Q`` is a no-op semantically but still clones the queryset and can
        perturb a later ``exclude()`` or aggregate, and the whole-tenant caller is
        the common case that must not change at all.
        """
        if self.branch_ids is None:
            return qs
        return qs.filter(self.q(prefix, field=field))


#: A caller nothing narrows. Shared because it is immutable and by far the
#: commonest answer - every whole-tenant caller resolves to this exact object.
UNNARROWED = BranchScope(WHOLE_TENANT)


def caller_branch_ids(request) -> Optional[FrozenSet[int]]:
    """The branches the caller behind *request* may work in, or :data:`WHOLE_TENANT`.

    Branch context is **not** carried by a header or a query parameter. It is
    derived from what the caller has actually been granted, by the one function
    that also decides whether they may open the screen at all
    (:func:`visible_branch_ids`) - so "may I?" and "whose rows?" cannot give
    different answers.

    Resolved against the caller's **own** tenant, because branch grants only
    exist there; reaching another tenant's rows is refused by entity/tenant
    scoping, which is a separate mechanism and is not touched here. DRF's
    ``request.user`` is the *effective* user, so this stays correct through
    impersonation - an impersonating platform admin is narrowed by the grants of
    the person they are standing in for, which is the point of impersonation.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return WHOLE_TENANT
    return visible_branch_ids(user, getattr(user, "tenant", None))


def branch_scope(request, *, include_shared: bool = True) -> BranchScope:
    """Resolve the caller's branch narrowing once, for as many querysets as need it.

    Resolving once per request rather than per queryset is what lets a list, its
    KPI header and the dashboard card above it agree; see :class:`BranchScope`
    for what ``include_shared`` decides.
    """
    ids = caller_branch_ids(request)
    if ids is None and include_shared:
        return UNNARROWED
    return BranchScope(ids, include_shared=include_shared)


def branch_q(request, prefix: str = "", *, field: str = "branch",
             include_shared: bool = True):
    """The caller's branch narrowing as a ``Q``, ready to drop into a ``filter()``.

    For the very common case of a queryset that is already being filtered for
    something else, where threading a wrapper around the call would obscure it::

        qs = Invoice.objects.filter(branch_q(request), entity=entity)

    An unbound caller renders to an empty ``Q()``, which Django compiles to
    *byte-identical* SQL - no extra clause, no extra join - so a whole-tenant
    caller, and a school that has never pinned a grant to a branch, are not
    merely unaffected but indistinguishable from before.
    """
    return branch_scope(request, include_shared=include_shared).q(prefix, field=field)


def branch_visible(request, qs, prefix: str = "", *, field: str = "branch",
                   include_shared: bool = True):
    """Narrow *qs* to the branches the caller behind *request* is entitled to.

    The read half of the branch rule with no request input at all: it answers
    "may this caller see this row", which is what a list, a detail read, an
    action lookup and an aggregate all need. It never widens - it can only remove
    rows the surrounding entity or tenant scoping already allowed - so it is safe
    to apply after any other filter and in any order.
    """
    return branch_scope(request, include_shared=include_shared).filter(
        qs, prefix, field=field,
    )
