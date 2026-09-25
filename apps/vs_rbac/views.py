from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from .models import (
    FieldDefinition,
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    TenantRoleChangeRequest,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
    UserFieldAccessOverride,
    UserPermissionOverride,
)
from .serializers import (
    FieldDefinitionSerializer,
    UserFieldAccessOverrideSerializer,
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
ROLE_LIST_KEYS = ROLE_VIEW_KEYS + ["workflow.template.update"]
ROLE_CREATE_KEYS = ["school.roles.create", "platform.roles.create"]
ROLE_UPDATE_KEYS = ["school.roles.update", "platform.roles.update"]
ROLE_APPROVE_KEYS = ["school.roles.approve", "platform.roles.approve"]
ROLE_DELETE_KEYS = ["school.roles.delete", "platform.roles.delete"]
ROLE_ASSIGN_KEYS = ["school.roles.assign", "platform.roles.assign"]
# Field Access switches on a tenant's roles, in the same dual vocabulary.
FIELD_ACCESS_VIEW_KEYS = ["school.field_access.view", "platform.field_access.view"]
FIELD_ACCESS_UPDATE_KEYS = ["school.field_access.update", "platform.field_access.update"]


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
# Permission vocabulary - Module / Resource / Action (backend-owned)
# -----------------------------------------------------------------------------

# Permission definitions are registered by the backend seeders. These endpoints
# expose that vocabulary to the console without allowing it to become a second
# source of truth.
class RegistryReadOnlyMixin:
    rbac_permission = "platform.permissions.view"

    def get_permissions(self):
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


class PermissionModuleListCreateView(RegistryReadOnlyMixin, generics.ListAPIView):
    """List backend-owned permission modules.

    docstring-name: Permission modules
    """
    queryset = PermissionModule.objects.order_by("name")
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


class PermissionModuleDetailView(
    RegistryReadOnlyMixin, RetrieveModelMixin, generics.RetrieveAPIView
):
    """Retrieve one backend-owned permission module.

    docstring-name: Permission modules
    """
    queryset = PermissionModule.objects.all()
    serializer_class = PermissionModuleSerializer
    lookup_field = "name"


class PermissionResourceListCreateView(RegistryReadOnlyMixin, generics.ListAPIView):
    """List backend-owned resources within permission modules.

    docstring-name: Permission resources
    """
    queryset = PermissionResource.objects.select_related("module").order_by(
        "module_id", "name",
    )
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


class PermissionResourceDetailView(
    RegistryReadOnlyMixin, RetrieveModelMixin, generics.RetrieveAPIView
):
    """Retrieve one backend-owned permission resource.

    docstring-name: Permission resources
    """
    queryset = PermissionResource.objects.select_related("module").annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionResourceSerializer


class PermissionActionListCreateView(RegistryReadOnlyMixin, generics.ListAPIView):
    """List backend-owned action verbs used in permission keys.

    docstring-name: Permission actions
    """
    queryset = PermissionAction.objects.order_by("name")
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


class PermissionActionDetailView(
    RegistryReadOnlyMixin, RetrieveModelMixin, generics.RetrieveAPIView
):
    """Retrieve one backend-owned permission action.

    docstring-name: Permission actions
    """
    queryset = PermissionAction.objects.annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionActionSerializer
    lookup_field = "name"

# -----------------------------------------------------------------------------
# Global Permission Registry (backend-owned)
# -----------------------------------------------------------------------------
class PermissionListCreateView(RegistryReadOnlyMixin, generics.ListAPIView):
    """List backend-owned permission definitions.

    docstring-name: Permissions
    """
    queryset = Permission.objects.select_related(
        "module", "resource", "action",
    ).order_by("module_id", "resource__name", "action_id")
    serializer_class = PermissionSerializer
    pagination_class = XVSPagination

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


class PermissionDetailView(
    RegistryReadOnlyMixin, RetrieveModelMixin, generics.RetrieveAPIView
):
    """Retrieve one backend-owned permission definition.

    docstring-name: Permissions
    """
    queryset = Permission.objects.prefetch_related(
        "groups", "dependencies__depends_on", "required_by__permission"
    ).all()
    lookup_field = "key"

    serializer_class = PermissionDetailSerializer


class PermissionDependencyListCreateView(RegistryReadOnlyMixin, generics.ListAPIView):
    """List backend-owned dependency rules between permission keys.

    docstring-name: Permission dependencies
    """
    queryset = PermissionDependency.objects.select_related(
        "permission", "depends_on",
    ).order_by(
        "permission__module_id", "permission__resource__name",
        "permission__action_id", "depends_on_id",
    )
    serializer_class = PermissionDependencySerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if search := (params.get("search") or "").strip():
            qs = qs.filter(
                Q(permission__key__icontains=search)
                | Q(permission__description__icontains=search)
                | Q(depends_on__key__icontains=search)
                | Q(depends_on__description__icontains=search)
            )

        graph_for = (params.get("graph_for") or "").strip()
        if not graph_for:
            return qs

        edges = list(qs.values_list("id", "permission_id", "depends_on_id"))
        connected = {graph_for}
        changed = True
        while changed:
            changed = False
            for _pk, permission_key, required_key in edges:
                if permission_key in connected or required_key in connected:
                    before = len(connected)
                    connected.update({permission_key, required_key})
                    changed = changed or len(connected) != before
        edge_ids = [
            pk
            for pk, permission_key, required_key in edges
            if permission_key in connected and required_key in connected
        ]
        return qs.filter(pk__in=edge_ids)


class PermissionDependencyDetailView(
    RegistryReadOnlyMixin, RetrieveModelMixin, generics.RetrieveAPIView
):
    """Retrieve one backend-owned permission dependency.

    docstring-name: Permission dependencies
    """
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    lookup_field = "id"

# -----------------------------------------------------------------------------
# Permission Groups (administrator-owned role-building shortcuts)
# -----------------------------------------------------------------------------
class PermissionGroupListCreateView(
    CreateModelMixin, generics.ListCreateAPIView,
):
    """List and create administrator-owned permission bundles.

    docstring-name: Permission groups
    """
    pagination_class = XVSPagination

    def get_permissions(self):
        self.rbac_permission = (
            "platform.permission_groups.create"
            if self.request.method == "POST"
            else "platform.permissions.view"
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PermissionGroup.objects.filter(is_system=False)
            .annotate(permissions_count=Count("group_permissions", distinct=True))
            .order_by("name")
        )

        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)

        if search := self.request.query_params.get("search"):
            qs = qs.filter(Q(name__icontains=search))

        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PermissionGroupDetailSerializer
        return PermissionGroupListSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        group = serializer.save()
        from .audit import record_rbac_audit

        record_rbac_audit(
            action_type="permission_group.create",
            entity_type="permission_group",
            entity_id=str(group.pk),
            entity_label=group.name,
            actor_user=self.request.user,
            summary=f"Created permission group {group.name}.",
            diff_data={
                "permission_keys": sorted(
                    group.permissions.values_list("key", flat=True),
                ),
            },
        )


