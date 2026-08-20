# services/sign_in_scope.py
# Resolving which tenant a sign-in (or a self-service password reset) is
# addressed to, BEFORE the account is looked up by email.
#
# Why this exists at all
# ----------------------
# Both LoginService.login and PasswordService.request_reset used to find the
# account with an unscoped ``User.objects.filter(email__iexact=...).first()``
# and take the tenant from whatever row came back. That is only correct while
# ``User.email`` is unique across the whole platform. The moment uniqueness is
# narrowed to one address per tenant, the same query returns two rows and
# ``.first()`` picks one: a parent whose two children attend two different
# customers of this platform, and who reuses her password, is signed in to the
# wrong one - and a reset she asks for at one rewrites her password at the
# other. Neither failure raises anything.
#
# So the caller may now assert the tenant. It is a ``Tenant.slug``, and the
# frontend reads it off the subdomain it is served from: the page at
# ``bright-star.xvs.codexng.com`` sends ``bright-star``. The lookup is then
# scoped to it. That check is real today (an account that belongs to another
# tenant is refused) and becomes load-bearing when uniqueness narrows.

from __future__ import annotations

from vs_tenants.models import Tenant

from ..models import User


# ── The switch ────────────────────────────────────────────────────────────────
#
# While this is False the tenant is OPTIONAL: a request that omits it behaves
# exactly as it did before (global email lookup, tenant derived from the row),
# and a request that supplies it is scoped and checked.
#
# It is now True. Both frontends send the tenant: school-fe reads it off the
# subdomain it is served from (bright-star.xvs.codexng.com sends "bright-star")
# and console-fe sends the constant "codex", the platform tenant's slug. A
# sign-in or reset that names no tenant is refused outright rather than
# resolved by an ambiguous email lookup.
#
# TWO THINGS MOVED WITH IT, and neither needed editing, because both read the
# switch through tenant_is_required() rather than copying its value:
#
#   * the legacy unscoped branch in resolve_sign_in_account below is now
#     unreachable - every tenantless request is refused before it; and
#   * ``User._guard_cross_tenant_email`` stands down, so one address may now be
#     an account at more than one tenant. That is the point of the change: the
#     guard existed only to hold the data to what an unscoped lookup could
#     safely answer for.
#
# DEPLOY ORDER MATTERS. A backend carrying this flip must not reach production
# before both frontends do, or every login on the platform fails with correct
# credentials.
REQUIRE_TENANT_ON_SIGN_IN = True

# A primary key no tenant row can hold. When the asserted slug matches no
# authenticable tenant we still run the same scoped user query against this
# sentinel rather than skipping the query. Skipping it would answer "no such
# tenant" measurably faster than "no such user in that tenant", which is a
# timing oracle over the tenant list; running it keeps the two paths identical
# in shape, cost and outcome.
_NO_SUCH_TENANT_ID = 0

# Audit failure codes. They are deliberately distinct from INVALID_CREDENTIALS
# so support can tell "signed in at the wrong address" from "typed the wrong
# password", and deliberately say nothing about where the account really lives:
# see resolve_sign_in_account for what is - and is not - recorded.
FAILURE_TENANT_REQUIRED = 'TENANT_REQUIRED'
FAILURE_TENANT_MISMATCH = 'TENANT_MISMATCH'


def tenant_is_required() -> bool:
    """Whether a sign-in must name the tenant it is addressed to.

    Reads the module global at call time on purpose: that keeps the switch a
    single assignment above, and lets a test flip it without reimporting the
    services that depend on it.
    """
    return REQUIRE_TENANT_ON_SIGN_IN


def normalize_tenant(tenant: str | None) -> str:
    """Fold an asserted tenant identifier to the form ``Tenant.slug`` stores."""
    return (tenant or '').strip().lower()


def resolve_sign_in_account(*, email: str, tenant: str | None):
    """Find the account a sign-in or reset is for, scoped to the asserted tenant.

    ``tenant`` is a slug, not an object: it arrives from an unauthenticated
    request body, so it is an assertion to be checked, never a fact.

    Returns ``(user, tenant, failure_code)``:

    ``user``
        The matching account, or None when there is nothing to authenticate.
    ``tenant``
        The tenant the request ASSERTED, resolved to a row - never one derived
        from an account the caller has not proved they own. It is None when no
        tenant was named, and None when the named slug matched no authenticable
        tenant.
    ``failure_code``
        Empty when the lookup ran to a conclusion the caller is entitled to (a
        user, or genuinely no user in scope). Otherwise the audit code for a
        refusal that the caller must not be able to distinguish from a wrong
        password.

    The scoped branch never reads a row outside the asserted tenant. That is
    what stops the refusal from leaking: nothing in this function can learn -
    let alone record - that the address exists elsewhere on the platform, so
    the response, the timing and the audit row are the same whether the caller
    holds an account at another customer or has never heard of the place.
    """
    slug = normalize_tenant(tenant)

    if not slug:
        if tenant_is_required():
            return None, None, FAILURE_TENANT_REQUIRED
        # The legacy path, and the ONLY unscoped email lookup left on User
        # that is deliberately cross-tenant. It stays, and it stays unchanged.
        #
        # It cannot be scoped: there is no tenant to scope it to. The caller
        # named none, which is exactly the case this branch exists to serve
        # while the two frontends still send none. Making it refuse instead
        # would not be a smaller change than flipping the switch above - it
        # would BE flipping the switch, and it would lock every existing user
        # out on deploy.
        #
        # And ``.first()`` is not ambiguous here, which is the whole reason
        # Phase 3 shipped its guard: while REQUIRE_TENANT_ON_SIGN_IN is False,
        # ``User._guard_cross_tenant_email`` refuses to CREATE a second
        # tenant's copy of an address, so at most one row on the platform can
        # match and ``.first()`` has nothing to choose between. The two halves
        # are the same switch read from opposite ends - the guard holds the
        # data to what this lookup can safely answer for, and both stand down
        # together on the flip.
        #
        # When the switch does flip this branch becomes unreachable: the
        # ``tenant_is_required()`` return above takes every tenantless request.
        # So it is not a thing to fix later either; it is a thing that retires.
        user = User.objects.filter(email__iexact=email).first()
        return user, (user.tenant if user else None), ''

    # ACTIVE or PENDING, matching vs_rbac.authentication: a customer that has
    # not gone live still has to sign its first administrator in to finish
    # onboarding (FR-012). SUSPENDED and INACTIVE are refused exactly as an
    # unknown slug is, and by the same code path.
    resolved = Tenant.objects.filter(
        slug=slug, status__in=Tenant.AUTHENTICABLE_STATUSES,
    ).first()

    user = User.objects.filter(
        email__iexact=email,
        tenant_id=resolved.pk if resolved is not None else _NO_SUCH_TENANT_ID,
    ).first()

    if user is None:
        return None, resolved, FAILURE_TENANT_MISMATCH
    return user, resolved, ''
