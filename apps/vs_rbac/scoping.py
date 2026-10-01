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
    then: exactly the tenant's only branch -> the whole tenant

The last line applies to whichever arm answered. Harbour Primary has one
branch, Main, and its bursar holds their role pinned to Main. Every row Harbour
has is a Main row, a shared record, or a transaction still waiting to be given
its branch, and all of them are Main's, so the pin says nothing a whole-tenant
grant would not: they read the tenant-level figures, changes shared records and
grants roles across the tenant exactly as an unpinned bursar does. The day
Harbour opens a second branch the same grant narrows to Main again, on the next
request, because a shared row then binds a branch they do not work in. A
caller covering every branch of a tenant with two or more stays narrowed: the
next branch opened would inherit whatever they did to a shared row. The grant
row itself is never rewritten; only its reading changes with the branch count.

A whole-tenant grant dominating is not a detail: it is what "whole tenant"
means, and it is how everybody working today holds their access. It dominates
``User.branch`` too. Let the first arm and the last one both answer "no
narrowing" and the caller falls through to their home posting either way, so a
Finance Officer for the whole school sees the one branch their staff record happens
to name, and two people holding the identical grant see different schools
because one of them has a home posting and the other does not. That is a
permission decided by a field which is not a permission. The grant wins;
``User.branch`` is the fallback for somebody whose grants say nothing, which is
all it is for.

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


