from rest_framework.exceptions import NotFound

from vs_tenants.references import find_branch_in_tenant

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope


# Collapse tenant/branch objects into the persisted configuration scope name.
def scope_name(tenant=None, branch=None):
    if branch is not None:
        return BRANCH_SCOPE
    if tenant is not None:
        # A tenant-level value maps to the definition's "school" allowed-scope
        # label - a school IS a tenant; the label predates the cutover and is
        # kept so ConfigurationDefinition.allowed_scopes shapes never change.
        return SCHOOL_SCOPE
    # Absence of a tenant means the value belongs to the platform default layer.
    return PLATFORM_SCOPE


# Keep branch-scoped writes tied to their owning tenant before keys are built.
def normalize_scope(*, tenant=None, branch=None):
    if branch is not None:
        # The branch owns a tenant directly; nothing travels through the school.
        # Compare ids rather than objects: ``tenant_id`` is a column on the
        # branch row that is already loaded, so the common case (a caller that
        # named both) costs no query at all. Only the case that has to *return*
        # a tenant materialises one.
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
        # The branch must live under the resolved tenant; foreign, missing and
        # malformed references all return the same 404 to avoid tenant
        # enumeration. Scoping the lookup itself (rather than fetching first and
        # comparing afterwards) is what keeps a non-numeric or oversized id a
        # 404 instead of a database error.
        branch = find_branch_in_tenant(tenant, branch_ref)
        if branch is None:
            raise NotFound("Configuration scope not found.")
        # Belonging to the tenant is not the same as being the caller's to write.
        # This checked the first and never the second, so a Configuration Admin
        # pinned to one branch could read and write any other branch of her own
        # school by changing ``?branch=``: the RBAC key answers "may you edit
        # configuration", never "whose". Nothing downstream caught it either -
        # the scope resolved cleanly and the write looked ordinary in the audit
        # trail, with no branch in her grants to explain where it came from.
        #
        # visible_branch_ids answers None for a caller with whole-tenant reach,
        # which is what a platform operator asserting a business tenant has, and
        # what an unpinned school admin has - so both keep working untouched.
        # Only a caller who IS pinned is narrowed, and then to their own set.
        #
        # The refusal is the same 404 an unknown reference gets, deliberately.
        # A distinct 403 would confirm that the branch exists, which is the
        # enumeration the lookup above is already scoped to prevent.
        from vs_rbac.scoping import visible_branch_ids

        entitled = visible_branch_ids(request.user, branch.tenant)
        if entitled is not None and branch.pk not in entitled:
            raise NotFound("Configuration scope not found.")
        # A branch selection implies its tenant even for platform callers.
        scope_tenant = branch.tenant

    return normalize_scope(tenant=scope_tenant, branch=branch)
