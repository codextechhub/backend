from rest_framework.exceptions import NotFound

from vs_schools.models import Branch

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope


# Collapse tenant/branch objects into the persisted configuration scope name.
def scope_name(tenant=None, branch=None):
    if branch is not None:
        return BRANCH_SCOPE
    if tenant is not None:
        # A tenant-level value maps to the definition's "school" allowed-scope
        # label — a school IS a tenant; the label predates the cutover and is
        # kept so ConfigurationDefinition.allowed_scopes shapes never change.
        return SCHOOL_SCOPE
    # Absence of a tenant means the value belongs to the platform default layer.
    return PLATFORM_SCOPE


# Keep branch-scoped writes tied to their owning tenant before keys are built.
def normalize_scope(*, tenant=None, branch=None):
    if branch is not None:
        # branch -> school -> tenant is the only cross-tenant traversal retained.
        branch_tenant = branch.school.tenant
        if tenant is None:
            tenant = branch_tenant
        elif branch_tenant.pk != tenant.pk:
            raise InvalidConfigurationScope("Branch must belong to the selected tenant.")
    return tenant, branch


# Resolve the caller's authorized tenant/branch scope from request.tenant.
def resolve_request_scope(request, *, allow_platform=True):
    """Derive the write/read scope from the request's asserted tenant.

    ``request.tenant`` is the single source of truth (set by
    TenantJWTAuthentication from the mandatory ``?tenant=`` assertion, which the
    auth layer already validates against the caller's own tenant — platform
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
        branch = Branch.all_objects.filter(pk=branch_ref).first()
        # The branch must live under the resolved tenant; foreign/missing
        # branches return the same 404 to avoid tenant enumeration.
        target_tenant_id = tenant.pk if tenant is not None else None
        if branch is None or branch.school.tenant_id != target_tenant_id:
            raise NotFound("Configuration scope not found.")
        # A branch selection implies its tenant even for platform callers.
        scope_tenant = branch.school.tenant

    return normalize_scope(tenant=scope_tenant, branch=branch)