class _NoGrants:
    """Type of the :data:`_SILENT` sentinel (a singleton, compared by identity)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "SILENT"


#: "The caller's grants say nothing at all, so ask their home posting."
#:
#: Module-private, and deliberately *not* :data:`WHOLE_TENANT`. A set of grants
#: gives three answers, not two:
#:
#: * a whole-tenant grant -> :data:`WHOLE_TENANT`, the whole tenant, final;
#: * branch-pinned grants -> those branches, possibly *none* of them once the
#:   sites are withdrawn, which is an empty frozenset meaning "sees nothing";
#: * no grants at all -> this, the only case ``User.branch`` still decides.
#:
#: The first and third must never share ``None``, or
#: :func:`visible_branch_ids` cannot tell them apart and a whole-tenant grant
#: ends up narrowed to its holder's home posting. This value never leaves the
#: module: the public answer is still ``None`` or a frozenset, and only that is
#: memoised.
_SILENT = _NoGrants()


def _assignment_scope_rows(queryset, *, with_user=False, extra=()):
    """Read effective branch inputs in one query, including role reach.

    ``extra`` names annotations to append to each row, after the branch inputs.
    """
    fields = (
        "branch_id", "branch__status", "role__branch_id",
        "role__branch__status", "role__additional_branches__id",
        "role__additional_branches__status",
    )
    fields = (("user_id",) + fields if with_user else fields) + tuple(extra)
    return queryset.values_list(*fields)


def _only_branch_subquery(tenant):
    """*tenant*'s only branch id as a subquery (NULL when it has several).

    Uncorrelated, so the database evaluates it once however many rows carry it.
    """
    from django.db.models import Case, Count, F, Min, Subquery, When

    from vs_tenants.models import Branch

    return Subquery(
        Branch.all_objects.filter(tenant=tenant).order_by().values("tenant")
        .annotate(_count=Count("pk"), _lowest=Min("pk"))
        .annotate(_only=Case(When(_count=1, then=F("_lowest"))))
        .values("_only")[:1]
    )


def _effective_grant_rows(rows):
    """Expand inherited role reach while keeping silent grants distinguishable."""
    from vs_tenants.models import Branch

    for branch_id, status, role_branch_id, role_status, extra_id, extra_status in rows:
        if role_branch_id is None and extra_id is None:
            yield branch_id, status
            continue
        if branch_id is None:
            if role_branch_id is not None:
                yield role_branch_id, role_status
            if extra_id is not None:
                yield extra_id, extra_status
            continue
        allowed = (
            (branch_id == role_branch_id and role_status in Branch.IN_SERVICE_STATES)
            or (branch_id == extra_id and extra_status in Branch.IN_SERVICE_STATES)
        )
        yield branch_id, status if allowed else ""


def _grant_scope(user, tenant):
    """The narrowing the caller's role grants imply: one of the three answers above.

    :data:`WHOLE_TENANT` for a whole-tenant grant, a frozenset (possibly empty)
    when every grant is branch-pinned, and :data:`_SILENT` when there are no
    grants to speak for the caller at all - access may still come from a personal
    override, so that case is not "sees nothing", it is "not this function's
    answer".

    One query: the branch ids of the caller's active grants, with ``None``
    present in the result iff they hold a whole-tenant one, each row also
    carrying the tenant's only branch, which is memoised for
    :func:`_only_branch_id`.
    """
    if getattr(user, "tenant_id", None) != tenant.pk:
        # Branch grants only exist inside the caller's own tenant, so this
        # function has nothing to say about another one - the same silence as
        # holding no grants, and answered the same way. Cross-tenant access is
        # refused by entity scoping, which this change does not touch.
        return _SILENT

    # Deliberately *not* filtered by branch liveness in SQL. "This person holds
    # no grants" and "every branch this person was granted has since been
    # withdrawn" are different answers - the first falls back to their home
    # posting, the second must show nothing - and a filter that drops the
    # withdrawn rows makes the two indistinguishable. The status comes back with
    # the row instead, so this is still one query.
    # The tenant's only branch rides along on every row, so the one-branch rule
    # costs a caller with grants no query of its own.
    raw = list(_assignment_scope_rows(
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            user=user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            role__status="ACTIVE",
        ).annotate(_tenant_only_branch=_only_branch_subquery(tenant)),
        extra=("_tenant_only_branch",),
    ))
    if raw:
        _remember_only_branch(user, tenant, raw[0][-1])
    rows = set(_effective_grant_rows(row[:-1] for row in raw))
    return _scope_from_rows(rows)


def _scope_from_rows(rows):
    """Read one person's grant rows into a scope.

    Split out so the bulk reader below cannot interpret the same rows a
    different way from the single one. ``rows`` is an iterable of
    ``(branch_id, branch_status)``.
    """
    from vs_tenants.models import Branch

    rows = set(rows)
    if not rows:
        return _SILENT  # No grants: overrides may still admit them.
    if any(branch_id is None for branch_id, _ in rows):
        # A whole-tenant grant means the whole tenant, and it outranks both the
        # branch-pinned grants held beside it and the holder's home posting.
        return WHOLE_TENANT
    # ``IN_SERVICE_STATES`` is the same constant the permission gate filters on
    # in ``evaluator._assignment_branch_q``, so a branch that stops conferring
    # access stops conferring visibility in the same breath.
    return frozenset(
        branch_id for branch_id, status in rows
        if status in Branch.IN_SERVICE_STATES
    )


def _posting_fallback(user, extra_ids=None):
    """The account's equal posting set when no active grant speaks for it."""
    if extra_ids is None:
        extra_ids = user.additional_branches.values_list("pk", flat=True)
    ids = set(extra_ids)
    if user.branch_id is not None:
        ids.add(user.branch_id)
    return frozenset(ids) if ids else WHOLE_TENANT


def only_branch_id(tenant) -> Optional[int]:
    """The id of *tenant*'s one branch, or ``None`` when it has more than one.

    Counted over every branch the tenant owns, whatever its status, which is
    the same count that decides whether the branch dimension shows on a screen
    at all: a suspended or pending second branch still has rows that a
    tenant-wide change would reach. One query, reading at most two ids.

    Not memoised. A caller asking on behalf of one person goes through
    :func:`_only_branch_id`, which is.
    """
    from vs_tenants.models import Branch

    if tenant is None:
        return None
    ids = list(
        Branch.all_objects.filter(tenant=tenant).values_list("pk", flat=True)[:2]
    )
    return ids[0] if len(ids) == 1 else None


