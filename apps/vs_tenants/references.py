"""Resolving user-supplied branch references safely, in one place.

``Branch`` is keyed by an ordinary auto-incrementing integer, so a reference
arriving from a request body, a query parameter or an import row is only ever a
decimal id. Every caller that resolves one has to answer the same three
questions, and each has its own way of going wrong:

* is the value a usable integer? Declaring the input as a UUID or a slug means
  the lookup can never match, so the reference is silently unusable;
* does the row exist? Handing the raw string straight to the ORM turns a
  non-numeric or oversized value into a database error, a 500 where a 400
  belongs;
* does it belong to the tenant the caller is entitled to? Checking tenancy
  after the row is fetched lets the caller tell "someone else's branch" apart
  from "no such branch", which is an id oracle.

:func:`find_branch_in_tenant` answers all three at once, and
:func:`resolve_branch_reference` puts the standard validation error on top.
Foreign and unknown branches are deliberately indistinguishable.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError

# The largest value a 64-bit signed column can hold. Anything above it is a bad
# request, not a lookup: PostgreSQL raises rather than returning no rows.
_MAX_BIGINT = 9_223_372_036_854_775_807

BRANCH_NOT_FOUND = "No such branch in this tenant."

TENANT_NOT_FOUND = "No such tenant."


def find_tenant(ref):
    """Return the :class:`~vs_tenants.models.Tenant` ``ref`` names, or ``None``.

    Accepts either the numeric primary key or the slug, because the two
    audiences want different things: ``Tenant.slug`` is the stable
    human-readable identifier (and the sign-in subdomain), while the numeric pk
    is what an operator reads off a row they are already looking at. Blank
    references, unparseable ones and unknown ones all answer ``None``.

    The operator-facing management commands depend on this, because an email
    address alone does not identify an account: the same address can be a login
    at several tenants, so the tenant has to be named alongside it.
    """
    if ref in (None, ""):
        return None

    from vs_tenants.models import Tenant

    raw = str(ref).strip()
    if not raw:
        return None
    if raw.isdigit():
        if int(raw) > _MAX_BIGINT:
            return None
        return Tenant.objects.filter(pk=int(raw)).first()
    return Tenant.objects.filter(slug=raw.lower()).first()


def find_branch_in_tenant(tenant, ref):
    """Return the :class:`~vs_tenants.models.Branch` ``ref`` names in ``tenant``.

    Returns ``None`` for a blank reference, a reference that is not a plausible
    integer id, a branch that does not exist, and a branch owned by another
    tenant. Collapsing those cases is intentional: the caller cannot use the
    parameter to discover which ids exist outside its own tenant.

    The branch carries its tenant directly, so the boundary is a single column
    on the row and the explicit filter is the whole check.
    """
    if tenant is None or ref in (None, ""):
        return None

    from vs_tenants.models import Branch

    raw = str(ref).strip()
    if not raw.isdigit() or int(raw) > _MAX_BIGINT:
        return None

    # all_objects deliberately: the filter is the boundary, not ambient state.
    return Branch.all_objects.filter(tenant=tenant, pk=int(raw)).first()


def resolve_branch_reference(tenant, ref, field="branch"):
    """Resolve ``ref`` inside ``tenant``, or ``None`` when it is blank.

    A blank reference is a valid answer meaning "no branch", not missing data.
    Anything else that fails to resolve raises a validation error keyed on
    ``field`` with a message that reveals nothing about other tenants.
    """
    if ref in (None, ""):
        return None
    branch = find_branch_in_tenant(tenant, ref)
    if branch is None:
        raise ValidationError({field: BRANCH_NOT_FOUND})
    return branch
