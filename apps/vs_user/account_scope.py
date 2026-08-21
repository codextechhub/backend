"""Which accounts one caller may act **on**.

``vs_rbac`` answers "may this person suspend staff?". Nothing answered *whose*
staff, and the two are different questions. ``User.objects`` is a plain manager
- ``UserManager``, not a tenant-aware one - and ``User.pk`` is a sequential
integer, so every endpoint that resolved a target with
``User.objects.get(id=user_id)`` was reachable with any id on the platform.

Concretely: Amaka administers Bright Star and holds ``platform.team.suspend``,
which is how she suspends her own leavers. Tunde teaches at Greenfield, a
different customer of this platform, and his account happens to be id 41.
``POST /v1/user/41/suspend/`` used to return 200 and log him out of his school.
Her key was never the thing that was wrong - she is supposed to hold it - and a
403 would have been the wrong refusal anyway, because it confirms that 41 is
somebody. The lookup was the thing that was wrong.

Eight surfaces had it, so the answer lives here rather than eight times over:

* ``UserEmailChangeView``, ``UserSuspendView``, ``UserReactivateView``,
  ``UserUnlockView`` (``views/accounts.py``);
* ``AdminPasswordResetView`` (``views/passwords.py``);
* ``InvitationResendView`` (``views/auth.py``);
* ``ForceLogoutSerializer`` and ``UnlockAccountSerializer``
  (``serializers.py``), whose ``PrimaryKeyRelatedField`` declared
  ``queryset=User.objects.all()``.

``UserAccountViewSet.get_queryset`` builds on the same function, so list,
retrieve, update, destroy and submit are behind this gate rather than beside
it, and there is one answer to "whose account is this?" on the whole app.

The rule
--------

Read in order::

    caller on a PLATFORM tenant  -> every account, on every tenant
    else                         -> the asserted tenant, narrowed to the
                                    caller's branches, plus always their own row

The first arm is the point of the CX console and must stay: a Codex operator
legitimately unlocks a school's principal. It is keyed on the caller's **own**
tenant kind, the same discriminator ``1da5c2a`` and ``a4916e9`` used, and not
on a permission key - the key is already held by exactly the people this is
meant to admit and by the people it is meant to refuse.

The second arm is ``654e7af``'s branch narrowing, which already governed
retrieve/update/destroy, so suspending somebody now answers the same way
deactivating them always did.

The third clause is the one deliberate widening. Self-service is the common
case, and a caller whose only role grant is pinned to a branch they are not
posted at would otherwise be unable to change their own email address. An
account is always within reach of the person it belongs to; every action here
still has to pass its own RBAC key first.
"""
from __future__ import annotations

from django.db.models import Q

from vs_rbac.scoping import branch_q
from vs_tenants.models import Tenant

from .models import User


def administrable_users(request, queryset=None):
    """Every account the caller behind *request* may act on.

    Pass *queryset* to narrow something already built (the viewset hands in its
    own ``select_related``/``prefetch_related`` chain); omit it for the plain
    table.

    A platform-kind caller is returned untouched, so the console keeps
    byte-identical SQL and its cross-tenant reach.
    """
    qs = User.objects.all() if queryset is None else queryset

    user = getattr(request, "user", None)
    if getattr(getattr(user, "tenant", None), "kind", None) == Tenant.Kind.PLATFORM:
        return qs

    qs = qs.filter(tenant=getattr(request, "tenant", None) or getattr(user, "tenant", None))

    # ``branch_q`` renders an *empty* ``Q()`` for a caller nothing narrows, and
    # ``Q() | Q(pk=...)`` collapses to ``Q(pk=...)`` in Django - which would cut
    # an unnarrowed admin down to a single row. Ask whether there is a narrowing
    # at all before combining, which also keeps the unnarrowed caller's SQL
    # unchanged, as :mod:`vs_rbac.scoping` intends.
    # ``include_shared=True`` spelled out rather than left to the default: a null
    # branch means "across the whole tenant" on this very model, and a4916e9 made
    # that the normal shape for a school user - so the shared arm is the common
    # case here, not the edge. Getting it backwards would empty the staff list and
    # every picker built on it, and would make a colleague with no posting
    # un-suspendable.
    narrowing = branch_q(request, include_shared=True)
    if not narrowing:
        return qs
    own = getattr(user, "pk", None)
    return qs.filter(narrowing if own is None else narrowing | Q(pk=own))


def administrable_user(request, user_id, queryset=None):
    """Resolve *user_id* inside the caller's scope, or ``None``.

    ``None`` covers all three ways an id can fail - no such account, an account
    at another tenant, an account at a branch the caller does not cover - and
    callers must report them identically. Anything else hands back the
    enumeration this module exists to close: a 403 on "another tenant" and a 404
    on "nobody" together walk the whole platform one integer at a time.

    A non-numeric or oversized reference is one of those three, not a server
    error. The routes used to declare ``<str:user_id>`` and pass it straight to
    the ORM, so ``/v1/user/abc/suspend/`` raised ``ValueError`` and answered 500.
    """
    raw = str(user_id).strip()
    if not raw.isdigit() or int(raw) > 9_223_372_036_854_775_807:
        return None
    return administrable_users(request, queryset).filter(pk=int(raw)).first()