def only_branch_id_or_several(tenant) -> Optional[int]:
    """The id of *tenant*'s one branch, or ``None`` when it owns several.

    *tenant* is a tenant or its id. Counted as :func:`only_branch_id` counts,
    over every branch the tenant owns whatever its status. This is the check for
    a caller about to write something that must name a branch: a transaction,
    or a ledger line, bank account or payroll line not yet given one, is that
    branch's at a tenant with one, and at a tenant with several belongs to none
    until somebody gives it one.

    Raises :class:`~vs_tenants.exceptions.BranchlessTenantError` when the tenant
    owns no branch. Every tenant owns at least one, so that is a data fault, and
    ``None`` here always means several branches: nothing is ever written without
    a branch because the tenant had none to give. One query, reading at most
    two ids.
    """
    from vs_tenants.exceptions import BranchlessTenantError
    from vs_tenants.models import Branch

    ids = list(Branch.all_objects.filter(tenant=tenant).values_list("pk", flat=True)[:2])
    if not ids:
        raise BranchlessTenantError(tenant)
    return ids[0] if len(ids) == 1 else None


def _only_branch_id(user, tenant) -> Optional[int]:
    """:func:`only_branch_id`, memoised on the user instance, keyed by tenant.

    The same life as :func:`visible_branch_ids`' own memo: ``request.user`` is
    rebuilt per request, so a second branch opened between two requests is
    seen by the next one, while a list judging many rows asks the database
    once.
    """
    cache = getattr(user, "_rbac_only_branch", None)
    if cache is not None and tenant.pk in cache:
        return cache[tenant.pk]
    return _remember_only_branch(user, tenant, only_branch_id(tenant))


def _remember_only_branch(user, tenant, only):
    """Memoise *only* as *tenant*'s only branch on *user*, and return it."""
    cache = getattr(user, "_rbac_only_branch", None)
    if cache is None:
        cache = {}
        try:
            user._rbac_only_branch = cache
        except AttributeError:  # pragma: no cover - defensive, mirrors visible_branch_ids
            return only
    cache[tenant.pk] = only
    return only


def _home_posting(user, tenant):
    """The home-posting fallback and *tenant*'s only branch, in one query.

    Returns ``(scope, only_branch_id)``. The caller's equal postings and the
    tenant's branches come back as one list of branch rows, so a caller whose
    grants say nothing learns both their posting set and whether it is the
    tenant's only branch for the price of the postings query alone. That
    keeps a posted reader's per-request cost where it was before the
    one-branch rule existed.
    """
    from django.db.models import Exists, OuterRef, Q

    from vs_tenants.models import Branch
    from vs_user.models import User

    postings = User.additional_branches.through.objects.filter(user_id=user.pk)
    rows = (
        Branch.all_objects
        .filter(Q(tenant=tenant) | Q(pk__in=postings.values("branch_id")))
        .annotate(_posted=Exists(postings.filter(branch_id=OuterRef("pk"))))
        .values_list("pk", "tenant_id", "_posted")
    )
    extra_ids, own = [], []
    for branch_id, tenant_id, posted in rows:
        if posted:
            extra_ids.append(branch_id)
        if tenant_id == tenant.pk:
            own.append(branch_id)
    return _posting_fallback(user, extra_ids), (own[0] if len(own) == 1 else None)


def _is_only_branch(scope, only_id) -> bool:
    """Whether *scope* is exactly the tenant's only branch; see the module docstring."""
    return (
        scope is not WHOLE_TENANT
        and only_id is not None
        and len(scope) == 1
        and next(iter(scope)) == only_id
    )


