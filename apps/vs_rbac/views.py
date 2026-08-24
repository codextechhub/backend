from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from .models import (
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    TenantRoleChangeRequest,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
    UserPermissionOverride,
)
from .serializers import (
    UserPermissionOverrideSerializer,
    PermissionActionSerializer,
    PermissionDependencySerializer,
    PermissionDetailSerializer,
    PermissionGroupDetailSerializer,
    PermissionGroupListSerializer,
    PermissionModuleSerializer,
    PermissionResourceSerializer,
    PermissionSerializer,
    TenantRoleChangeRequestSerializer,
    TenantRoleTemplateDetailSerializer,
    TenantRoleTemplateListSerializer,
    TenantUserRoleAssignmentSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsVisionSuperAdmin,
    HasRBACPermission,
    is_vision_super_admin,
)
from .services import SUPER_ADMIN_ROLE_KEY


# -----------------------------------------------------------------------------
# Tenant-scoped RBAC - shared plumbing
# -----------------------------------------------------------------------------
# Permission keys per operation are any-of lists spanning the school-side
# (``school.roles.*``) and platform-side (``platform.roles.*``) vocabularies so
# both already-migrated grants keep working on the unified endpoint. The Vision
# super admin bypasses these checks via HasRBACPermission.
ROLE_VIEW_KEYS = ["school.roles.view", "platform.roles.view"]
# Reading the tenant's role list is a prerequisite of managing workflow
# templates: an approval stage names the role that approves it, and there is no
# way to name one without seeing the list. The alternative - making every
# template manager also a role administrator - grants far more than it needs.
# Limited to the list endpoint, which carries names, keys and counts; role
# detail and every write still take the role keys themselves.
ROLE_LIST_KEYS = ROLE_VIEW_KEYS + ["workflow.template.manage"]
ROLE_CREATE_KEYS = ["school.roles.create", "platform.roles.create"]
ROLE_UPDATE_KEYS = ["school.roles.update", "platform.roles.update"]
ROLE_DELETE_KEYS = ["school.roles.delete", "platform.roles.delete"]
ROLE_ASSIGN_KEYS = ["school.roles.assign", "platform.roles.assign"]


class TenantScopedRBACMixin:
    """Bind + validate the URL tenant slug against the authenticated tenant.

    ``request.tenant`` is established by ``TenantJWTAuthentication`` (which also
    enforces that the caller may assert that tenant). This mixin adds the
    non-enumerating guard that the URL ``tenant_slug`` matches the bound tenant,
    so a caller cannot reach another tenant's rows by changing the path.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Runs after authentication + permission checks; request.tenant is set.
        self.tenant = self.get_tenant()

    def get_tenant(self):
        slug = self.kwargs.get("tenant_slug")
        tenant = getattr(self.request, "tenant", None)
        if tenant is None or tenant.slug != slug:
            raise NotFound("No tenant matches the requested context.")
        return tenant

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["tenant"] = getattr(self, "tenant", None) or self.get_tenant()
        return context


# -----------------------------------------------------------------------------
# Permission vocabulary - Module / Resource / Action (Vision-owned)
# -----------------------------------------------------------------------------

# List and create permission modules in the Vision-owned vocabulary.
class PermissionModuleListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission modules"""
    queryset = PermissionModule.objects.all()
    serializer_class = PermissionModuleSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        # Creating vocabulary is stricter than reading it.
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one permission module by its stable name.
class PermissionModuleDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission modules"""
    queryset = PermissionModule.objects.all()
    serializer_class = PermissionModuleSerializer
    lookup_field = "name"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# List and create resources within a permission module.
class PermissionResourceListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission resources"""
    queryset = PermissionResource.objects.select_related("module").all()
    serializer_class = PermissionResourceSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset().annotate(permissions_count=Count("permissions", distinct=True))
        qp = self.request.query_params
        if module := qp.get("module"):
            qs = qs.filter(module_id=module)
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one permission resource.
class PermissionResourceDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission resources"""
    queryset = PermissionResource.objects.select_related("module").annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionResourceSerializer

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# List and create action verbs used when composing permission keys.
class PermissionActionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission actions"""
    queryset = PermissionAction.objects.all()
    serializer_class = PermissionActionSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset().annotate(permissions_count=Count("permissions", distinct=True))
        qp = self.request.query_params
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one action verb by name.
class PermissionActionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission actions"""
    queryset = PermissionAction.objects.annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionActionSerializer
    lookup_field = "name"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# -----------------------------------------------------------------------------
# Global Permission Registry (Vision-owned)
# -----------------------------------------------------------------------------
# List and create concrete permission keys from module/resource/action vocabulary.
class PermissionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permissions"""
    queryset = Permission.objects.select_related("module", "resource", "action").order_by("-updated_at", "module", "action", "key")
    serializer_class = PermissionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        # Registry writes use create rights; list views require only read access.
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params

        if module_key := qp.get("module_key"):
            qs = qs.filter(module_id=module_key)
        if action_key := qp.get("action"):
            qs = qs.filter(action_id=action_key)
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if is_restricted := qp.get("is_restricted"):
            lowered = is_restricted.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_restricted=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_restricted=False)
        if sensitivity_level := qp.get("sensitivity_level"):
            qs = qs.filter(sensitivity_level=sensitivity_level)
        if search := qp.get("search"):
            qs = qs.filter(
                Q(key__icontains=search) |
                Q(module__name__icontains=search) |
                Q(resource__name__icontains=search) |
                Q(action__name__icontains=search) |
                Q(description__icontains=search)
            )

        return qs