class PermissionGroupDetailView(
    RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """Retrieve or change one administrator-owned permission bundle.

    docstring-name: Permission groups
    """
    serializer_class = PermissionGroupDetailSerializer
    lookup_field = "id"

    def get_permissions(self):
        self.rbac_permission = (
            "platform.permission_groups.delete"
            if self.request.method == "DELETE"
            else "platform.permission_groups.update"
            if self.request.method in {"PUT", "PATCH"}
            else "platform.permissions.view"
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        return PermissionGroup.objects.filter(is_system=False).prefetch_related(
            "permissions",
        )

    @transaction.atomic
    def perform_update(self, serializer):
        group = serializer.instance
        before = {
            "name": group.name,
            "description": group.description,
            "is_active": group.is_active,
            "permission_keys": sorted(
                group.permissions.values_list("key", flat=True),
            ),
        }
        updated = serializer.save()
        from .audit import record_rbac_audit

        record_rbac_audit(
            action_type="permission_group.update",
            entity_type="permission_group",
            entity_id=str(updated.pk),
            entity_label=updated.name,
            actor_user=self.request.user,
            summary=f"Updated permission group {updated.name}.",
            before_data=before,
            diff_data={
                "name": updated.name,
                "description": updated.description,
                "is_active": updated.is_active,
                "permission_keys": sorted(
                    updated.permissions.values_list("key", flat=True),
                ),
            },
        )

    @transaction.atomic
    def perform_destroy(self, instance):
        from .audit import record_rbac_audit

        record_rbac_audit(
            action_type="permission_group.delete",
            entity_type="permission_group",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            actor_user=self.request.user,
            summary=f"Deleted permission group {instance.name}.",
            before_data={
                "name": instance.name,
                "description": instance.description,
                "is_active": instance.is_active,
                "permission_keys": sorted(
                    instance.permissions.values_list("key", flat=True),
                ),
            },
        )
        instance.delete()


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
            .prefetch_related("additional_branches")
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
    """Return the backend wording a person reads beside the checkbox."""
    return permission.readable_label


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

    ``available`` is read through :func:`vs_rbac.plan_gate.plan_reader`, the
    same function the plan gate itself resolves a capability with. That is the
    point rather than a convenience: the two answered the question separately
    once, the picker asking only whether the school had Finance and the gate
    asking which band of it, so a school on Core was offered a hundred and
    forty-eight keys that refused the first person who used one. Anything this
    marks available, the gate allows. The reverse is not promised - a
    permission may be withheld for reasons that have nothing to do with a plan.

    ``band``, ``depth_label`` and ``unavailable_reason`` exist so a dimmed box
    can say what it is waiting on. A picker that only greys a row leaves the
    administrator to guess whether they misconfigured something or their school
    never bought it.

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

    def get(self, request, *args, **kwargs):
        """Group the tenant's permissions by module.

        A module is offerable when anything in it is. ``school`` holds the
        school's own branches and roles alongside its students, so it is never
        wholly unavailable even to a school that bought neither the students
        nor the teachers module.
        """
        from .plan_gate import plan_reader

        tenant = self.get_tenant()
        read_plan = plan_reader(tenant)

        modules: dict[str, dict] = {}
        for permission in _catalogue_permissions(tenant):
            entry = _permission_entry(permission, read_plan)
            bucket = modules.setdefault(
                permission.module_id,
                {"module": permission.module_id, "available": False, "permissions": []},
            )
            bucket["available"] = bucket["available"] or entry["available"]
            bucket["permissions"].append(entry)

        return success_response(
            message="Data retrieved successfully",
            data=list(modules.values()),
        )


def _catalogue_permissions(tenant):
    """The active permissions *tenant* may hold, with everything an entry reads.

    Filtered on ``PermissionScope.TENANT`` for any tenant that is not the
    platform, the same column the grant guard reads. Both catalogue views read
    this one queryset, so they cannot offer different keys.
    """
    from .models import PermissionScope, tenant_is_platform

    permissions = (
        Permission.objects.filter(is_active=True)
        .select_related(
            "module", "resource", "action", "capability", "capability__parent",
        )
        .order_by("module_id", "resource_id", "action_id")
    )
    if not tenant_is_platform(tenant):
        permissions = permissions.filter(scope=PermissionScope.TENANT)
    return permissions


def _permission_entry(permission, read_plan) -> dict:
    """One permission as a role picker shows it, shared by both catalogues.

    ``is_restricted`` is flagged so the picker can say the grant flows through
    an approval rather than taking effect on save.

    ``capability``, ``band`` and ``depth_label`` say what governs the
    permission and what the school may do with it. A null capability is core
    to every school; a capability with no band is a whole module, sold or not
    sold.

    ``unavailable_reason`` is present only when something is closed, and is
    phrased for the person who can open it rather than for the one who cannot.

    *read_plan* is the function :func:`vs_rbac.plan_gate.plan_reader` returns
    for the tenant, built once per request.
    """
    resource = permission.resource.name if permission.resource_id else ""
    capability, available, reason = read_plan(permission)
    band = capability if capability and capability.parent_id else None
    return {
        "key": permission.key,
        "label": _permission_label(permission),
        "resource": resource,
        "action": permission.action_id,
        "sensitivity": permission.sensitivity_level,
        "is_restricted": permission.is_restricted,
        "capability": capability.key if capability else None,
        "band": band.key if band else None,
        "depth_label": band.get_depth_display() if band else None,
        "available": available,
        "unavailable_reason": reason or None,
    }


def _field_entry(field) -> dict:
    """One registered field as the access catalogue shows it."""
    return {
        "key": field.key,
        "name": field.name,
        "api_names": list(field.api_names or [field.name]),
        "label": field.label,
        "group": field.group,
        "description": field.description,
        "sensitive": field.sensitive,
        "writable": field.writable,
        "default": field.default_access,
    }


# Permissions and fields a tenant may pick from, as one Module, Resource tree.
class TenantAccessCatalogueView(TenantScopedRBACMixin, APIView):
    """GET /rbac/tenants/<slug>/access-catalogue/ - permissions and fields, as a tree.

    The same vocabulary as ``permission-catalogue/``, shaped for a screen that
    walks Module, then Resource, then what sits under the resource: its
    permissions and the fields an administrator may restrict. Each permission
    entry is built by the function the older catalogue uses, so flattening this
    tree gives exactly that catalogue's entries for the same tenant.

    What a tenant is shown follows the grant guard, as it does there: a tenant
    that is not the platform never sees a ``PLATFORM`` permission or a
    ``PLATFORM`` field, and only active fields appear. ``available`` on a
    permission comes from :func:`vs_rbac.plan_gate.plan_reader`. A resource is
    available when any permission in it is, or when it has no permissions at
    all, since a field is never sold separately. A module keeps the older
    catalogue's rule, available when any of its permissions is, and is
    available when it carries fields only.

    Query parameters, all optional:

    ``module``
        A module slug; narrows the tree to that module.
    ``resource``
        A resource slug; narrows to resources with that slug, and to exactly
        one when ``module`` is given too.
    ``search``
        Case-insensitive text matched against permission keys and labels and
        field keys and labels. A resource in which anything matches is
        returned whole, so a match is always shown beside the rest of its
        resource.

    Ordering: modules by slug, resources by display label, permissions with
    ``view`` first and then by action, fields by group, sort order and label.
    A resource left with neither permissions nor fields is omitted, and an
    empty result is ``[]``.

    Cost is flat: one query for permissions, one for fields, plus what the plan
    reader costs, whatever the number of resources.

    A platform operator holding a platform role-view key may assert a school
    tenant, as the console does when choosing fields for a school user's field
    exception. Entries follow the asserted tenant, so a school's catalogue never
    shows a ``PLATFORM`` entry, whoever reads it.

    docstring-name: Access catalogue
    """

    pending_tenant_surface = ("get",)
    platform_cross_tenant_param = True

    def get_permissions(self):
        self.rbac_permission = ROLE_VIEW_KEYS + ["platform.permissions.view"]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get(self, request, *args, **kwargs):
        from .models import PermissionScope, display_label, tenant_is_platform
        from .plan_gate import plan_reader

        tenant = self.get_tenant()
        params = request.query_params
        module_slug = (params.get("module") or "").strip()
        resource_slug = (params.get("resource") or "").strip()
        search = (params.get("search") or "").strip().casefold()

        permissions = _catalogue_permissions(tenant)
        fields = FieldDefinition.objects.filter(is_active=True).select_related(
            "resource", "resource__module",
        )
        if not tenant_is_platform(tenant):
            fields = fields.filter(scope=PermissionScope.TENANT)
        if module_slug:
            permissions = permissions.filter(module_id=module_slug)
            fields = fields.filter(resource__module_id=module_slug)
        if resource_slug:
            permissions = permissions.filter(resource__name=resource_slug)
            fields = fields.filter(resource__name=resource_slug)

        read_plan = plan_reader(tenant)
        buckets: dict[int, dict] = {}

        def bucket_for(module, resource):
            if resource.pk not in buckets:
                buckets[resource.pk] = {
                    "module": module, "resource": resource,
                    "permissions": [], "fields": [], "matched": not search,
                }
            return buckets[resource.pk]

        for permission in permissions:
            entry = _permission_entry(permission, read_plan)
            bucket = bucket_for(permission.module, permission.resource)
            bucket["permissions"].append(entry)
            if search and (
                search in entry["key"].casefold()
                or search in entry["label"].casefold()
            ):
                bucket["matched"] = True

        for field in fields:
            bucket = bucket_for(field.resource.module, field.resource)
            bucket["fields"].append(field)
            if search and (
                search in field.key.casefold() or search in field.label.casefold()
            ):
                bucket["matched"] = True

        modules: dict[str, dict] = {}
        for bucket in buckets.values():
            if not bucket["matched"]:
                continue
            module, resource = bucket["module"], bucket["resource"]
            node = modules.setdefault(module.name, {
                "module": module.name,
                "label": display_label(module.label, module.name),
                "has_permissions": False,
                "available": False,
                "resources": [],
            })
            entries = sorted(
                bucket["permissions"],
                key=lambda entry: (entry["action"] != "view", entry["action"]),
            )
            available = any(entry["available"] for entry in entries)
            if entries:
                node["has_permissions"] = True
                node["available"] = node["available"] or available
            node["resources"].append({
                "resource": resource.name,
                "label": display_label(resource.label, resource.name),
                "available": available if entries else True,
                "permissions": entries,
                "fields": [
                    _field_entry(field)
                    for field in sorted(
                        bucket["fields"],
                        key=lambda f: (f.group.casefold(), f.sort_order, f.label.casefold()),
                    )
                ],
            })

        data = []
        for name in sorted(modules):
            node = modules[name]
            if not node.pop("has_permissions"):
                node["available"] = True
            node["resources"].sort(
                key=lambda row: (row["label"].casefold(), row["resource"]),
            )
            data.append(node)

        return success_response(message="Data retrieved successfully", data=data)


# List the code-owned field registry for platform staff.
class FieldDefinitionListView(generics.ListAPIView):
    """GET /rbac/vision/fields/ - every registered field, active or not.

    The registry is owned by code: apps declare fields in ``field_access.py``
    and ``sync_field_registry`` writes them. So this is read-only, and a write
    verb answers 405.

    Filters: ``module`` and ``resource`` (slugs), ``sensitive`` and
    ``is_active`` (true/false), ``search`` over key, label and description.

    docstring-name: Field registry
    """

    serializer_class = FieldDefinitionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = FieldDefinition.objects.select_related(
            "resource", "resource__module",
        ).order_by("resource__module_id", "resource__name", "group", "sort_order", "label")
        qp = self.request.query_params
        if module := qp.get("module"):
            qs = qs.filter(resource__module_id=module)
        if resource := qp.get("resource"):
            qs = qs.filter(resource__name=resource)
        for flag in ("sensitive", "is_active"):
            lowered = (qp.get(flag) or "").lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(**{flag: True})
            elif lowered in {"false", "0"}:
                qs = qs.filter(**{flag: False})
        if search := qp.get("search"):
            qs = qs.filter(
                Q(key__icontains=search)
                | Q(label__icontains=search)
                | Q(description__icontains=search)
            )
        return qs


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
                "additional_branches",
            )
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        # A school may shape every role it holds, including the ones CodeX set
        # up for it. Those are a starting point, not a settlement: a school that
        # wants its admin configuring a payment gateway should tick the box,
        # not invent a second role to carry the one permission and assign it
        # alongside. What a school may grant AT ALL is already decided by
        # ``PermissionScope.TENANT`` and enforced on the grant models, so
        # refusing the edit on top of that only decided that CodeX's first guess
        # was final.
        #
        # ``is_system_role`` keeps its other jobs - it marks provenance, and the
        # roles screen groups on it - and no longer means "read-only".
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
        if instance.user_assignments.exists():
            return error_response(
                message="This role has assignment history. Take it out of use instead.",
                status=status.HTTP_409_CONFLICT,
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

        from .validators import (
            missing_restricted_grant_authority,
            role_restricted_permission_keys,
        )

        missing = missing_restricted_grant_authority(
            request.user, role_restricted_permission_keys(target_role),
        )
        if missing:
            return error_response(
                message=(
                    "You cannot assign a role carrying restricted permissions "
                    "outside your grant authority."
                ),
                error={"role": [
                    f"Missing grant authority: {', '.join(sorted(missing))}."
                ]},
                status=status.HTTP_403_FORBIDDEN,
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
    # A platform reviewer may bootstrap the first qualified holder inside a
    # tenant. Authentication keeps RBAC evaluation on the reviewer's platform
    # tenant while request.tenant remains the school being reviewed.
    platform_cross_tenant_param = True

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
    platform_cross_tenant_param = True

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

    **The decision belongs to the workflow engine; this is the roles screen's
    door to it.** The request was submitted to an ``rbac.role_change`` ladder
    when it was raised, and this records a vote on that ladder's active stage.
    Eligibility, quorum, the audit trail, delegation and reversal are all the
    engine's, so nothing here re-decides who may approve: a caller who is not on
    the frozen approver list is refused by ``record_action``, whatever role keys
    they hold.

    The endpoint stays rather than sending the screen to the engine's own action
    routes because it is the roles screen's vocabulary - approve or turn down one
    role change - and because those routes are gated on ``workflow.instance.*``,
    which is a different question from who administers roles. The RBAC gate here
    is the coarse one: may this caller decide role changes at all. The engine's
    answer is the precise one: is this caller an approver of THIS request.

    docstring-name: Decide a role change request
    """

    platform_cross_tenant_param = True

    def get_permissions(self):
        self.rbac_permission = ROLE_APPROVE_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def post(self, request, tenant_slug: str, request_id: str):
        from vs_workflow.constants import WorkflowStageAction
        from vs_workflow.models import WorkflowInstance
        from vs_workflow.services.actions import record_action

        tenant = self.tenant
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        if action not in {"APPROVE", "DENY"}:
            return error_response(
                message="Invalid action. Must be APPROVE or DENY.",
                error={"action": ["Must be APPROVE or DENY."]},
            )

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

        # The engine requires a comment to reject. Saying so here puts the
        # message beside the box rather than letting it come back as a 422.
        if action == "DENY" and not notes:
            return error_response(
                message="Denial reason is required.",
                error={"notes": ["Denial reason is required."]},
            )

        instance = (
            WorkflowInstance.objects.for_document(obj).order_by("-created_at").first()
        )
        if instance is None:
            # A request raised before this document type had a ladder, or one
            # whose instance was removed. It cannot be decided here, and saying
            # so plainly beats a 500 from the engine.
            return error_response(
                message=(
                    "This request has no approval to act on. Raise it again so "
                    "it goes through the current approval steps."
                ),
                code="APPROVAL_MISSING",
                status=status.HTTP_409_CONFLICT,
            )

        # Every refusal below is a typed workflow error and renders itself
        # through the global handler with its own code: not an eligible
        # approver, already voted, stage not active, requester cannot approve.
        # Catching them here to rewrite the message would lose the code the
        # screens branch on.
        record_action(
            instance.id,
            request.user,
            WorkflowStageAction.APPROVED if action == "APPROVE"
            else WorkflowStageAction.REJECTED,
            comment=notes,
        )
        obj.refresh_from_db()
        return success_response(
            message=(
                "Role change request approved."
                if obj.status == TenantRoleChangeRequest.Status.APPROVED
                else "Role change request recorded."
                if action == "APPROVE"
                else "Role change request denied."
            ),
            data=TenantRoleChangeRequestSerializer(
                obj, context={"request": request, "tenant": tenant}
            ).data,
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
    "create": "platform.team_overrides.create",
    "delete": "platform.team_overrides.delete",
}
OVERRIDE_SCHOOL_KEYS = {
    "view": "school.user_overrides.view",
    "create": "school.user_overrides.create",
    "delete": "school.user_overrides.delete",
}


def _override_keys(actor) -> dict:
    from vs_tenants.models import Tenant

    is_platform = getattr(getattr(actor, "tenant", None), "kind", None) == Tenant.Kind.PLATFORM
    return OVERRIDE_PLATFORM_KEYS if is_platform else OVERRIDE_SCHOOL_KEYS


class _UserPermissionOverrideBase(TenantScopedRBACMixin):
    """Shared target resolution, self-override ban and audit for override views.

    Self-visibility rule (owner requirement): there is deliberately **no**
    self-service exemption here. Reading your own overrides still requires the
    viewer's ``.view`` key, so a user without it can never learn
    that exceptions exist on their account - they only observe permissions
    working or not working. Nothing about overrides is exposed on ``/me`` or
    any self-service profile serializer.
    """

    # Lets a platform (CX) actor administer a tenant user's overrides by asserting
    # ?tenant=<school-slug>; RBAC still evaluates against the actor's own
    # tenant (request.rbac_tenant), so the platform key is what is required.
    platform_cross_tenant_param = True
    self_refusal_message = "You cannot create or lift permission overrides on yourself."

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
                message=self.self_refusal_message,
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
      ``.view`` key, including for their own id).
    - POST: create an exception. Both modes apply immediately; a new override
      for a key the user already has REPLACES the old row (both audited).

    docstring-name: User permission overrides
    """

    serializer_class = UserPermissionOverrideSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        keys = _override_keys(self._actor())
        if self.request.method == "POST":
            self.rbac_permission = keys["create"]
        else:
            # Managing implies seeing.
            self.rbac_permission = [keys["view"], keys["create"]]
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
        self.rbac_permission = _override_keys(self._actor())["delete"]
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
# Field Access: role switches and one-person field exceptions
# -----------------------------------------------------------------------------
#: The ``state`` filter values on a role's field access, and the test for each.
_FIELD_STATES = {
    "hidden": lambda read, write: not read,
    "read_only": lambda read, write: read and not write,
    "full": lambda read, write: read and write,
}


def _tenant_fields(tenant):
    """The active fields *tenant* may hold, with the tree each entry names.

    Filtered on ``TENANT`` scope for any tenant that is not the platform, the
    same column the switch guard and the evaluator read, so a school is never
    shown, and can never name, a field it could not be granted.
    """
    from .models import PermissionScope, tenant_is_platform

    fields = FieldDefinition.objects.filter(is_active=True).select_related(
        "resource", "resource__module",
    )
    if not tenant_is_platform(tenant):
        fields = fields.filter(scope=PermissionScope.TENANT)
    return fields


def _field_order(field):
    """The access catalogue's order: module, resource label, group, sort order, label."""
    from .models import display_label

    resource = field.resource
    return (
        resource.module_id,
        display_label(resource.label, resource.name).casefold(),
        resource.name,
        field.group.casefold(),
        field.sort_order,
        field.label.casefold(),
    )


def _role_block(role) -> dict:
    return {
        "key": role.key,
        "name": role.name,
        "branch_name": role.branch.name if role.branch_id else None,
    }


def _person_name(user):
    if user is None:
        return None
    return getattr(user, "full_name", None) or getattr(user, "email", None)


def _role_field_entry(field, row) -> dict:
    """One field as a role's Field Access screen shows it.

    ``source`` is ``role`` when the role carries a row for the field and
    ``default`` otherwise. Write is reported as the role confers it, so a row
    stored before its field became non-writable never shows a write switch.
    """
    from rest_framework.fields import DateTimeField

    default = field.default_access
    if row is None:
        read, write, source = default["read"], default["write"], "default"
    else:
        read, write, source = row.can_read, row.can_write and field.writable, "role"
    return {
        "key": field.key,
        "name": field.name,
        "api_names": list(field.api_names or [field.name]),
        "label": field.label,
        "module": field.resource.module_id,
        "resource": field.resource.name,
        "group": field.group,
        "sensitive": field.sensitive,
        "writable": field.writable,
        "read": read,
        "write": write,
        "source": source,
        "default": default,
        "set_by_name": _person_name(row.set_by) if row is not None else None,
        "set_at": DateTimeField().to_representation(row.set_at) if row is not None else None,
    }


def _actor_holds_read(request, tenant, field_key) -> bool:
    """Whether the caller could read *field_key* at the moment of the change.

    Evaluated for the identity the permission gate evaluated (``request.user``)
    in the tenant its authority comes from (``request.rbac_tenant``), so a
    platform operator working on a school is judged by the switches of their
    own roles. The value is recorded on every Field Access audit row: an
    administrator may open a field they cannot read themselves, and this is
    what makes those changes one filter away.
    """
    from .field_evaluator import get_field_access

    rbac_tenant = getattr(request, "rbac_tenant", None) or tenant
    return get_field_access(request.user, tenant=rbac_tenant).can_read(field_key)


# Read or change one role's Read and Write switches on every registered field.
class RoleFieldAccessView(TenantScopedRBACMixin, APIView):
    """GET, PATCH /rbac/tenants/<slug>/roles/<key>/field-access/

    **GET** lists every active field the tenant may hold with the role's
    switches on it. Not paginated: it is a filtered vocabulary, like the access
    catalogue, and in the same order. Query parameters, all optional:
    ``module`` and ``resource`` (slugs), ``search`` (case-insensitive, over the
    field key and label) and ``state`` (``hidden``, ``read_only`` or ``full``).
    Cost is flat: the role, the fields and the role's rows.

    **PATCH** changes switches, atomically::

        {"changes": [
            {"field": "procurement.vendor.phone", "read": true, "write": false},
            {"field": "procurement.vendor.bank_account_number", "reset": true}
        ]}

    Between 1 and 200 changes, each field at most once. ``read`` and ``write``
    are optional; a switch not sent keeps its current value. Write implies
    Read, so ``write: true`` also stores ``read: true`` and ``read: false`` also
    stores ``write: false``. ``reset`` deletes the role's row so the field
    falls back to its default.

    A field that is unknown, inactive, or not holdable by the tenant is refused
    with the same 400, so a school cannot learn which platform fields exist.
    ``write: true`` on a field that is not writable is a 400 naming it.

    A change that leaves the stored (or default) state as it was writes and
    audits nothing. Everything else writes one ``FIELD_ACCESS_CHANGED`` audit
    row per switch that moved, or one ``FIELD_ACCESS_RESET`` row per deleted
    row, inside the same transaction, and bumps ``role.version`` once. The role
    and its existing rows are locked first, so two administrators on the same
    role serialise.

    An administrator may change a field they cannot read, and may change a
    role they hold. Neither is refused; both are audited, the first through
    ``actor_holds_read``.

    The response is the GET shape for the fields named in ``changes``, showing
    what is now stored.

    Reachable before go-live on the same verbs as role detail, because a school
    shapes its roles during onboarding. Not reachable cross-tenant by a
    platform operator, the same as role detail.

    docstring-name: Role field access
    """

    pending_tenant_surface = ("get", "patch")

    def get_permissions(self):
        if self.request.method == "PATCH":
            self.rbac_permission = FIELD_ACCESS_UPDATE_KEYS
        else:
            self.rbac_permission = FIELD_ACCESS_VIEW_KEYS + FIELD_ACCESS_UPDATE_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def _role(self):
        role = (
            TenantRoleTemplate.objects.select_related("branch")
            .filter(tenant=self.tenant, key=self.kwargs.get("key"))
            .first()
        )
        if role is None:
            # Non-enumerating: another tenant's role reads as no role at all.
            raise NotFound("No role matches the requested context.")
        return role

    def get(self, request, *args, **kwargs):
        from .models import RoleFieldAccess

        role = self._role()
        params = request.query_params
        state = (params.get("state") or "").strip().lower()
        if state and state not in _FIELD_STATES:
            return error_response(
                message="Unknown state filter.",
                error={"state": [f"Use one of: {', '.join(_FIELD_STATES)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fields = _tenant_fields(self.tenant)
        if module := (params.get("module") or "").strip():
            fields = fields.filter(resource__module_id=module)
        if resource := (params.get("resource") or "").strip():
            fields = fields.filter(resource__name=resource)
        if search := (params.get("search") or "").strip():
            fields = fields.filter(Q(key__icontains=search) | Q(label__icontains=search))

        rows = {
            row.field_id: row
            for row in RoleFieldAccess.objects.filter(role=role).select_related("set_by")
        }
        entries = [
            _role_field_entry(field, rows.get(field.key))
            for field in sorted(fields, key=_field_order)
        ]
        if state:
            wanted = _FIELD_STATES[state]
            entries = [e for e in entries if wanted(e["read"], e["write"])]

        return success_response(
            message="Data retrieved successfully",
            data={"role": _role_block(role), "fields": entries},
        )

    def patch(self, request, *args, **kwargs):
        from .models import RoleFieldAccess
        from .serializers import RoleFieldAccessPatchSerializer

        role = self._role()
        body = RoleFieldAccessPatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        changes = body.validated_data["changes"]

        requested = [change["field"] for change in changes]
        fields = {
            field.key: field
            for field in _tenant_fields(self.tenant).filter(key__in=requested)
        }
        unknown = sorted(key for key in requested if key not in fields)
        if unknown:
            return error_response(
                message="Some fields cannot be set on this role.",
                error={"changes": [f"Unknown field(s): {', '.join(unknown)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        not_writable = sorted(
            change["field"] for change in changes
            if change.get("write") and not fields[change["field"]].writable
        )
        if not_writable:
            return error_response(
                message="Some fields cannot be written.",
                error={"changes": [
                    f"Not writable, so they have no write switch: {', '.join(not_writable)}."
                ]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = getattr(request, "actor_user", None) or request.user
        now = timezone.now()
        with transaction.atomic():
            locked = (
                TenantRoleTemplate.objects.select_for_update(of=("self",))
                .select_related("tenant", "branch")
                .get(pk=role.pk)
            )
            rows = {
                row.field_id: row
                for row in RoleFieldAccess.objects.select_for_update(of=("self",))
                .select_related("set_by")
                .filter(role=locked, field_id__in=requested)
            }

            to_create, to_update, to_delete, audits = [], [], [], []
            for change in changes:
                field = fields[change["field"]]
                row = rows.get(field.key)
                default = field.default_access
                before = (
                    {"read": row.can_read, "write": row.can_write}
                    if row is not None else dict(default)
                )

                if change.get("reset"):
                    if row is None:
                        continue
                    to_delete.append(row)
                    rows.pop(field.key)
                    audits.append(("FIELD_ACCESS_RESET", None, field, row.pk, before, dict(default)))
                    continue

                read = change.get("read", before["read"])
                write = change.get("write", before["write"])
                if change.get("write") is True:
                    read = True
                if not read or not field.writable:
                    write = False
                after = {"read": read, "write": write}
                if after == before:
                    continue

                if row is None:
                    row = RoleFieldAccess(role=locked, field=field)
                    to_create.append(row)
                    rows[field.key] = row
                else:
                    to_update.append(row)
                row.can_read, row.can_write = read, write
                row.set_by, row.set_at = actor, now
                for switch in ("read", "write"):
                    if before[switch] != after[switch]:
                        audits.append(("FIELD_ACCESS_CHANGED", switch, field, row, before, after))

            if to_delete:
                RoleFieldAccess.objects.filter(pk__in=[row.pk for row in to_delete]).delete()
            if to_create:
                RoleFieldAccess.objects.bulk_create(to_create)
            if to_update:
                for row in to_update:
                    row.assert_scope_allowed()
                    row.updated_at = now
                RoleFieldAccess.objects.bulk_update(
                    to_update, ["can_read", "can_write", "set_by", "set_at", "updated_at"],
                )

            for action_type, switch, field, row_or_pk, before, after in audits:
                self._audit(
                    action_type=action_type,
                    switch=switch,
                    role=locked,
                    field=field,
                    entity_id=getattr(row_or_pk, "pk", row_or_pk),
                    before=before,
                    after=after,
                    actor=actor,
                    holds_read=_actor_holds_read(request, self.tenant, field.key),
                )

            if audits:
                locked.version = (locked.version or 1) + 1
                locked.save(update_fields=["version", "updated_at"])

        entries = [
            _role_field_entry(field, rows.get(field.key))
            for field in sorted(fields.values(), key=_field_order)
        ]
        return success_response(
            message="Field access updated.",
            data={"role": _role_block(locked), "fields": entries},
        )

    def _audit(self, *, action_type, switch, role, field, entity_id, before, after,
               actor, holds_read):
        """One durable audit row for one switch moved, or one row reset.

        A change that opens a sensitive field (a switch moving from off to on)
        is a WARNING; every other change, and every reset, is INFO.
        """
        from .audit import record_rbac_audit

        opened = switch is not None and not before[switch] and after[switch]
        if switch is None:
            summary = f"Field '{field.key}' reset to its default on role '{role.name}'"
        else:
            summary = (
                f"{switch.capitalize()} on field '{field.key}' turned "
                f"{'on' if after[switch] else 'off'} for role '{role.name}'"
            )
        record_rbac_audit(
            action_type=action_type,
            entity_type="RoleFieldAccess",
            entity_id=str(entity_id),
            entity_label=f"{role.key}:{field.key}"[:255],
            actor_user=actor,
            severity="WARNING" if (opened and field.sensitive) else "INFO",
            summary=summary,
            before_data=before,
            diff_data=after,
            metadata={
                "tenant_id": str(self.tenant.pk),
                "school_id": self.tenant.slug,
                "role_key": role.key,
                "field_key": field.key,
                "switch": switch,
                "sensitive": field.sensitive,
                "actor_holds_read": holds_read,
            },
        )


class _UserFieldAccessOverrideBase(_UserPermissionOverrideBase):
    """Target resolution, self ban and audit for field access exceptions.

    Inherits the permission override rules unchanged: the viewer's key decides
    who may read a person's exceptions (there is no self-service exemption),
    nobody may create or lift an exception on themselves under either identity
    of a proxy session, a user outside the tenant reads as no user, and a
    platform operator may act on a school user by asserting its tenant.

    Every audit row records the target's effective state on the field before
    and after, and whether the caller could read the field themselves.
    """

    self_refusal_message = "You cannot create or lift field access exceptions on yourself."

    def _effective_state(self, target, field_key) -> dict:
        """The target's current Read and Write on *field_key*, freshly evaluated."""
        from .field_evaluator import get_field_access

        target.__dict__.pop("_rbac_field_access", None)
        return get_field_access(target, tenant=self.tenant).state(field_key)

    def _audit_exception(self, *, action_type, override, entity_id, target, summary,
                         before, after, holds_read, replaced=False):
        from .audit import record_rbac_audit

        record_rbac_audit(
            action_type=action_type,
            entity_type="UserFieldAccessOverride",
            entity_id=str(entity_id),
            entity_label=(
                f"{getattr(target, 'email', target.pk)}:{override.field_id}:{override.access}"
            )[:255],
            actor_user=self._actor(),
            severity="WARNING",
            summary=summary,
            before_data=before,
            diff_data=after,
            metadata={
                "school_id": self.tenant.slug,
                "tenant_id": str(self.tenant.pk),
                "target_user_id": str(target.pk),
                "target_user_email": getattr(target, "email", ""),
                "field_key": override.field_id,
                "sensitive": override.field.sensitive,
                "access": override.access,
                "mode": override.mode,
                "reason": override.reason,
                "expires_at": override.expires_at.isoformat() if override.expires_at else None,
                "replaced": replaced,
                "actor_holds_read": holds_read,
            },
        )


# List a user's field access exceptions, or create one.
class UserFieldAccessOverrideListCreateView(
    _UserFieldAccessOverrideBase, generics.ListCreateAPIView,
):
    """
    Tenant-facing:
    - GET: list the field access exceptions on one user, paginated, newest
      first. Filters: ``mode`` (ALLOW or DENY) and ``access`` (READ or WRITE).
      Each row carries ``role_state``, what the user's roles alone say about
      the field. The viewer needs the ``.view`` override key,
      including for their own id.
    - POST: create an exception::

        {"field": "procurement.vendor.bank_account_number", "access": "READ",
         "mode": "ALLOW", "reason": "Covering bursar duties 14-28 Sept",
         "expires_at": "2026-09-28T23:59:00Z"}

      It applies on the user's next request. A new exception on the same field
      and access REPLACES the old row, and both halves are audited. A field
      the tenant may not hold reads as a field that does not exist, and
      ``ALLOW WRITE`` on a field that is not writable is refused.

    docstring-name: User field access exceptions
    """

    serializer_class = UserFieldAccessOverrideSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        keys = _override_keys(self._actor())
        if self.request.method == "POST":
            self.rbac_permission = keys["create"]
        else:
            self.rbac_permission = [keys["view"], keys["create"]]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def _get_target(self):
        target = getattr(self, "_target", None) or self.get_target_user()
        self._target = target
        return target

    def get_queryset(self):
        qs = (
            UserFieldAccessOverride.objects
            .filter(tenant=self.tenant, user=self._get_target())
            .select_related("field", "created_by")
            .order_by("-created_at")
        )
        if mode := self.request.query_params.get("mode"):
            qs = qs.filter(mode=mode.upper())
        if access := self.request.query_params.get("access"):
            qs = qs.filter(access=access.upper())
        return qs

    def get_serializer_context(self):
        from .field_evaluator import get_role_field_access

        context = super().get_serializer_context()
        if getattr(self, "_role_state", None) is None:
            self._role_state = get_role_field_access(self._get_target(), tenant=self.tenant)
        context["role_field_state"] = self._role_state
        context["tenant"] = self.tenant
        return context

    def create(self, request, *args, **kwargs):
        target = self._get_target()
        if (denied := self._reject_self(target)) is not None:
            return denied

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        field = serializer.validated_data["field"]
        access = serializer.validated_data["access"]
        email = getattr(target, "email", target.pk)
        holds_read = _actor_holds_read(request, self.tenant, field.key)

        with transaction.atomic():
            existing = (
                UserFieldAccessOverride.objects
                .select_for_update(of=("self",))
                .select_related("field")
                .filter(user=target, field=field, access=access)
                .first()
            )
            before = self._effective_state(target, field.key)
            if existing is not None:
                existing_pk = existing.pk
                existing.delete()
                lifted = self._effective_state(target, field.key)
                self._audit_exception(
                    action_type="FIELD_OVERRIDE_LIFTED",
                    override=existing,
                    entity_id=existing_pk,
                    target=target,
                    summary=(
                        f"{existing.mode} {existing.access} exception on '{existing.field_id}' "
                        f"for {email} replaced by a new exception"
                    ),
                    before=before,
                    after=lifted,
                    holds_read=holds_read,
                    replaced=True,
                )
                before = lifted

            override = serializer.save(
                tenant=self.tenant, user=target, created_by=self._actor(),
            )
            self._audit_exception(
                action_type="FIELD_OVERRIDE_CREATED",
                override=override,
                entity_id=override.pk,
                target=target,
                summary=(
                    f"{override.mode} {override.access} exception on '{override.field_id}' "
                    f"created for {email}"
                ),
                before=before,
                after=self._effective_state(target, field.key),
                holds_read=holds_read,
                replaced=existing is not None,
            )

        return success_response(
            message="Field access exception applied.",
            data=self.get_serializer(override).data,
            status=status.HTTP_201_CREATED,
        )


# Lift (delete) one field access exception.
class UserFieldAccessOverrideDetailView(_UserFieldAccessOverrideBase, APIView):
    """
    Tenant-facing:
    - DELETE: lift a field access exception. A lifted DENY gives the user back
      what their roles allow; a lifted ALLOW removes the extra access. Effective
      on the user's next request, and audited.

    docstring-name: User field access exceptions
    """

    def get_permissions(self):
        self.rbac_permission = _override_keys(self._actor())["delete"]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def delete(self, request, tenant_slug: str, user_id: int, id: int):
        target = self.get_target_user()
        if (denied := self._reject_self(target)) is not None:
            return denied

        override = (
            UserFieldAccessOverride.objects
            .select_related("field", "created_by")
            .filter(pk=id, tenant=self.tenant, user=target)
            .first()
        )
        if override is None:
            return error_response(
                message="Field access exception not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        holds_read = _actor_holds_read(request, self.tenant, override.field_id)
        with transaction.atomic():
            before = self._effective_state(target, override.field_id)
            override_pk = override.pk
            override.delete()
            self._audit_exception(
                action_type="FIELD_OVERRIDE_LIFTED",
                override=override,
                entity_id=override_pk,
                target=target,
                summary=(
                    f"{override.mode} {override.access} exception on '{override.field_id}' "
                    f"lifted for {getattr(target, 'email', target.pk)}"
                ),
                before=before,
                after=self._effective_state(target, override.field_id),
                holds_read=holds_read,
            )

        return success_response(message="Field access exception lifted.")


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