def visible_branch_ids_for(users, tenant):
    """:func:`visible_branch_ids` for many people, in one query.

    Returns ``{user_id: WHOLE_TENANT | frozenset}``.

    The single version memoises on the user INSTANCE, which is exactly right
    for ``request.user`` and useless for a loop over other people: each row
    carries its own user object, so the cache never hits and a screen asking
    the question about every member of staff ran a query per member of staff.
    A branch roster is the one that found it.

    The rule is not restated here. The rows are read by ``_scope_from_rows``,
    the same function the single version uses, and the no-grants fallback to
    the holder's home posting is the same fallback for the same reason. The
    tenant's only branch is looked up once for the whole list, and only when
    somebody's answer is a single branch.
    """
    users = list(users)
    if not users or tenant is None:
        return {}

    by_user = {}
    rows = _assignment_scope_rows(TenantUserRoleAssignment.objects.filter(
        tenant=tenant,
        user__in=users,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        role__status="ACTIVE",
    ), with_user=True)
    for user_id, *grant in rows:
        by_user.setdefault(user_id, []).append(tuple(grant))

    silent_ids = [user.pk for user in users if not by_user.get(user.pk)]
    from vs_user.models import User
    extras = {}
    if silent_ids:
        through = User.additional_branches.through
        for user_id, branch_id in through.objects.filter(
            user_id__in=silent_ids,
        ).values_list("user_id", "branch_id"):
            extras.setdefault(user_id, set()).add(branch_id)

    answer = {}
    for user in users:
        # Branch grants only exist inside the holder's own tenant, so somebody
        # from another one is silent here - the same answer the single version
        # gives, reached the same way.
        if getattr(user, "tenant_id", None) != tenant.pk:
            scope = _SILENT
        else:
            scope = _scope_from_rows(_effective_grant_rows(by_user.get(user.pk, ())))
        if scope is _SILENT:
            scope = _posting_fallback(user, extras.get(user.pk, ()))
        answer[user.pk] = scope

    home = {user.pk for user in users if getattr(user, "tenant_id", None) == tenant.pk}
    single = [
        user_id for user_id, scope in answer.items()
        if user_id in home and scope is not WHOLE_TENANT and len(scope) == 1
    ]
    if single:
        only = only_branch_id(tenant)
        for user_id in single:
            if _is_only_branch(answer[user_id], only):
                answer[user_id] = WHOLE_TENANT
    return answer


