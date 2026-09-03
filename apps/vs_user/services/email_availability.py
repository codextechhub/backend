"""The one answer to "may this address become an account here?", shared by every
creation and rename path that asks it.

Why this exists at all
``uq_user_email_per_tenant`` makes an address unique per tenant, not across the
platform, so an unscoped ``filter(email=...)`` is a defect wherever it appears:
it answers about the WHOLE platform when it is only entitled to ask about one
tenant.

The damage is not symmetrical, which is why both halves matter:

* An unscoped ``.exists()`` REFUSES something legal. Ada Okoye has a child at
  Bright Star and enrols a second at Greenfield with the same address;
  Greenfield's admin is told the address is taken and cannot make her an
  account, while learning that she holds one somewhere else.
* An unscoped ``.first()`` ACCEPTS something wrong, silently. CodeX creates
  Greenfield with ada.okoye@example.test as its primary administrator, the
  provisioner finds her Bright Star row, decides the admin "already exists",
  and hands Greenfield's admin link to another school's account. Nothing
  raises and nothing is logged as an error.

So the rule is one function, not a pattern to be re-typed per call site: pass
the tenant that WOULD own the account, get back the refusal message or an
empty string.

The transitional second rule
While ``sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN`` is False a sign-in that
names no tenant still falls back to a platform-wide email lookup, so a second
tenant's copy of an address could not be told apart at the door.
``User._guard_cross_tenant_email`` therefore refuses to CREATE that pair
while the switch is off, and lifts the refusal when it is on. The switch is
now on, so the refusal has lifted; this module follows it either way.

This module mirrors that rule rather than restating it, so a pre-check and the
model can never disagree, and so flipping the switch narrows every pre-check
in the same instant it narrows the model. Get it wrong in the other direction
and Phase 4 achieves nothing: the switch goes on, sign-in is safe, and
Greenfield is still refused Ada's second account by a stale global check in a
serializer.
"""
from __future__ import annotations

from ..email_normalization import normalize_email
from ..models import CROSS_TENANT_EMAIL_REFUSAL, User
from . import sign_in_scope

#: The refusal for an address already held INSIDE the target tenant. Naming it
#: here keeps the wording identical across the serializers, the importer and
#: the management commands, all of which used to spell it out themselves.
SAME_TENANT_REFUSAL = 'A user with this email already exists.'


def _tenant_pk(tenant):
    """Fold a Tenant, a pk, or None to the pk to compare rows against.

    ``None`` is a legitimate, meaningful argument here and not a caller
    oversight: a school-creation request is validated BEFORE its tenant row
    exists, so there is genuinely no tenant yet for an address to be taken in.
    """
    if tenant is None:
        return None
    return getattr(tenant, 'pk', tenant)


def email_refusal(email, *, tenant, exclude_pk=None) -> str:
    """Why *email* may not become an account at *tenant*, or ``''`` if it may.

    ``tenant``
        The tenant that would own the account. ``None`` when the tenant does
        not exist yet (school creation), which makes the same-tenant rule
        vacuous by construction: a tenant with no rows can hold no address.
    ``exclude_pk``
        The account being renamed, so its own current address does not read as
        a clash with itself.

    One query. The rows it walks are one per tenant holding the address, which
    is at most the tenant count and in practice zero or one.
    """
    email = normalize_email(email)
    if not email:
        return ''

    tenant_pk = _tenant_pk(tenant)
    owners = User.objects.filter(email=email)
    if exclude_pk is not None:
        owners = owners.exclude(pk=exclude_pk)

    cross_tenant_refused = not sign_in_scope.tenant_is_required()
    refusal = ''
    for owner_pk in owners.values_list('tenant_id', flat=True):
        if tenant_pk is not None and owner_pk == tenant_pk:
            # The strongest answer available, and the only one the database
            # itself will also refuse. Return immediately.
            return SAME_TENANT_REFUSAL
        if cross_tenant_refused:
            refusal = CROSS_TENANT_EMAIL_REFUSAL
    return refusal


def email_refusals(emails, *, tenant) -> dict[str, str]:
    """The same answer for several addresses at once, in one query.

    Keyed by the NORMALISED address, so a caller that tagged its inputs must
    normalise them the same way (they all go through ``normalize_email``)
    before looking a result up. Addresses that may be used are absent from the
    mapping rather than present with an empty value.
    """
    normalized = {normalize_email(value) for value in emails}
    normalized.discard('')
    if not normalized:
        return {}

    tenant_pk = _tenant_pk(tenant)
    cross_tenant_refused = not sign_in_scope.tenant_is_required()

    refusals: dict[str, str] = {}
    for stored, owner_pk in User.objects.filter(
        email__in=normalized,
    ).values_list('email', 'tenant_id'):
        if tenant_pk is not None and owner_pk == tenant_pk:
            refusals[stored] = SAME_TENANT_REFUSAL
        elif cross_tenant_refused:
            refusals.setdefault(stored, CROSS_TENANT_EMAIL_REFUSAL)
    return refusals