# Retrieve, update, or delete one concrete permission key.
class PermissionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permissions"""
    queryset = Permission.objects.prefetch_related(
        "groups", "dependencies__depends_on", "required_by__permission"
    ).all()
    lookup_field = "key"

    def get_serializer_class(self):
        # Detail reads include dependencies and group membership; writes use the lean serializer.
        if self.request.method == "GET":
            return PermissionDetailSerializer
        return PermissionSerializer

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.delete"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        try:
            instance = self.get_object()
        except Exception:
            return error_response(
                message="Permission not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        if not serializer.is_valid():
            return error_response(
                message="Invalid data.",
                error={"errors": serializer.errors},
            )

        try:
            validated = serializer.validated_data

            # Auto-compute new key from whatever module/resource/action ended up in
            # validated_data (new value if sent, existing instance value otherwise).
            # key is read-only in the serializer so we handle the PK update here.
            new_module = validated.get("module", instance.module)
            new_resource = validated.get("resource", instance.resource)
            new_action = validated.get("action", instance.action)
            new_key = f"{new_module.pk}.{new_resource.name}.{new_action.pk}"

            if new_key != instance.key:
                # Updating module/resource/action changes the natural key used by role grants.
                Permission.objects.filter(key=instance.key).update(key=new_key)
                instance.key = new_key

            self.perform_update(serializer)
        except Exception as exc:
            return error_response(
                message="Update failed.",
                error={"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return success_response(
            message="Permission updated successfully.",
            data=serializer.data,
        )

    def delete(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
        except Exception:
            return error_response(
                message="Permission not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            self.perform_destroy(instance)
        except Exception as exc:
            return error_response(
                message="Delete failed.",
                error={"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return success_response(message="Permission deleted successfully.")


# List and create dependency rules between permission keys.
class PermissionDependencyListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission dependencies"""
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or remove one dependency rule.
class PermissionDependencyDetailView(RetrieveModelMixin, DestroyModelMixin, generics.RetrieveDestroyAPIView):
    """docstring-name: Permission dependencies"""
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# -----------------------------------------------------------------------------
# Permission Groups (Vision-owned, shared across school + platform roles)
# -----------------------------------------------------------------------------
# List and create reusable permission bundles.
class PermissionGroupListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list all permission groups
    - POST: create a new permission group with optional permission_keys

    docstring-name: Permission groups
    """
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PermissionGroup.objects.all()
            .annotate(permissions_count=Count("group_permissions", distinct=True))
            .order_by("name")
        )

        is_active = self.request.query_params.get("is_active")
        is_system = self.request.query_params.get("is_system")

        if is_active is not None:
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)

        if is_system is not None:
            lowered = is_system.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_system=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_system=False)
        if search := self.request.query_params.get("search"):
            qs = qs.filter(Q(name__icontains=search))

        return qs

    def get_serializer_class(self):
        # Create accepts permission_keys, while list keeps the payload compact.
        if self.request.method == "POST":
            return PermissionGroupDetailSerializer
        return PermissionGroupListSerializer


# Retrieve, update, or delete one permission bundle.
class PermissionGroupDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: group detail with expanded permissions
    - PATCH/PUT: update group fields and optionally replace permission_keys
    - DELETE: blocked for system groups

    docstring-name: Permission groups
    """
    serializer_class = PermissionGroupDetailSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        return PermissionGroup.objects.all().prefetch_related("permissions")

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            # System bundles may back shipped roles, so they are not user-deletable.
            return error_response(
                message="System permission groups cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# Tenant-scoped Role Templates
# -----------------------------------------------------------------------------
# List and create role templates inside one tenant boundary.
class TenantRoleTemplateListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list role templates in a tenant
    - POST: create a role template in a tenant

    docstring-name: Roles
    """
    pagination_class = XVSPagination
    # Open to a school that has not gone live. "Confirm Default Roles & RBAC" is
    # the first step on the onboarding checklist, and a school cannot confirm
    # roles it is refused sight of. Reading and shaping its OWN roles is safe
    # before go-live for the same reason it is safe after: a tenant role can
    # only ever hold keys declared ``PermissionScope.TENANT``, enforced on the
    # grant models themselves (``assert_tenant_may_hold``) and again in the
    # evaluator, so nothing here can reach across the tenant boundary.
    #
    # DELETE is deliberately absent. Onboarding asks a school to confirm and
    # extend its roles, not to dismantle the baseline CodeX seeded - and the
    # gate that checks the baseline is intact reads those very rows.
    pending_tenant_surface = ("get", "post")

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_CREATE_KEYS
        else:
            self.rbac_permission = ROLE_LIST_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qp = self.request.query_params
        qs = (
            TenantRoleTemplate.objects.filter(tenant=tenant)
            .annotate(
                # Count the people, not the grants. ``distinct=True`` over
                # ``user_assignments`` de-duplicates assignment rows, and one
                # person holding Storekeeper at Ikeja and at Lekki is two of
                # those and one user - so the roles list would report 2.
                assigned_users_count=Count(
                    "user_assignments__user",
                    filter=Q(user_assignments__assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE),
                    distinct=True,
                ),
                permissions_count=Count(
                    "role_permissions",
                    filter=Q(role_permissions__granted=True),
                    distinct=True,
                ),
            )
            .select_related("created_by", "tenant", "branch")
            .order_by("name")
        )
        if branch_id := qp.get("branch"):
            qs = qs.filter(branch_id=branch_id)
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TenantRoleTemplateDetailSerializer
        return TenantRoleTemplateListSerializer


def _permission_label(permission) -> str:
    """The sentence a person reads beside the checkbox.

    ``description`` when the registry has one, which is most of them. When it
    does not, the key is composed back into English from its own parts rather
    than printed raw: 48 of the keys a school can hold carry no description,
    and they are all in ``academics`` and ``school`` - precisely the modules a
    school spends this screen in. "school.administrators.view" is not a label,
    but "View administrators" is, and it is built from the same two fields the
    key itself is built from, so it cannot describe a different permission than
    the one it sits beside.
    """
    described = (permission.description or "").strip()
    if described:
        return described
    action = (permission.action_id or "").replace("_", " ").strip()
    resource = (
        permission.resource.name if permission.resource_id else ""
    ).replace("_", " ").strip()
    if not action and not resource:
        return permission.key
    return f"{action} {resource}".strip().capitalize()


# The permissions a tenant may pick from, grouped the way a picker shows them.
class TenantPermissionCatalogueView(TenantScopedRBACMixin, APIView):
    """GET /rbac/tenants/<slug>/permission-catalogue/ - what this tenant may grant.

    Why this exists beside ``vision/permissions/``: that endpoint is the global
    registry, gated on ``platform.permissions.view`` and carrying every key on
    the platform, including the ones only CodeX may ever hold. A school editing
    its own roles needs the opposite - the short list it is actually allowed to
    tick - and had no way to ask for it. Without this, the roles screen can
    show which permissions a role HAS and offer no way to add one.

    The filter is ``PermissionScope.TENANT``, which is the same column the
    grant guard on the models and the evaluator both read. So the picker cannot
    offer a key that the save would refuse, and cannot leak the existence of the
    platform-only ones. A platform tenant sees everything, because it may hold
    everything.

    Grouped by module and returned whole rather than paginated: it is a
    vocabulary, not a list of records, and a picker that has to page through
    its own options in order to tick two boxes is not a picker.

    docstring-name: Permission catalogue
    """

    # A school confirms its roles during onboarding, so the vocabulary those
    # roles are written in has to be readable then. Read-only, and narrower
    # than the registry it stands in front of.
    pending_tenant_surface = ("get",)

    def get_permissions(self):
        # Whoever may see this tenant's roles may see what those roles could
        # hold. Anything narrower would leave a reader able to open a role and
        # unable to read the labels on its own permissions.
        self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def _capability_reader(self, tenant):
        """A function answering "is this capability on for this school?".

        Two things it is careful about.

        **It asks the capability service, not the entitlement table.** Whether a
        capability is on is computed from entitlement AND dependencies AND
        operator overrides AND its own default; reading the entitlement rows
        directly would disagree with the rest of the platform.

        **A school with nothing switched on is treated as having everything.**
        Entitlements are not granted at provisioning yet, so today every school
        answers False to every capability. "Not provisioned" and "not bought"
        are different facts and only one of them should hide a permission - and
        with no way to tell them apart, hiding would empty this screen for every
        school on the platform. So when a tenant has no capability on at all,
        the catalogue offers everything and flags nothing. The moment
        provisioning starts granting entitlements, the flags become real with no
        change here.
        """
        from vs_config.conf import is_capability_enabled
        from vs_config.models import Capability

        cache: dict[str, bool] = {}

        def enabled(key: str) -> bool:
            if key not in cache:
                try:
                    cache[key] = bool(is_capability_enabled(key, tenant=tenant))
                except Exception:  # noqa: BLE001 - a broken graph must not 500 a picker
                    cache[key] = False
            return cache[key]

        anything_on = any(
            enabled(key)
            for key in Capability.objects.filter(is_active=True)
            .values_list("key", flat=True)
        )

        def is_on(capability: str | None) -> bool:
            if capability is None:
                return True
            if not anything_on:
                return True
            return enabled(capability)

        return is_on

    def get(self, request, *args, **kwargs):
        from .capability_map import capability_for
        from .models import PermissionScope, tenant_is_platform
        from .unenforced import UNENFORCED_KEYS

        tenant = self.get_tenant()

        permissions = (
            Permission.objects.filter(is_active=True)
            .select_related("module", "resource", "action")
            .order_by("module_id", "resource_id", "action_id")
        )
        if not tenant_is_platform(tenant):
            permissions = permissions.filter(scope=PermissionScope.TENANT)
            # Withhold keys that gate nothing. A box that changes nothing when
            # ticked is worse than an absent one: it tells the person editing
            # the role she has granted something she has not. See
            # ``unenforced.py`` for what each one was expected to control and
            # what actually controls it.
            permissions = permissions.exclude(key__in=UNENFORCED_KEYS)

        is_on = self._capability_reader(tenant)

        modules: dict[str, dict] = {}
        for permission in permissions:
            resource = permission.resource.name if permission.resource_id else ""
            capability = capability_for(permission.module_id, resource)
            available = is_on(capability)

            bucket = modules.setdefault(
                permission.module_id,
                {"module": permission.module_id, "available": False, "permissions": []},
            )
            # A module is offerable when anything in it is. ``school`` holds the
            # school's own branches and roles alongside its students, so it is
            # never wholly unavailable even to a school that bought neither the
            # students nor the teachers module.
            bucket["available"] = bucket["available"] or available
            bucket["permissions"].append({
                "key": permission.key,
                "label": _permission_label(permission),
                "resource": resource,
                "action": permission.action_id,
                "sensitivity": permission.sensitivity_level,
                # Flagged so the picker can say so. These flow through an
                # approval rather than taking effect on save.
                "is_restricted": permission.is_restricted,
                # Which product this permission belongs to, and whether the
                # school has it. Null capability means core: every school.
                "capability": capability,
                "available": available,
            })

        return success_response(
            message="Data retrieved successfully",
            data=list(modules.values()),
        )


# Retrieve or mutate one tenant role template (addressed by per-tenant key).
class TenantRoleTemplateDetailView(TenantScopedRBACMixin, RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Tenant-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: blocked for system or locked roles

    docstring-name: Roles
    """
    serializer_class = TenantRoleTemplateDetailSerializer
    lookup_field = "key"
    # Read and edit, never delete, before go-live. See the note on the list
    # view above; DELETE stays closed so a school cannot dismantle the seeded
    # baseline that the onboarding gate is checking.
    pending_tenant_surface = ("get", "patch", "put")

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = ROLE_DELETE_KEYS
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = ROLE_UPDATE_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantRoleTemplate.objects.filter(tenant=tenant)
            .select_related("created_by", "tenant", "branch")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        super_admin = is_vision_super_admin(request.user)
        if instance.is_locked and not super_admin:
            return error_response(
                message="This role is locked and cannot be modified.",
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.is_system_role and not super_admin:
            return error_response(
                message="System roles cannot be modified.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system_role:
            return error_response(
                message="System roles cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.is_locked:
            return error_response(
                message="This role is locked and cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# Tenant-scoped Role Assignments
# -----------------------------------------------------------------------------
# List and create tenant-scoped role assignments.
class TenantUserRoleAssignmentListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list assignments in a tenant
    - POST: assign a role to a user inside a tenant

    docstring-name: Role assignments
    """
    serializer_class = TenantUserRoleAssignmentSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_ASSIGN_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantUserRoleAssignment.objects.filter(tenant=tenant)
            .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
            .order_by("-created_at")
        )
        qp = self.request.query_params
        if user_id := qp.get("user"):
            qs = qs.filter(user_id=user_id)
        if role := qp.get("role"):
            if role.isdigit():
                qs = qs.filter(role_id=role)
            else:
                qs = qs.filter(role__key=role)
        if assignment_status := qp.get("assignment_status"):
            qs = qs.filter(assignment_status=assignment_status)
        return qs


# Retrieve or update one tenant-scoped role assignment.
class TenantUserRoleAssignmentDetailView(TenantScopedRBACMixin, RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    Tenant-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow

    docstring-name: Role assignments
    """
    serializer_class = TenantUserRoleAssignmentSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = ROLE_ASSIGN_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantUserRoleAssignment.objects.filter(tenant=tenant)
            .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
        )


# Revoke a tenant role assignment with an audit reason.
class TenantUserRoleAssignmentRevokeView(TenantScopedRBACMixin, APIView):
    """
    Tenant-facing revoke endpoint for role assignments.

    POST /rbac/tenants/<slug>/role-assignments/<id>/revoke/
    Body: { "reason_note": "Required justification for the audit trail." }

    docstring-name: Revoke a role assignment
    """

    def get_permissions(self):
        self.rbac_permission = ROLE_ASSIGN_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def post(self, request, tenant_slug: str, id: int):
        tenant = self.tenant

        try:
            assignment = (
                TenantUserRoleAssignment.objects
                .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
                .get(id=id, tenant=tenant)
            )
        except TenantUserRoleAssignment.DoesNotExist:
            return error_response(
                message="Assignment not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if assignment.assignment_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED:
            return error_response(
                message="This assignment has already been revoked.",
                status=status.HTTP_409_CONFLICT,
            )

        if assignment.role.key == SUPER_ADMIN_ROLE_KEY:
            return error_response(
                message="Transfer Super Admin before revoking this assignment.",
                code="SUPER_ADMIN_TRANSFER_REQUIRED",
                status=status.HTTP_409_CONFLICT,
            )

        reason = (request.data.get("reason_note") or "").strip()
        if not reason:
            return error_response(
                message="A reason is required to revoke an assignment.",
                error={"reason_note": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignment.revoke(by_user=request.user, reason=reason)
        assignment.save(update_fields=[
            "assignment_status", "revoked_at", "revoked_by", "reason_note", "updated_at",
        ])

        return success_response(
            message="Assignment revoked successfully.",
            data=TenantUserRoleAssignmentSerializer(
                assignment, context={"request": request, "tenant": tenant}
            ).data,
        )


class TenantUserRoleAssignmentReplaceView(TenantScopedRBACMixin, APIView):
    """Atomically replace one active role assignment with another.

    This is intentionally assignment-scoped instead of user-scoped: tenants may
    grant more than one role to a user, and changing one role must not silently
    revoke their other legitimate assignments.
    """

    def get_permissions(self):
        self.rbac_permission = ROLE_ASSIGN_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    @transaction.atomic
    def post(self, request, tenant_slug: str, id: int):
        tenant = self.tenant
        target_role_id = request.data.get("role")

        if not target_role_id:
            return error_response(
                message="Select the new role for this assignment.",
                error={"role": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            assignment = (
                TenantUserRoleAssignment.objects
                .select_for_update(of=("self",))
                .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
                .get(id=id, tenant=tenant)
            )
        except TenantUserRoleAssignment.DoesNotExist:
            return error_response(
                message="Assignment not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if assignment.assignment_status != TenantUserRoleAssignment.AssignmentStatus.ACTIVE:
            return error_response(
                message="Only an active assignment can be changed.",
                status=status.HTTP_409_CONFLICT,
            )

        if assignment.role.key == SUPER_ADMIN_ROLE_KEY:
            return error_response(
                message="Transfer Super Admin before changing this assignment.",
                code="SUPER_ADMIN_TRANSFER_REQUIRED",
                status=status.HTTP_409_CONFLICT,
            )

        try:
            target_role = TenantRoleTemplate.objects.get(
                id=target_role_id,
                tenant=tenant,
                status=TenantRoleTemplate.Status.ACTIVE,
            )
        except (TenantRoleTemplate.DoesNotExist, ValueError, TypeError):
            return error_response(
                message="The selected role is not active in this tenant.",
                error={"role": ["Select a valid active role."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if target_role.key == SUPER_ADMIN_ROLE_KEY:
            return error_response(
                message="Use Transfer Super Admin to assign the Super Admin role.",
                code="SUPER_ADMIN_TRANSFER_REQUIRED",
                status=status.HTTP_409_CONFLICT,
            )

        if assignment.role_id == target_role.id:
            return error_response(
                message="The user already has this role through the selected assignment.",
                status=status.HTTP_409_CONFLICT,
            )

        # The replacement is written at ``assignment.branch`` (below), so the
        # duplicate it must not collide with is the one at that same branch -
        # not any grant of the role anywhere in the tenant. Holding Storekeeper
        # at Ikeja is no reason to refuse Storekeeper at Lekki.
        duplicate = (
            TenantUserRoleAssignment.conflicting_active_grants(
                tenant=tenant,
                user=assignment.user,
                role=target_role,
                branch=assignment.branch,
                exclude_pk=assignment.pk,
            )
            .select_for_update()
            .exists()
        )
        if duplicate:
            return error_response(
                message=TenantUserRoleAssignment.duplicate_grant_message(
                    target_role, assignment.branch,
                ),
                status=status.HTTP_409_CONFLICT,
            )

        supplied_reason = (request.data.get("reason_note") or "").strip()
        reason = supplied_reason or f"Role changed from {assignment.role.name} to {target_role.name}."

        assignment.revoke(
            by_user=request.user,
            reason=f"Changed to {target_role.name}. {reason}",
        )
        assignment.save(update_fields=[
            "assignment_status", "revoked_at", "revoked_by", "reason_note", "updated_at",
        ])

        replacement = TenantUserRoleAssignment.objects.create(
            tenant=tenant,
            branch=assignment.branch,
            user=assignment.user,
            role=target_role,
            assigned_by=request.user,
            reason_note=reason,
        )

        return success_response(
            message="Role changed successfully.",
            data=TenantUserRoleAssignmentSerializer(
                replacement, context={"request": request, "tenant": tenant}
            ).data,
            status=status.HTTP_201_CREATED,
        )


# -----------------------------------------------------------------------------
# Tenant Role Change Requests (tenant-internal approval)
# -----------------------------------------------------------------------------
# List and create tenant-internal role change requests.
class TenantRoleChangeRequestListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list requests for a tenant
    - POST: create a change request for a role in that tenant

    docstring-name: Role change requests
    """
    serializer_class = TenantRoleChangeRequestSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_UPDATE_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )
        qp = self.request.query_params
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        if role_id := qp.get("target_role"):
            qs = qs.filter(target_role_id=role_id)
        return qs


# List role change requests that tenant admins can review.
class TenantRoleChangeRequestApprovalQueueView(TenantScopedRBACMixin, generics.ListAPIView):
    """
    Tenant-admin-facing:
    - GET: role change requests for a tenant (filter by ?status=)

    docstring-name: Role change approval queue
    """
    serializer_class = TenantRoleChangeRequestSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )
        qp = self.request.query_params
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        if target_role := qp.get("target_role"):
            qs = qs.filter(target_role_id=target_role)
        return qs


# Retrieve one tenant role change request for review.
class TenantRoleChangeRequestApprovalDetailView(TenantScopedRBACMixin, RetrieveModelMixin, generics.RetrieveAPIView):
    """
    Tenant-admin-facing:
    - GET: single role change request within the tenant

    docstring-name: Role change approval queue
    """
    serializer_class = TenantRoleChangeRequestSerializer
    lookup_field = "id"

    def get_permissions(self):
        self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
        )


# Decide a tenant role change request and apply approved permission deltas.
class TenantRoleChangeRequestDecisionView(TenantScopedRBACMixin, APIView):
    """
    Tenant-admin decision endpoint for role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }

    docstring-name: Decide a role change request
    """

    def get_permissions(self):
        self.rbac_permission = ROLE_UPDATE_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def post(self, request, tenant_slug: str, request_id: str):
        tenant = self.tenant
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = TenantRoleChangeRequest.objects.select_related("target_role", "tenant").get(
                id=request_id, tenant=tenant,
            )
        except TenantRoleChangeRequest.DoesNotExist:
            return error_response(
                message="Request not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != TenantRoleChangeRequest.Status.PENDING:
            return error_response(
                message=f"Request already decided ({obj.status}).",
                status=status.HTTP_409_CONFLICT,
            )

        if action == "DENY":
            if not notes:
                return error_response(
                    message="Denial reason is required.",
                    error={"notes": ["Denial reason is required."]},
                )
            obj.mark_denied(reviewer=request.user, notes=notes)
            obj.save(update_fields=[
                "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
            ])
            return success_response(
                message="Role change request denied.",
                data=TenantRoleChangeRequestSerializer(
                    obj, context={"request": request, "tenant": tenant}
                ).data,
            )

        if action == "APPROVE":
            try:
                with transaction.atomic():
                    from .services import apply_role_change_request
                    apply_role_change_request(obj=obj, reviewer=request.user, notes=notes)
            except Exception as exc:
                obj.mark_apply_failed(reviewer=request.user, notes=str(exc))
                obj.save(update_fields=[
                    "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
                ])
                return error_response(
                    message="Approval failed while applying changes.",
                    error={"error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            return success_response(
                message="Role change request approved.",
                data=TenantRoleChangeRequestSerializer(
                    obj, context={"request": request, "tenant": tenant}
                ).data,
            )

        return error_response(
            message="Invalid action. Must be APPROVE or DENY.",
            error={"action": ["Must be APPROVE or DENY."]},
        )


# -----------------------------------------------------------------------------
# Per-user permission overrides
# -----------------------------------------------------------------------------
# Which namespace gates the endpoint is decided by the ACTOR's home tenant, not
# by the target - exactly like impersonation (vs_admin_console.views). The two
# sets are never unioned, so a school role that somehow carried a platform key
# still gets no extra reach: a school actor cannot assert another tenant at all
# (TenantJWTAuthentication), and a platform actor needs the platform key.
OVERRIDE_PLATFORM_KEYS = {
    "view": "platform.team_overrides.view",
    "manage": "platform.team_overrides.manage",
}
OVERRIDE_SCHOOL_KEYS = {
    "view": "school.user_overrides.view",
    "manage": "school.user_overrides.manage",
}


def _override_keys(actor) -> dict:
    from vs_tenants.models import Tenant

    is_platform = getattr(getattr(actor, "tenant", None), "kind", None) == Tenant.Kind.PLATFORM
    return OVERRIDE_PLATFORM_KEYS if is_platform else OVERRIDE_SCHOOL_KEYS


class _UserPermissionOverrideBase(TenantScopedRBACMixin):
    """Shared target resolution, self-override ban and audit for override views.

    Self-visibility rule (owner requirement): there is deliberately **no**
    self-service exemption here. Reading your own overrides still requires the
    viewer's ``.view``/``.manage`` key, so a user without it can never learn
    that exceptions exist on their account - they only observe permissions
    working or not working. Nothing about overrides is exposed on ``/me`` or
    any self-service profile serializer.
    """

    # Lets a platform (CX) actor manage a school user's overrides by asserting
    # ?tenant=<school-slug>; RBAC still evaluates against the actor's own
    # tenant (request.rbac_tenant), so the platform key is what is required.
    platform_cross_tenant_param = True

    def _actor(self):
        return getattr(self.request, "actor_user", None) or self.request.user

    def get_target_user(self):
        from django.contrib.auth import get_user_model

        user = (
            get_user_model().objects
            .select_related("tenant")
            .filter(pk=self.kwargs.get("user_id"), tenant=self.tenant)
            .first()
        )
        if user is None:
            # Non-enumerating: a user in another tenant is indistinguishable
            # from a user that does not exist.
            raise NotFound("No user matches the requested context.")
        return user

    def _reject_self(self, target):
        """Nobody may create or lift an override on themselves.

        Prevents self-escalation via ALLOW (and reviewer-dodging via a token
        self-DENY). Both identities are checked so an impersonator cannot use
        a proxy session to edit their own - or the proxied user's own - access.
        """
        actor = self._actor()
        effective = getattr(self.request, "user", None)
        if target.pk in {getattr(actor, "pk", None), getattr(effective, "pk", None)}:
            return error_response(
                message="You cannot create or lift permission overrides on yourself.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _role_permission_keys(self, target):
        from .evaluator import get_role_permissions

        return get_role_permissions(target, tenant=self.tenant)

    def _audit(self, *, action_type, override, target, summary, replaced=False):
        from .audit import record_rbac_audit

        record_rbac_audit(
            action_type=action_type,
            entity_type="UserPermissionOverride",
            entity_id=str(override.pk),
            entity_label=f"{getattr(target, 'email', target.pk)}:{override.permission_id}",
            actor_user=self._actor(),
            severity="WARNING",
            summary=summary,
            metadata={
                "school_id": self.tenant.slug,
                "tenant_id": str(self.tenant.pk),
                "target_user_id": str(target.pk),
                "target_user_email": getattr(target, "email", ""),
                "permission_key": override.permission_id,
                "mode": override.mode,
                "reason": override.reason,
                "expires_at": override.expires_at.isoformat() if override.expires_at else None,
                "replaced": replaced,
            },
        )


# List a user's permission overrides, or create one.
class UserPermissionOverrideListCreateView(
    _UserPermissionOverrideBase, generics.ListCreateAPIView,
):
    """
    Tenant-facing:
    - GET: list the permission exceptions on one user (viewer needs the
      ``.view`` or ``.manage`` key - including for their own id).
    - POST: create an exception. Both modes apply immediately; a new override
      for a key the user already has REPLACES the old row (both audited).

    docstring-name: User permission overrides
    """

    serializer_class = UserPermissionOverrideSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        keys = _override_keys(self._actor())
        if self.request.method == "POST":
            self.rbac_permission = keys["manage"]
        else:
            # Managing implies seeing.
            self.rbac_permission = [keys["view"], keys["manage"]]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        target = getattr(self, "_target", None) or self.get_target_user()
        self._target = target
        qs = (
            UserPermissionOverride.objects
            .filter(tenant=self.tenant, user=target)
            .select_related("permission", "created_by")
            .order_by("-created_at")
        )
        if mode := self.request.query_params.get("mode"):
            qs = qs.filter(mode=mode.upper())
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        target = getattr(self, "_target", None) or self.get_target_user()
        self._target = target
        context["role_permission_keys"] = self._role_permission_keys(target)
        # The tenant the override will land in decides which keys may be
        # granted through it (see UserPermissionOverrideSerializer.validate).
        context["tenant"] = self.tenant
        return context

    def create(self, request, *args, **kwargs):
        target = self.get_target_user()
        self._target = target
        if (denied := self._reject_self(target)) is not None:
            return denied

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        permission = serializer.validated_data["permission"]

        actor = self._actor()
        with transaction.atomic():
            existing = (
                UserPermissionOverride.objects
                .select_for_update()
                .filter(user=target, permission=permission)
                .first()
            )
            if existing is not None:
                # Replace rather than stack - the unique constraint guarantees
                # one row per (user, key). Both halves land in the audit trail.
                self._audit(
                    action_type="OVERRIDE_LIFTED",
                    override=existing,
                    target=target,
                    summary=(
                        f"{existing.mode} override on '{existing.permission_id}' for "
                        f"{getattr(target, 'email', target.pk)} replaced by a new override"
                    ),
                    replaced=True,
                )
                existing.delete()

            override = serializer.save(
                tenant=self.tenant, user=target, created_by=actor,
            )
            self._audit(
                action_type="OVERRIDE_CREATED",
                override=override,
                target=target,
                summary=(
                    f"{override.mode} override on '{override.permission_id}' created for "
                    f"{getattr(target, 'email', target.pk)}"
                ),
                replaced=existing is not None,
            )

        return success_response(
            message="Permission override applied.",
            data=self.get_serializer(override).data,
            status=status.HTTP_201_CREATED,
        )


# Lift (delete) one permission override.
class UserPermissionOverrideDetailView(_UserPermissionOverrideBase, APIView):
    """
    Tenant-facing:
    - DELETE: lift an override. A lifted DENY restores role access; a lifted
      ALLOW removes the extra grant. Effective on the target's next request.

    docstring-name: User permission overrides
    """

    def get_permissions(self):
        self.rbac_permission = _override_keys(self._actor())["manage"]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def delete(self, request, tenant_slug: str, user_id: int, id: int):
        target = self.get_target_user()
        if (denied := self._reject_self(target)) is not None:
            return denied

        override = (
            UserPermissionOverride.objects
            .select_related("permission", "created_by")
            .filter(pk=id, tenant=self.tenant, user=target)
            .first()
        )
        if override is None:
            return error_response(
                message="Permission override not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            self._audit(
                action_type="OVERRIDE_LIFTED",
                override=override,
                target=target,
                summary=(
                    f"{override.mode} override on '{override.permission_id}' lifted for "
                    f"{getattr(target, 'email', target.pk)}"
                ),
            )
            override.delete()

        return success_response(message="Permission override lifted.")


# -----------------------------------------------------------------------------
# Super Admin Transfer (codex tenant)
# -----------------------------------------------------------------------------
# Transfer the singleton Vision super-admin role to another Vision staff user.
class TransferSuperAdminView(APIView):
    """
    POST platform/transfer-super-admin/

    Allows the current Vision Super Admin to transfer their role to another
    Vision Staff member. The caller is demoted to Vision Platform Admin. Operates
    on the codex platform tenant's TenantUserRoleAssignment rows.

    Body: { "new_super_admin_id": "<uuid>" }

    docstring-name: Transfer super admin
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin, HasRBACPermission]
    rbac_permission = "platform.roles.transfer"

    def post(self, request):
        from django.conf import settings
        from django.apps import apps
        UserModel = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
        from .services import transfer_super_admin

        new_id = request.data.get("new_super_admin_id")
        if not new_id:
            return error_response(
                message="new_super_admin_id is required.",
                error={"new_super_admin_id": ["This field is required."]},
            )

        try:
            new_user = UserModel.objects.get(pk=new_id)
        except (UserModel.DoesNotExist, Exception):
            return error_response(
                message="User not found.",
                error={"new_super_admin_id": ["No user with this ID exists."]},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            # The service owns demotion, revocation of existing roles, and audit.
            transfer_super_admin(from_user=request.user, to_user=new_user)
        except ValueError as exc:
            return error_response(message=str(exc), error={})

        return success_response(
            message=f"Super admin role transferred to {new_user.email}. You are now a Platform Admin.",
        )