def visible_branch_ids(user, tenant=None) -> Optional[FrozenSet[int]]:
    """The branch ids *user* may work in, or :data:`WHOLE_TENANT` for no narrowing.

    Memoised on the user instance for the life of the request, keyed by tenant:
    this is on the hot path of every list, every detail read and every aggregate,
    and ``request.user`` is rebuilt per request, so the cache can never go stale
    across one. Cost is one query per request per tenant, and one more for a
    caller whose grants say nothing, to read their home postings; none at all
    after the first call. Whichever query answers also brings the tenant's
    only branch with it, so the one-branch rule (see the module docstring)
    adds no query of its own.

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
    if scope is _SILENT:
        # No grants at all to speak for this caller, so their home posting still
        # decides - exactly as it did before branch grants worked, and the only
        # arm that reads the column. A whole-tenant grant does not come through
        # here: it answered WHOLE_TENANT above and that is the final answer.
        # ``branch_id`` rather than ``branch``: the id is already on the row, and
        # dereferencing the relation would fetch the whole Branch on the hot path
        # of every read just to read its primary key back.
        scope, only = _home_posting(user, tenant)
        _remember_only_branch(user, tenant, only)
    if (
        scope is not WHOLE_TENANT and len(scope) == 1
        and getattr(user, "tenant_id", None) == tenant.pk
        and _is_only_branch(scope, _only_branch_id(user, tenant))
    ):
        # Only ever for the tenant the caller belongs to: their only branch
        # makes them whole-tenant at home, never in a tenant being asked about.
        scope = WHOLE_TENANT

    if cache is None:
        cache = {}
        try:
            user._rbac_visible_branches = cache
        except AttributeError:  # pragma: no cover - defensive, mirrors evaluator
            return scope
    cache[tenant.pk] = scope
    return scope


def branch_reach_payload(user, tenant=None) -> dict:
    """The caller's reach in the shape the session payload carries.

    ``whole_tenant`` is true when :func:`visible_branch_ids` answers the whole
    tenant, which includes a caller pinned to a tenant's only branch. It is the
    one answer every read narrows by and every shared-record write asks, so a
    client that reads it to decide whether a shared screen is read-only agrees
    with the server that refuses the write. ``branch_ids`` is then empty and
    means nothing. Otherwise ``branch_ids`` is exactly the set of branches the
    caller may work in, and may be empty, which means they see no branch rows
    at all.

    The sign-in response and ``/me`` both carry it so a client can tell "this
    school has two branches" apart from "this person may work in two
    branches". The branch list endpoint answers the first; only this answers
    the second, and a branch picker built from the first offers a choice the
    server would then refuse to honour.
    """
    scope = visible_branch_ids(user, tenant)
    if scope is WHOLE_TENANT:
        return {"whole_tenant": True, "branch_ids": []}
    return {"whole_tenant": False, "branch_ids": sorted(scope)}


# --------------------------------------------------------------------------- #
# Turning the answer into a filter                                            #
# --------------------------------------------------------------------------- #
#
# :func:`visible_branch_ids` answers "which branches?" and stops there. Every
# caller then has to render that answer against its own model, and a NULL branch
# means one of two different things depending on what the row is:
#
#   * **Shared records and configuration** - a customer, a vendor, a fee
#     structure, the chart of accounts, a catalogue item, a cost centre, a role
#     grant, a workflow template, a notification setting. A NULL branch means
#     *every branch*: the school publishes the row once and each branch uses it.
#     Hiding it from a branch-pinned caller looks like missing data rather than a
#     permission error, so this reading is inclusive, and it is the default of
#     :class:`BranchScope`, :func:`branch_q` and :func:`branch_visible`.
#   * **Transactions** - every document (invoice, receipt, credit note, refund,
#     journal, payroll run, requisition, order, vendor bill, vendor payment) and
#     the containers that hold a branch's money or stock (a bank account, a
#     petty-cash fund, a store). There is no school-wide transaction: every
#     school has at least one real branch and every transaction names one. A
#     NULL branch on a transaction is a row that has not been given its branch
#     yet, not a scope of its own, so a branch-pinned caller never sees it and a
#     whole-school caller does, and can give it one. This reading is exclusive,
#     and it is spelled once, in :func:`transaction_branch_scope` and its
#     siblings, so no call site chooses it for itself.
#
# Getting a transaction wrong in the inclusive direction is a leak: Mrs Adeyemi
# works at Ikeja only, and an unbranched refund of 250,000 raised before the
# school opened Lekki would sit in their list with no way to tell whose it is.
# Hence the exclusive transaction helpers, which every transaction read uses.


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
        branch-pinned caller. This is what the column means for master data,
        academic structure and configuration: anything a school publishes once
        for every branch.

    ``False`` ("exclusive")
        Only the caller's own branches. The reading for every transaction, where
        a NULL branch is a row not yet given its branch rather than a scope. A
        transaction read does not pass this itself; it asks
        :func:`transaction_branch_scope`, which does.

    A whole-tenant caller, including one pinned to a tenant's only branch, is
    not narrowed in either mode, and :meth:`filter` then
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


def transaction_branch_scope_for_user(user, *, tenant=None) -> BranchScope:
    """The exclusive narrowing every transaction read uses, for code holding a user.

    A branch-bound caller sees exactly their own branches' transactions and none
    without a branch; a whole-tenant caller, including one pinned to a tenant's
    only branch, is not narrowed and so still reaches an unbranched row to give it
    its branch. See the section comment above for why a transaction's NULL branch
    is not shared.
    """
    return branch_scope_for_user(user, include_shared=False, tenant=tenant)


def transaction_branch_scope(request) -> BranchScope:
    """:func:`transaction_branch_scope_for_user` for the caller behind *request*."""
    return transaction_branch_scope_for_user(getattr(request, "user", None))


def transaction_branch_q(request, prefix: str = "", *, field: str = "branch"):
    """The caller's transaction narrowing as a ``Q`` (see :func:`transaction_branch_scope`).

    Renders to an empty ``Q()`` for a whole-tenant caller, like :func:`branch_q`.
    """
    return transaction_branch_scope(request).q(prefix, field=field)


def transaction_branch_q_for_user(user, prefix: str = "", *, field: str = "branch"):
    """:func:`transaction_branch_q` for code that holds a user rather than a request."""
    return transaction_branch_scope_for_user(user).q(prefix, field=field)


def transaction_branch_visible(request, qs, prefix: str = "", *, field: str = "branch"):
    """Narrow a queryset of transactions to the caller behind *request*."""
    return transaction_branch_scope(request).filter(qs, prefix, field=field)


# --------------------------------------------------------------------------- #
# Reading a shared record is inclusive; changing it is not                    #
# --------------------------------------------------------------------------- #
#
# A branch-bound caller reads their own branches' rows AND the shared ones,
# because a school-wide subject, class or registrar belongs to their branch too.
# They may not change a shared row: every other branch relies on it, and a
# correction made from Ikeja reaches Lekki's screens with nobody at Lekki
# knowing. The rule below is that asymmetry, in one place, for every module.
# Transactions are not shared rows and never reach this question: a branch-bound
# caller cannot read one without a branch in the first place.

#: "The caller's scope has not been looked up yet", distinct from
#: :data:`WHOLE_TENANT`, which is itself ``None``.
_UNRESOLVED = object()


def caller_reaches_whole_tenant(user, tenant=None, *, visible=_UNRESOLVED) -> bool:
    """Whether *user*'s reach is the whole of *tenant*, for a write to a shared row.

    Exactly :func:`visible_branch_ids` answering the whole tenant, so the
    screens a caller reads and the shared rows they may change cannot give two
    answers. That includes a caller pinned to a tenant's only branch (see the
    module docstring). A caller covering every branch of a tenant with two or
    more is still branch-bound: the next branch opened would inherit their
    change.

    Pass ``visible`` when it is already resolved, so it is not looked up twice.
    """
    if visible is _UNRESOLVED:
        visible = visible_branch_ids(user, tenant or getattr(user, "tenant", None))
    return visible is WHOLE_TENANT


def caller_may_change(user, tenant, branch_ids, *, visible=_UNRESOLVED) -> bool:
    """Whether *user* may change a row belonging to *branch_ids*, not merely read it.

    ``branch_ids`` is the row's whole branch set: one id for a row with a single
    branch, several for a row posted or linked to several, and empty for a row
    shared across the tenant. A whole-tenant caller, which includes one pinned
    to a tenant's only branch, may change any row they can see. A branch-bound
    caller may change a row only when its set is non-empty and every branch in
    it is one of theirs.

    Pass ``visible`` when judging many rows, so the caller's scope is resolved
    once rather than per row.
    """
    if visible is _UNRESOLVED:
        visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return True
    ids = {getattr(branch, "pk", branch) for branch in branch_ids if branch is not None}
    return bool(ids) and ids <= visible


def assert_caller_may_change(user, tenant, branch_ids, *, message: str = "") -> None:
    """Refuse a write to a row the caller may read but not change (403).

    An empty *branch_ids* is a row shared across the tenant, which only a
    whole-tenant caller may change.
    """
    from .exceptions import SharedRecordReadOnly

    if not caller_may_change(user, tenant, branch_ids):
        raise SharedRecordReadOnly(message)


def assert_caller_may_configure(user, tenant, branch=None, *, message: str = "") -> None:
    """Refuse a settings write the caller's branch reach does not cover (403).

    A setting is a row like any other, and :func:`caller_may_change` already
    says who may change it; this is that rule spelled for settings, so every
    settings screen asks it the same way. With no ``branch`` the setting is the
    tenant's own, it binds every branch, and only a caller whose reach is the
    whole tenant may change it: a branch administrator whose role carries the
    settings key still only reads it, because raising a minimum for Ikeja
    raises it for Lekki too. A caller pinned to a tenant's only branch reaches
    the whole tenant (:func:`caller_reaches_whole_tenant`) and may change it.
    With a ``branch`` the setting is that branch's own override, and a caller
    who covers that branch may set or remove it.

    The permission key is a separate question, answered before this by
    :class:`~vs_rbac.permissions.HasRBACPermission`. A view that accepts a
    named branch still resolves it inside the tenant and refuses one the caller
    cannot see with its usual 404 first, so this 403 is only ever the answer
    for a scope the caller can read.
    """
    assert_caller_may_change(
        user, tenant, () if branch is None else (branch,), message=message,
    )


def shared_write_refusal(subject: str) -> str:
    """The sentence a refused write to a shared record carries, naming *subject*."""
    return f"Only a school-wide administrator can change {subject}."


class WholeTenantWriteMixin:
    """Refuse every write on a view whose rows are shared across the whole tenant.

    For records that carry no branch and bind every branch at once: a fiscal
    period, a tax code, a dunning ladder, a catalogue item. Holding the write
    key is not enough to change one, because Lekki's bursar closing January
    closes it for Ikeja too. The caller's reach has to be the whole tenant
    (:func:`caller_reaches_whole_tenant`), and a refusal is a 403
    ``SHARED_RECORD_READ_ONLY`` raised before the handler runs, so nothing is
    written.

    The check sits in :meth:`check_permissions`, after the permission classes,
    so a caller without the key still gets the ordinary permission refusal, and
    every unsafe method of the view is covered without its handler having to
    remember. Reads are untouched.

    ``shared_subject`` completes the refusal sentence
    (:func:`shared_write_refusal`). A view mixing shared and branch rows does
    not use this: it judges the row with :func:`assert_caller_may_change`.
    """

    shared_subject = "these records"

    def check_permissions(self, request):
        from rest_framework.permissions import SAFE_METHODS

        super().check_permissions(request)
        if request.method not in SAFE_METHODS:
            assert_caller_may_configure(
                request.user, getattr(request, "tenant", None),
                message=shared_write_refusal(self.shared_subject),
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
#     in (:func:`raised_transaction_branch` for a transaction,
#     :func:`raised_branch` for a shared record);
#   * it **continues** a chain, and takes the branch from the row it continues and
#     from nothing else (:func:`inherited_branch_id`).
#
# One implementation of each rule serves every module, so finance and
# procurement cannot come to disagree about which branch a row belongs to; each
# app keeps its local names as one-line adapters over these.
#
# The two halves differ on an absent branch. A shared record may be raised with
# none, which means every branch. A transaction may not: it is always raised for
# a real branch, and a chain carries exactly one branch from end to end.


def sole_caller_branch_id(request) -> Optional[int]:
    """The one branch a caller works in, or ``None`` when that is not exactly one.

    Used only where a *default* is needed (creating a row without naming a
    branch). It is never used to decide what a caller may reach: answering
    ``None`` for a caller entitled to two branches would read as "unbound", and
    unbound means the whole tenant.

    A caller in a tenant with one branch works in that branch whatever their
    grants say, so it is the answer for them even though their reach is the
    whole tenant. :func:`raised_branch` does not default from this for a
    whole-tenant caller, because a shared record raised without a branch belongs
    to every branch; :func:`raised_transaction_branch` does, because a
    transaction always has one.
    """
    ids = caller_branch_ids(request)
    if ids is None:
        user = getattr(request, "user", None)
        tenant = getattr(user, "tenant", None)
        if tenant is None or not getattr(user, "is_authenticated", False):
            return None
        return _only_branch_id(user, tenant)
    if len(ids) != 1:
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
    """The branch a newly created shared record belongs to, from the caller and the body.

    For a record where no branch is a first-class answer meaning every branch: a
    customer, a vendor, a fee structure. A transaction asks
    :func:`raised_transaction_branch` instead, which never answers ``None``.

    A caller bound to one branch always creates for that branch; naming a
    different one is refused rather than silently retargeted. A caller who is not
    bound at all may name any branch belonging to *tenant*, or leave it out,
    which files the record for every branch. A caller pinned to a tenant's only
    branch is not bound (see the module docstring), so at Harbour Primary the
    pinned bursar and the unpinned one file the same record the same way.

    ``shared_when_ambiguous`` decides the one case in between: a caller bound to
    **several** branches who names none.

    ``False`` (the default)
        Ask them with a 400. Naming a branch outside their own set is refused
        exactly as a single-branch caller's would be.

    ``True``
        File it for every branch. Correct where forcing a choice would make a
        genuinely shared record, such as a fee template a school publishes once,
        invisible to every branch but one.
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


