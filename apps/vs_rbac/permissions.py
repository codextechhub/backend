from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS
from .evaluator import (
    ANY_BRANCH,
    _group_permission_keys,
    get_effective_permissions,
    has_permission,
    has_all_permissions,
)


# Read the DRF request user through one helper so permission classes stay consistent.
def _get_user(obj, request):
    return getattr(request, "user", None)


# Check the platform super-admin assignment used for privileged RBAC bypasses.
def is_vision_super_admin(user):
    """Return True if *user* currently holds an active xvs_super_admin role.

    Memoised on the user instance - user objects are re-fetched on every
    request, so this saves one EXISTS query per permission check within a
    request without ever serving stale data across requests.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    cached = getattr(user, "_is_xvs_super_admin", None)
    if cached is not None:
        return cached  # Reuse the request-local assignment check.
    from .models import TenantUserRoleAssignment
    result = TenantUserRoleAssignment.objects.filter(
        user=user,
        tenant=getattr(user, "tenant", None),
        role__key="xvs_super_admin",
        role__tenant=getattr(user, "tenant", None),
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).exists()
    try:
        user._is_xvs_super_admin = result
    except AttributeError:
        pass
    return result


# Check a raw permission key against active school or platform role assignments.
def user_has_rbac_permission(
    user, permission_key, tenant=None, branch=ANY_BRANCH, school=None,
):
    """
    Check whether *user* holds *permission_key* through any active role.

    For school-scoped users the check is limited to roles in *school*.
    For Vision staff the check runs against platform roles.

    Returns True if any active assignment grants the permission.

    ``branch`` defaults to :data:`~vs_rbac.evaluator.ANY_BRANCH`: unless a caller
    names a scope, every grant the user holds counts, branch-pinned ones
    included. It used to default to ``None``, which asks the narrower question
    "may they do this for the entity as a whole?" - and since no caller ever
    passed anything else, a branch-pinned grant answered no everywhere.
    """
    if not user or not user.is_authenticated:
        return False

    # ``school`` is a convenience for callers that hold a School rather than a
    # Tenant; the evaluator itself is tenant-only.
    if tenant is None and school is not None:
        tenant = getattr(school, "tenant", None)

    return has_permission(
        user, permission_key, tenant=tenant, branch=branch,
    )


# Decide whether the view being called is open to a tenant that is not yet live.
def _view_opens_to_pending_tenant(view, request) -> bool:
    """Read the view's ``pending_tenant_surface`` declaration.

    The attribute is the whole of the allowlist, and its absence means closed:
    a surface opened by default would silently admit every endpoint added
    afterwards, which is the failure this gate exists to prevent.

    Accepted forms:
      ``True``                 the whole view is open;
      an iterable of names     open only for those ViewSet actions or HTTP
                               methods, compared case-insensitively. This is
                               how a router-backed view opens one verb without
                               opening its siblings (POST /support/tickets/
                               without the ticket list, for instance).
    """
    declared = getattr(view, "pending_tenant_surface", False)
    if declared is True:
        return True
    if not declared:
        return False
    if isinstance(declared, str):
        declared = [declared]
    names = {str(name).lower() for name in declared}
    action = getattr(view, "action", None)
    if action and str(action).lower() in names:
        return True
    return (getattr(request, "method", "") or "").lower() in names


# Close every surface but onboarding to a tenant that has not gone live (FR-012).
class TenantSurfaceAllowed(BasePermission):
    """Refuse a PENDING tenant anything outside the onboarding surface.

    Authentication admits a tenant whose status is ACTIVE or PENDING, because
    the first School Admin has to sign in before the school is live. Deciding
    *what* they may then reach is a question about the view being called, not
    about the caller's identity, so it is answered here rather than at the
    door. A view opts in with ``pending_tenant_surface``; anything that does
    not declare it is closed.

    This is not a permission key, deliberately: a role grant must never be able
    to reopen the platform to a school that has not gone live.

    Installed everywhere a view can pick up its permissions, because DRF's
    ``permission_classes`` replaces ``DEFAULT_PERMISSION_CLASSES`` rather than
    adding to it, so the defaults alone would leave almost the whole repo
    ungated: in ``DEFAULT_PERMISSION_CLASSES`` (a view that declares nothing),
    in :class:`IsAuthenticatedAndActive` (which nearly every authenticated view
    composes), and in :class:`HasRBACPermission` / :class:`HasAnyModuleAccess`
    (for views that pair those with a bare ``IsAuthenticated``).

    Either returns True or raises ``TenantNotLive``; it never returns False, so
    the refusal carries its own error code rather than DRF's generic one.
    """

    def has_permission(self, request, view):
        from vs_tenants.exceptions import TenantNotLive
        from vs_tenants.models import Tenant

        u = _get_user(None, request)
        if not u or not getattr(u, "is_authenticated", False):
            # Unauthenticated callers hold no tenant; IsAuthenticated (or
            # AllowAny) owns that decision, not this class.
            return True

        # ``request.tenant`` is the tenant being operated on: the caller's own
        # for an ordinary call, and the target tenant when a platform actor
        # rides an impersonation session, which is exactly the scope that must
        # be governed in both cases.
        tenant = getattr(request, "tenant", None) or getattr(u, "tenant", None)
        if getattr(tenant, "status", None) != Tenant.Status.PENDING:
            return True

        if _view_opens_to_pending_tenant(view, request):
            return True
        raise TenantNotLive()


# Reserve a decision to the platform tenant, whoever happens to hold its key.
class PlatformDecisionAllowed(BasePermission):
    """Refuse everyone outside the platform tenant a view marked ``platform_decision``.

    Some decisions are *about* a tenant but are not *the tenant's to make*:
    approving a school's own go-live is the clearest case. Until this class
    existed the boundary was a naming convention - the key is "granted to
    platform roles by default" - which is not a boundary at all. A tenant admin
    who can create a role can put any key their tenant is allowed to hold into
    it, and every key in the ``onboarding`` module is ``PermissionScope.TENANT``,
    so the scope guard on ``Permission`` never runs for them.

    **The tenant tested is the caller's own, not ``request.tenant``.** A view
    that opts into ``platform_cross_tenant_param`` is reached by a platform
    actor asserting the *target's* slug, so ``request.tenant`` is the school on
    exactly the calls that must succeed. The question here is who is asking,
    which is a fact about the caller's home tenant.

    Reading ``request.user`` rather than ``request.actor_user`` is deliberate:
    under impersonation the effective user is the person being impersonated, and
    an actor wearing a school admin's identity holds that admin's authority and
    no more. A decision reserved to the platform is not one they may launder
    through a school account.

    It returns False rather than raising, so the refusal is DRF's ordinary 403
    and reads identically to the one a caller without the key receives. A
    distinct message here would be a probe: mint the role, call the endpoint,
    and read from the wording whether the grant landed.
    """

    def has_permission(self, request, view):
        from vs_tenants.models import Tenant

        if not getattr(view, "platform_decision", False):
            return True

        u = _get_user(None, request)
        if not u or not getattr(u, "is_authenticated", False):
            return False
        return (
            getattr(getattr(u, "tenant", None), "kind", None)
            == Tenant.Kind.PLATFORM
        )


# Enforce login plus non-terminal account status before RBAC is evaluated.
class IsAuthenticatedAndActive(BasePermission):
    """
    Minimal guardrail:
    - user must be authenticated
    - if your UserAccount has 'status', block locked/suspended
    - the tenant being operated on must be live, unless the view declares
      itself part of the pending-tenant surface (see TenantSurfaceAllowed)
    """

    def has_permission(self, request, view):
        u = _get_user(None, request)
        if not u or not u.is_authenticated:
            return False

        status = getattr(u, "status", None)
        if status == "SUSPENDED":
            raise PermissionDenied("Your account is suspended. Contact your administrator.")
        if status == "LOCKED":
            raise PermissionDenied("Your account is locked due to too many failed login attempts. Contact your administrator.")
        if status == "DEACTIVATED":
            raise PermissionDenied("This account has been deactivated. Contact your administrator.")

        # Nearly every authenticated view in the repo composes this class, and
        # DRF's permission_classes replaces DEFAULT_PERMISSION_CLASSES rather
        # than adding to it, so the surface gate has to live here too.
        return TenantSurfaceAllowed().has_permission(request, view)


# Allow only Vision staff into platform-owned RBAC administration surfaces.
class IsVisionStaff(BasePermission):
    """
    Vision staff can manage global permission registry + approve/deny requests.
    Assumes your user model has user_type and includes VISION_STAFF (Module 3).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(getattr(u, "tenant", None), "kind", None) == "PLATFORM"


