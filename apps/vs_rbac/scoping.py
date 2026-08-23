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


def branch_scope_for_user(user, *, include_shared: bool = True, tenant=None) -> BranchScope:
    """The same narrowing as :func:`branch_scope`, for code that holds no request.

    Some visibility rules are written against a *user* rather than a request -
    :mod:`vs_tickets` decides who may see a thread from the participant list and
    a permission check, and never looks at the request at all. Those still need
    the identical answer, so they get it from here rather than by faking a
    request object or, worse, re-reading ``User.branch`` and quietly disagreeing
    with every request-driven screen.

    :func:`branch_scope` delegates to this, so there is one implementation.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        ids = WHOLE_TENANT
    else:
        ids = visible_branch_ids(user, tenant or getattr(user, "tenant", None))
    if ids is None and include_shared:
        return UNNARROWED
    return BranchScope(ids, include_shared=include_shared)


def branch_scope(request, *, include_shared: bool = True) -> BranchScope:
    """Resolve the caller's branch narrowing once, for as many querysets as need it.

    Resolving once per request rather than per queryset is what lets a list, its
    KPI header and the dashboard card above it agree; see :class:`BranchScope`
    for what ``include_shared`` decides.
    """
    return branch_scope_for_user(
        getattr(request, "user", None), include_shared=include_shared,
    )


def branch_q_for_user(user, prefix: str = "", *, field: str = "branch",
                      include_shared: bool = True):
    """:func:`branch_q` for code that holds a user rather than a request."""
    return branch_scope_for_user(user, include_shared=include_shared).q(
        prefix, field=field,
    )


def caller_may_use_branch(request, branch) -> bool:
    """Whether the caller behind *request* is entitled to work in *branch*.

    The predicate behind an explicitly named branch - a ``?branch=`` parameter or
    a body field - as opposed to :func:`branch_q`, which narrows rows the caller
    never named. Tenant membership is a *separate* check and is not done here:
    callers must already have resolved the branch inside the right tenant
    (:func:`resolve_branch` or
    :func:`vs_tenants.references.find_branch_in_tenant`), because "belongs to
    another tenant" and "belongs to a branch I do not cover" are different
    failures even though a careful endpoint reports them identically.

    ``None`` - the tenant-wide scope - is answered ``True`` only for an unbound
    caller. A branch-pinned caller naming no branch is not asking for a scope
    they hold; deciding what happens to them is :func:`raised_branch`'s job, and
    it needs the two cases apart.
    """
    ids = caller_branch_ids(request)
    if ids is None:
        return True
    return getattr(branch, "pk", branch) in ids


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


# --------------------------------------------------------------------------- #
# The write half: what branch goes *on* a row                                 #
# --------------------------------------------------------------------------- #
#
# Everything above answers "whose rows?". None of it does anything until
# something puts a branch on a row in the first place, and there are exactly two
# ways a row can get one:
#
#   * it **starts** a chain, and captures the branch the person creating it works
#     in (:func:`raised_branch`);
#   * it **continues** a chain, and takes the branch from the row it continues and
#     from nothing else (:func:`inherited_branch_id`).
#
# Both were procurement's, written per document type and then generalised there;
# they now live here because finance needs the identical rules and a second copy
# of "which branch does this belong to" is how two modules come to disagree about
# the same school. Procurement keeps its local names as one-line adapters over
# these, so there is one implementation of each rule on the platform.
#
# An absent branch remains a real, valid answer everywhere below - the row belongs
# to the tenant as a whole - and is never coerced or rejected.


def sole_caller_branch_id(request) -> Optional[int]:
    """The one branch a caller works in, or ``None`` when that is not exactly one.

    Used only where a *default* is needed (creating a row without naming a
    branch). It is never used to decide what a caller may reach: answering
    ``None`` for a caller entitled to two branches would read as "unbound", and
    unbound means the whole tenant.
    """
    ids = caller_branch_ids(request)
    if ids is None or len(ids) != 1:
        return None
    return next(iter(ids))


def sole_caller_branch(request, tenant):
    """:func:`sole_caller_branch_id` resolved to a :class:`~vs_tenants.models.Branch`.

    Resolved through the same tenant-checked lookup a request-supplied reference
    goes through, so a grant naming a branch outside *tenant* (which entity
    resolution already makes unreachable) answers ``None`` rather than writing a
    foreign tenant's branch onto a row.
    """
    branch_id = sole_caller_branch_id(request)
    if branch_id is None:
        return None
    return resolve_branch(tenant, branch_id)


def resolve_branch(tenant, ref, field: str = "branch"):
    """Resolve a branch reference inside *tenant*, or ``None`` when blank.

    A branch belonging to another tenant is reported exactly like an unknown one,
    so the parameter cannot be used to discover ids outside the caller's tenant.
    The rule itself lives with the model it protects
    (:mod:`vs_tenants.references`); this is only the name the scoping helpers
    reach it by, so every app that accepts a branch answers an unknown reference
    the same way.
    """
    from vs_tenants.references import resolve_branch_reference

    return resolve_branch_reference(tenant, ref, field)


def raised_branch(request, tenant, body, *, field: str = "branch",
                  shared_when_ambiguous: bool = False):
    """The branch a newly created row belongs to, from the caller and the body.

    A caller bound to one branch always creates for that branch; naming a
    different one is refused rather than silently retargeted. A caller who is not
    bound at all may name any branch belonging to *tenant*, or leave it out -
    leaving it out means the row belongs to the tenant as a whole and is a valid
    answer, not missing data.

    ``shared_when_ambiguous`` decides the one case in between: a caller bound to
    **several** branches who names none.

    ``False`` (the default)
        Ask them. There is no obvious default, and guessing one is worse than a
        400: a bursar covering Ikeja and Lekki who raises an invoice has raised
        it for one of them, and filing it as tenant-wide would leave it visible
        to every branch for the life of the row, with nothing later in the chain
        able to narrow it again. Naming a branch outside their own set is refused
        exactly as a single-branch caller's would be.

    ``True``
        File it as shared across the tenant. Correct only where tenant-wide is a
        first-class answer for that kind of row rather than an accident - a fee
        template a school publishes once for every branch, the bank account the
        whole school pays into - and where forcing a choice would make a
        genuinely shared thing invisible to every branch but one.

    The distinction is about the *row*, not the caller, so it is a property of the
    call site and is spelled out there.
    """
    from rest_framework.exceptions import PermissionDenied, ValidationError

    ids = caller_branch_ids(request)
    raw = body.get(field) if hasattr(body, "get") else None
    if ids is None:
        return resolve_branch(tenant, raw, field)
    if getattr(request.user, "tenant_id", None) != getattr(tenant, "pk", None):
        # A caller's grants live in their own tenant, so the caller's tenant is an
        # exact, query-free proxy for the tenant their branches belong to.
        # Unreachable through the API (entity resolution already pins the caller's
        # tenant), but fail closed rather than write a foreign tenant's branch.
        raise PermissionDenied("Your branch does not belong to this entity.")
    if not ids:
        # Every branch they were granted has since been suspended or closed.
        raise PermissionDenied("You are not assigned to a branch that can raise this.")
    if raw in (None, ""):
        own = sole_caller_branch(request, tenant)
        if own is None:
            if shared_when_ambiguous:
                return None
            raise ValidationError(
                {field: "Name the branch this is for; you work in more than one."},
            )
        return own
    chosen = resolve_branch(tenant, raw, field)
    if chosen is None or chosen.pk not in ids:
        raise PermissionDenied("You can only raise documents for your own branch.")
    return chosen


def inherited_branch_id(request, *sources, field: str = "branch",
                        include_shared: bool = False) -> Optional[int]:
    """The branch id a downstream row takes from the source(s) it continues.

    The chain decides, not the request: once a source row exists its branch is the
    answer, and no request body, header or query parameter may override it.
    Sources that disagree (a payment settling invoices from two branches) resolve
    to the tenant as a whole. The only check left is that the caller is entitled to
    work in the resulting scope at all - a branch-bound user may not continue
    another branch's chain.

    ``include_shared`` is the same fork :class:`BranchScope` draws, and it must be
    the same answer in both halves or a caller can see a row they may not build on:

    ``False`` (the default)
        A source with no branch belongs to the institution as a whole, which is a
        scope of its own that a branch-pinned caller is not in, so they may not
        continue it either. This is :mod:`vs_procurement`'s reading of spend.

    ``True``
        A source with no branch is shared across the tenant, so a branch-pinned
        caller may continue it - and the row they create stays tenant-wide, because
        the chain, not the caller, decides. This is the platform reading, and the
        one :mod:`vs_finance` takes: an Ikeja bursar can see a school-wide customer
        in her list, so she must be able to record that customer's receipt.
    """
    from rest_framework.exceptions import PermissionDenied

    known = {getattr(s, f"{field}_id") for s in sources if s is not None}
    branch_id = known.pop() if len(known) == 1 else None
    ids = caller_branch_ids(request)
    if ids is None:
        return branch_id
    if branch_id is None and include_shared:
        return None
    if branch_id not in ids:
        raise PermissionDenied("This document belongs to another branch.")
    return branch_id