def raised_transaction_branch(request, tenant, body, *, field: str = "branch"):
    """The branch a newly raised transaction belongs to, at every tenant.

    A transaction always names a real branch. Who decides it:

    * a caller bound to one branch raises for that branch, and naming another is
      refused (403), as :func:`raised_branch` refuses it;
    * a caller bound to several must name one of theirs (400 when they name none);
    * a whole-tenant caller may name any branch of *tenant*. Naming none is
      answered by the tenant's only branch when it has exactly one, so Harbour
      Primary's bursar is never asked which branch they mean, and is a 400 when
      it has several: Mr Bello, the school-wide bursar at a school with Ikeja
      and Lekki, raising a refund without saying whose, has raised it for one of
      them, and filing it under neither would hide it from both branches' staff.

    Never ``None``. Every tenant owns at least one branch, the platform tenant
    included, so a whole-tenant caller naming none at a tenant that owns no
    branch raises :class:`~vs_tenants.exceptions.BranchlessTenantError`
    (:func:`only_branch_id_or_several`): a data fault answered with a server
    error, never a transaction raised without a branch.
    """
    from rest_framework.exceptions import ValidationError

    raw = body.get(field) if hasattr(body, "get") else None
    if caller_branch_ids(request) is WHOLE_TENANT and raw in (None, ""):
        only = only_branch_id_or_several(tenant)
        if only is None:
            raise ValidationError(
                {field: "Name the branch this is for; the school has more than one."},
            )
        return resolve_branch(tenant, only, field)
    return raised_branch(request, tenant, body, field=field)