# Allow only the active xvs_super_admin role holder into top-level controls.
class IsVisionSuperAdmin(BasePermission):
    """
    Grants access only to the active Vision Super Admin -
    the single user with an active xvs_super_admin TenantUserRoleAssignment.
    """

    def has_permission(self, request, view):
        return is_vision_super_admin(request.user)


# Evaluate the permission keys declared by a DRF view.
class HasRBACPermission(BasePermission):
    """
    DRF permission that checks the user's RBAC roles for a specific key.

    Usage on a view::

        class InvoiceApproveView(APIView):
            permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
            rbac_permission = "finance.invoice.approve"

    You can also pass multiple keys (any-of)::

            rbac_permission = ["finance.invoice.approve", "finance.invoice.admin"]

    For group-based permissions (all-of), use rbac_group_permission::

            rbac_group_permission = "finance_group"

    Or multiple groups::

            rbac_group_permission = ["finance_group", "admin_group"]

    If both rbac_permission and rbac_group_permission are set, both conditions must be met.

    The tenant context is read from ``request.rbac_tenant`` / ``request.tenant``
    (set by ``TenantJWTAuthentication`` from the ``?tenant=`` assertion).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False

        # A handful of views pair this class with a bare ``IsAuthenticated``
        # instead of ``IsAuthenticatedAndActive``, so the surface gate is
        # applied here as well. It raises rather than returning False, hence
        # the discarded result. It runs before the super-admin bypass on
        # purpose: the question is whether the tenant being operated on is
        # live, not what the caller holds.
        TenantSurfaceAllowed().has_permission(request, view)

        # Vision super admin bypasses all RBAC permission checks.
        if is_vision_super_admin(u):
            return True

        rbac_perms = getattr(view, "rbac_permission", None)
        rbac_group_perms = getattr(view, "rbac_group_permission", None)

        passed = True  # Both direct-key and group-key checks must remain satisfied.
        tenant = (
            getattr(request, "rbac_tenant", None)
            or getattr(request, "tenant", None)
            or getattr(u, "tenant", None)
        )
        # No branch is named here, deliberately. This used to read
        # ``request.branch``, an attribute no middleware has ever set, so the
        # evaluator was always asked for the "entity as a whole" scope and every
        # branch-pinned grant was discarded - a role granted for one site let its
        # holder do nothing anywhere. Access is now "any grant I hold covers this
        # key"; which rows that same holder may see is answered separately and
        # once by ``vs_rbac.scoping.visible_branch_ids``.

        if rbac_perms is not None and rbac_perms != "":
            if isinstance(rbac_perms, list) and not rbac_perms:
                raise ImproperlyConfigured(
                    f"{view.__class__.__name__}.rbac_permission cannot be an empty list."
                )
            if isinstance(rbac_perms, str):
                rbac_perms = [rbac_perms]
            # Direct permissions are any-of so views can accept equivalent operation grants.
            if not any(
                has_permission(u, perm_key, tenant=tenant)
                for perm_key in rbac_perms
            ):
                passed = False

        if rbac_group_perms is not None and rbac_group_perms != "":
            if isinstance(rbac_group_perms, list) and not rbac_group_perms:
                raise ImproperlyConfigured(
                    f"{view.__class__.__name__}.rbac_group_permission cannot be an empty list."
                )
            if isinstance(rbac_group_perms, str):
                rbac_group_perms = [rbac_group_perms]
            
            perm_keys = _group_permission_keys(rbac_group_perms)  # Group checks require every key in the bundle.

            if not has_all_permissions(u, perm_keys, tenant=tenant):
                passed = False

        return passed


# Grant access on module membership rather than one specific privilege key.
class HasAnyModuleAccess(BasePermission):
    """
    DRF permission that passes if the user holds ANY permission in a named module.

    For endpoints that carry no privilege of their own but are only meaningful to
    someone already working inside a module - shared reference data that every
    screen in that module needs. Listing every equivalent key on
    ``rbac_permission`` would work but goes stale the moment a new resource is
    added, so the check is on the module namespace instead::

        class PostingWindowView(APIView):
            permission_classes = [IsAuthenticatedAndActive & HasAnyModuleAccess]
            rbac_modules = ["finance", "procurement"]

    This is deliberately weaker than :class:`HasRBACPermission` - use it only for
    reads whose payload is not sensitive on its own. Entity/tenant scoping is a
    separate concern and still has to be enforced in the view.
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False

        # Same reasoning as HasRBACPermission: this class can be the only RBAC
        # gate on a view, so it must carry the surface check too. It raises
        # rather than returning False, hence the discarded result.
        TenantSurfaceAllowed().has_permission(request, view)

        # Vision super admin bypasses all RBAC permission checks.
        if is_vision_super_admin(u):
            return True

        modules = getattr(view, "rbac_modules", None)
        if not modules:
            raise ImproperlyConfigured(
                f"{view.__class__.__name__} uses HasAnyModuleAccess but sets no rbac_modules."
            )
        if isinstance(modules, str):
            modules = [modules]

        tenant = (
            getattr(request, "rbac_tenant", None)
            or getattr(request, "tenant", None)
            or getattr(u, "tenant", None)
        )
        # As in HasRBACPermission: no branch is named, so every grant this user
        # holds counts towards module membership.
        keys = get_effective_permissions(u, tenant=tenant)
        prefixes = tuple(f"{m}." for m in modules)  # "finance." must not match "financex.".
        return any(key.startswith(prefixes) for key in keys)


# Permit safe HTTP methods on read-only endpoints.
class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
