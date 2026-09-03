from rest_framework.exceptions import NotFound

from vs_tenants.references import find_branch_in_tenant

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope


# Collapse tenant/branch objects into the persisted configuration scope name.
def scope_name(tenant=None, branch=None):
    if branch is not None:
        return BRANCH_SCOPE
    if tenant is not None:
        # A tenant-level value maps to the "school" allowed-scope label.
        return SCHOOL_SCOPE
    # Absence of a tenant means the value belongs to the platform default layer.
    return PLATFORM_SCOPE


# Keep branch-scoped writes tied to their owning tenant before keys are built.
def normalize_scope(*, tenant=None, branch=None):
    if branch is not None:
        # Compare ids, not objects: `tenant_id` is already on the loaded branch
        # row, so a caller that named both costs no query at all.
        if tenant is None:
            tenant = branch.tenant
        elif branch.tenant_id != tenant.pk:
            raise InvalidConfigurationScope("Branch must belong to the selected tenant.")
    return tenant, branch


# Resolve the caller's authorized tenant/branch scope from request.tenant.
def resolve_request_scope(request, *, allow_platform=True):
    """Derive the write/read scope from the request's asserted tenant.

    ``request.tenant`` is the single source of truth (set by
    TenantJWTAuthentication from the mandatory ``?tenant=`` assertion, which the
    auth layer already validates against the caller's own tenant - platform
    staff may assert a business tenant only on views that opt in). There is no
    ``?school=`` override: a caller cannot read or write another tenant's rows
    by changing a query parameter.

    ``?branch=`` is held to two separate rules, because belonging to the tenant
    is not the same as being the caller's to write. The branch must live under
    the resolved tenant, and it must be one the caller is entitled to. Checking
    only the first would let a Configuration Admin pinned to Ikeja read and
    write Lekki by changing the parameter: the RBAC key answers "may you edit
    configuration", never "whose". Nothing downstream would catch it, because
    the scope resolves cleanly and the write looks ordinary in the audit trail.

    ``visible_branch_ids`` answers ``None`` for a caller with whole-tenant
    reach, which is what a platform operator asserting a business tenant has
    and what an unpinned school admin has, so neither is narrowed. Only a
    pinned caller is, and then to their own set.

    Every refusal is the same 404, whether the branch is unknown, foreign,
    malformed or simply not the caller's. A distinct 403 would confirm the
    branch exists, which is the enumeration the scoped lookup already prevents.
    Scoping the lookup itself, rather than fetching and comparing afterwards,
    is also what keeps a non-numeric or oversized id a 404 rather than a
    database error.
    """
    # Fall back to the user's home tenant for entry points that authenticate
    # without the assertion (e.g. force_authenticate in tests).
    tenant = getattr(request, "tenant", None) or getattr(request.user, "tenant", None)
    is_platform = getattr(tenant, "kind", None) == "PLATFORM"

    # Platform-tenant requests act on the platform layer; a business tenant
    # request acts on that tenant.
    scope_tenant = None if is_platform else tenant
    if scope_tenant is None and not allow_platform:
        # Some write paths require a tenant layer and must not fall back to platform.
        raise InvalidConfigurationScope("A tenant scope is required.")

    branch = None
    branch_ref = request.query_params.get("branch") or request.data.get("branch")
    if branch_ref:
        # Rule one: the branch belongs to the resolved tenant.
        branch = find_branch_in_tenant(tenant, branch_ref)
        if branch is None:
            raise NotFound("Configuration scope not found.")
        # Rule two: it is the caller's to write. See the docstring.
        from vs_rbac.scoping import visible_branch_ids

        entitled = visible_branch_ids(request.user, branch.tenant)
        if entitled is not None and branch.pk not in entitled:
            raise NotFound("Configuration scope not found.")
        # A branch selection implies its tenant even for platform callers.
        scope_tenant = branch.tenant

    return normalize_scope(tenant=scope_tenant, branch=branch)