def same_transaction_branch(tenant, *branch_ids) -> bool:
    """Whether transactions carrying *branch_ids* (ids or Branch rows) belong to one branch.

    Equal ids always do. A transaction not yet given a branch belongs, at a tenant
    with exactly one branch, to that branch, because it can belong to no other:
    Harbour Primary's receipt raised for Main settles its invoice raised before
    invoices carried a branch, and is paid out of its bank account from the same
    time. At a tenant with several, an unbranched transaction matches only another
    unbranched one, so nothing of Ikeja's is settled against, or paid from, a row
    whose branch nobody has decided. *tenant* may be a tenant or its id, and is
    read only when an unbranched id meets a branched one.
    """
    ids = {getattr(b, "pk", b) for b in branch_ids}
    if len(ids) <= 1:
        return True
    if None not in ids:
        return False
    only = only_branch_id(tenant)
    return len({only if b is None else b for b in ids}) == 1


def transaction_branch_match_q(tenant, branch_id, prefix: str = "", *, field: str = "branch"):
    """A ``Q`` for the transactions :func:`same_transaction_branch` pairs with *branch_id*."""
    from django.db.models import Q

    branch_id = getattr(branch_id, "pk", branch_id)
    unbranched = Q(**{f"{prefix}{field}__isnull": True})
    only = only_branch_id(tenant)
    if branch_id is None:
        return unbranched if only is None else unbranched | Q(**{f"{prefix}{field}_id": only})
    own = Q(**{f"{prefix}{field}_id": branch_id})
    return own | unbranched if only == branch_id else own


def inherited_branch_id(request, *sources, field: str = "branch") -> Optional[int]:
    """The branch id a downstream transaction takes from the source(s) it continues.

    The chain decides, not the request: once a source row exists its branch is the
    answer, and no request body, header or query parameter may override it. A
    chain carries one branch, so sources from two branches are refused with a
    400 rather than resolved to either: a payment settling one Ikeja bill and one
    Lekki bill would be booked to one branch while clearing the other's debt.

    The only check left is that the caller is entitled to work in the resulting
    branch: a branch-bound caller may not continue another branch's chain, nor a
    source that has not yet been given a branch, which they cannot read either
    (:func:`transaction_branch_scope`). A whole-tenant caller continuing an
    unbranched source gets ``None``, so the new row joins the one it continues
    and both are given their branch together.

    Sources are compared by :func:`same_transaction_branch`, so at a tenant with
    one branch a source raised before transactions carried a branch continues
    into that branch.

    A source is a row whose branch the new row must take. A shared record with no
    branch (a customer every branch bills) has none to give, so a document raised
    against it alone takes its branch from :func:`raised_transaction_branch`.
    """
    from rest_framework.exceptions import PermissionDenied, ValidationError

    known = {getattr(s, f"{field}_id") for s in sources if s is not None}
    if not same_transaction_branch(getattr(request.user, "tenant_id", None), *known):
        raise ValidationError({field: (
            "These documents belong to different branches. "
            "Handle each branch's documents separately."
        )})
    branch_id = next((b for b in known if b is not None), None)
    ids = caller_branch_ids(request)
    if ids is None:
        return branch_id
    if branch_id not in ids:
        raise PermissionDenied("This document belongs to another branch.")
    return branch_id
