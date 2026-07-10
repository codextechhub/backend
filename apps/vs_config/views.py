from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .constants import ConfigPermissions
from .models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
    ConfigurationAuditEvent,
    ConfigurationDefinition,
    ConfigurationValue,
)
from .serializers import (
    CapabilityEntitlementSerializer,
    CapabilityOverrideSerializer,
    CapabilitySerializer,
    ConfigurationAuditEventSerializer,
    ConfigurationDefinitionSerializer,
    ConfigurationValueSerializer,
    SetConfigurationValueSerializer,
    SetEntitlementSerializer,
    SetOverrideSerializer,
)
from .services.audit import record_configuration_event
from .services.capabilities import effective_capability, set_entitlement, set_override
from .services.resolution import resolve_value, set_value
from .services.scopes import resolve_request_scope


# Base API view that binds RBAC permissions and platform-only method guards.
class ConfigAPIView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    permission_map = {}
    platform_methods = set()

    # Resolve the RBAC permission key for the current HTTP method.
    @property
    def rbac_permission(self):
        method = getattr(self.request, "method", "GET")
        if method in {"HEAD", "OPTIONS"}:
            # Treat metadata probes as read checks so RBAC does not need duplicate keys.
            method = "GET"
        permission = self.permission_map.get(method)
        if not permission:
            raise MethodNotAllowed(method)
        return permission

    # Apply the standard config page size across catalogue endpoints.
    def paginate(self, request, queryset, serializer_class):
        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(serializer_class(page, many=True).data)

    # Enforce operations that must remain platform-owned.
    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method in self.platform_methods and request.user.user_type != "CX_STAFF":
            raise PermissionDenied("This operation is platform-scoped.")


# List active configuration definitions and allow platform staff to create new keys.
class DefinitionListCreateView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.DEFINITION_VIEW,
        "POST": ConfigPermissions.DEFINITION_CREATE,
    }

    # Return the configuration catalogue, hiding archived definitions by default.
    def get(self, request):
        qs = ConfigurationDefinition.objects.all()
        if request.query_params.get("include_inactive") != "true":
            # Archived definitions stay queryable only for explicit administrative review.
            qs = qs.filter(is_active=True)
        return self.paginate(request, qs, ConfigurationDefinitionSerializer)

    # Create a new definition and record the catalogue change for audit review.
    def post(self, request):
        serializer = ConfigurationDefinitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        definition = serializer.save(created_by=request.user)
        record_configuration_event(
            action="config.definition.created", target=definition, actor=request.user,
            after=ConfigurationDefinitionSerializer(definition).data,
        )
        return success_response(
            "Configuration definition created.",
            ConfigurationDefinitionSerializer(definition).data,
            status=status.HTTP_201_CREATED,
        )


# Retrieve, update, or archive one configuration definition by key.
class DefinitionDetailView(ConfigAPIView):
    platform_methods = {"PATCH", "DELETE"}
    permission_map = {
        "GET": ConfigPermissions.DEFINITION_VIEW,
        "PATCH": ConfigPermissions.DEFINITION_UPDATE,
        "DELETE": ConfigPermissions.DEFINITION_ARCHIVE,
    }

    # Resolve definitions by stable key rather than exposing database IDs.
    def get_object(self, key):
        return get_object_or_404(ConfigurationDefinition, key=key)

    # Return the definition metadata used by administrators.
    def get(self, request, key):
        obj = self.get_object(key)
        return success_response(
            "Configuration definition retrieved.",
            ConfigurationDefinitionSerializer(obj).data,
        )

    # Update definition metadata atomically with its audit event.
    @transaction.atomic
    def patch(self, request, key):
        obj = self.get_object(key)
        before = ConfigurationDefinitionSerializer(obj).data
        # Capture the serialized before-state so audits reflect admin-visible metadata.
        serializer = ConfigurationDefinitionSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_configuration_event(
            action="config.definition.updated", target=obj, actor=request.user,
            before=before, after=serializer.data, reason=request.data.get("reason", ""),
        )
        return success_response("Configuration definition updated.", serializer.data)

    # Soft-archive definitions so existing values and audit history remain readable.
    @transaction.atomic
    def delete(self, request, key):
        obj = self.get_object(key)
        if obj.is_active:
            # Archive instead of delete because values may still reference this key historically.
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])
            record_configuration_event(
                action="config.definition.archived", target=obj, actor=request.user,
                before={"is_active": True}, after={"is_active": False},
                reason=request.data.get("reason", ""),
            )
        return success_response("Configuration definition archived.")


# List and write scoped configuration values for the resolved tenant context.
class ValueListSetView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.VALUE_VIEW,
        "POST": ConfigPermissions.VALUE_UPDATE,
    }

    # Return only values stored at the caller's resolved scope.
    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = ConfigurationValue.all_objects.select_related("definition", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            # School scope excludes branch rows; effective-value endpoints handle inheritance.
            qs = qs.filter(school=school, branch__isnull=True)
        else:
            qs = qs.filter(school__isnull=True, branch__isnull=True)
        return self.paginate(request, qs.order_by("definition__key"), ConfigurationValueSerializer)

    # Save one or many values under one resolved scope and transaction.
    @transaction.atomic
    def post(self, request):
        raw_items = request.data.get("values")
        is_bulk = isinstance(raw_items, list)
        # Single and bulk writes share the same validation and transaction path.
        items = raw_items if is_bulk else [request.data]
        if not items:
            raise ValidationError({"values": "At least one configuration value is required."})
        serializers = [SetConfigurationValueSerializer(data=item) for item in items]
        for serializer in serializers:
            serializer.is_valid(raise_exception=True)
        school, branch = resolve_request_scope(request)
        rows = []
        # All submitted keys are written to the same resolved tenant scope.
        for serializer in serializers:
            definition = get_object_or_404(
                ConfigurationDefinition,
                key=serializer.validated_data["key"], is_active=True,
            )
            rows.append(set_value(
                definition=definition,
                value=serializer.validated_data["value"],
                actor=request.user,
                school=school,
                branch=branch,
                reason=serializer.validated_data["reason"],
            ))
        data = ConfigurationValueSerializer(rows, many=True).data
        return success_response(
            "Configuration values saved." if is_bulk else "Configuration value saved.",
            data if is_bulk else data[0],
            status=status.HTTP_201_CREATED,
        )


# Report resolved configuration values after inheritance and secret redaction.
class EffectiveValueView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.VALUE_VIEW}

    # Compute effective values instead of returning only physically stored rows.
    def get(self, request, key=None):
        school, branch = resolve_request_scope(request)
        definitions = ConfigurationDefinition.objects.filter(is_active=True)
        if key:
            definitions = definitions.filter(key=key)
            if not definitions.exists():
                raise NotFound("Configuration definition not found.")
        data = []
        for definition in definitions:
            value, source = resolve_value(definition, school=school, branch=branch)
            if definition.sensitivity == definition.Sensitivity.SECRET_REFERENCE:
                # Effective reads should reveal that a secret exists, never the reference value.
                value = "[REDACTED]" if value is not None else None
            data.append({
                "key": definition.key,
                "value": value,
                "source": source.scope_key if source else "default",
            })
        payload = data[0] if key else data
        return success_response("Effective configuration retrieved.", payload)


# List capability gates and allow platform staff to create new capability keys.
class CapabilityListCreateView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.CAPABILITY_VIEW,
        "POST": ConfigPermissions.CAPABILITY_MANAGE,
    }

    # Return the capability catalogue, hiding archived gates by default.
    def get(self, request):
        qs = Capability.objects.all()
        if request.query_params.get("include_inactive") != "true":
            # Inactive gates stay hidden unless the catalogue maintainer asks for them.
            qs = qs.filter(is_active=True)
        return self.paginate(request, qs, CapabilitySerializer)

    # Create a capability definition and audit the catalogue addition.
    @transaction.atomic
    def post(self, request):
        serializer = CapabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capability = serializer.save()
        record_configuration_event(
            action="config.capability.created", target=capability, actor=request.user,
            after=CapabilitySerializer(capability).data,
        )
        return success_response(
            "Capability created.", CapabilitySerializer(capability).data,
            status=status.HTTP_201_CREATED,
        )


# Retrieve, update, or archive one capability definition by key.
class CapabilityDetailView(ConfigAPIView):
    platform_methods = {"PATCH", "DELETE"}
    permission_map = {
        "GET": ConfigPermissions.CAPABILITY_VIEW,
        "PATCH": ConfigPermissions.CAPABILITY_MANAGE,
        "DELETE": ConfigPermissions.CAPABILITY_MANAGE,
    }

    # Resolve capabilities by stable key for public admin URLs.
    def get_object(self, key):
        return get_object_or_404(Capability, key=key)

    # Return capability metadata and dependency configuration.
    def get(self, request, key):
        obj = self.get_object(key)
        return success_response("Capability retrieved.", CapabilitySerializer(obj).data)

    # Update capability metadata atomically with the audit event.
    @transaction.atomic
    def patch(self, request, key):
        obj = self.get_object(key)
        before = CapabilitySerializer(obj).data
        # Dependency and default changes are audited from the serialized admin shape.
        serializer = CapabilitySerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_configuration_event(
            action="config.capability.updated", target=obj, actor=request.user,
            before=before, after=serializer.data, reason=request.data.get("reason", ""),
        )
        return success_response("Capability updated.", serializer.data)

    # Soft-archive capabilities so existing audit and override rows remain coherent.
    @transaction.atomic
    def delete(self, request, key):
        obj = self.get_object(key)
        if obj.is_active:
            # Archive instead of delete because overrides and audit rows depend on the key.
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])
            record_configuration_event(
                action="config.capability.archived", target=obj, actor=request.user,
                before={"is_active": True}, after={"is_active": False},
                reason=request.data.get("reason", ""),
            )
        return success_response("Capability archived.")


# Manage platform or school-level capability entitlements.
class EntitlementListSetView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.ENTITLEMENT_VIEW,
        "POST": ConfigPermissions.ENTITLEMENT_MANAGE,
    }

    # List entitlements at the resolved platform or school scope.
    def get(self, request):
        school, _ = resolve_request_scope(request)
        qs = CapabilityEntitlement.all_objects.select_related("capability", "updated_by")
        # Entitlements never live at branch scope, so discard the branch half of resolution.
        qs = qs.filter(school=school) if school else qs.filter(school__isnull=True)
        return self.paginate(request, qs, CapabilityEntitlementSerializer)

    # Grant or revoke entitlement before scoped overrides can enable the feature.
    def post(self, request):
        serializer = SetEntitlementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capability = get_object_or_404(Capability, key=serializer.validated_data["capability"])
        school, _ = resolve_request_scope(request)
        row = set_entitlement(
            capability=capability, school=school,
            state=serializer.validated_data["state"],
            source=serializer.validated_data["source"], actor=request.user,
            reason=serializer.validated_data["reason"],
        )
        return success_response(
            "Capability entitlement saved.", CapabilityEntitlementSerializer(row).data,
            status=status.HTTP_201_CREATED,
        )


# Manage capability overrides at branch, school, or platform scope.
class OverrideListSetView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.OVERRIDE_VIEW,
        "POST": ConfigPermissions.OVERRIDE_MANAGE,
    }

    # List overrides that are physically stored at the resolved scope.
    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = CapabilityOverride.all_objects.select_related("capability", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            # Listing physical overrides does not include inherited platform rows.
            qs = qs.filter(school=school, branch__isnull=True)
        else:
            qs = qs.filter(school__isnull=True, branch__isnull=True)
        return self.paginate(request, qs, CapabilityOverrideSerializer)

    # Write an override after the service enforces entitlement constraints.
    def post(self, request):
        serializer = SetOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capability = get_object_or_404(Capability, key=serializer.validated_data["capability"])
        school, branch = resolve_request_scope(request)
        row = set_override(
            capability=capability, state=serializer.validated_data["state"],
            actor=request.user, school=school, branch=branch,
            reason=serializer.validated_data["reason"],
        )
        return success_response(
            "Capability override saved.", CapabilityOverrideSerializer(row).data,
            status=status.HTTP_201_CREATED,
        )


# Report final capability states after entitlement, dependencies, and overrides.
class EffectiveCapabilitiesView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.CAPABILITY_VIEW}

    # Evaluate each active capability for the caller's resolved scope.
    def get(self, request):
        school, branch = resolve_request_scope(request)
        data = [
            {"key": item.key, "enabled": effective_capability(item, school=school, branch=branch)}
            for item in Capability.objects.filter(is_active=True)
        ]
        return success_response("Effective capabilities retrieved.", data)


# Expose immutable configuration audit events within the caller's tenant scope.
class AuditEventListView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_VIEW}

    # Restrict audit history to the branch or school selected by the caller.
    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = ConfigurationAuditEvent.all_objects.select_related("actor")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            # School audit includes branch-tagged rows so tenant admins see all config changes.
            qs = qs.filter(school=school)
        else:
            qs = qs.filter(school__isnull=True)
        return self.paginate(request, qs, ConfigurationAuditEventSerializer)


# Export effective configuration and capability state for the resolved scope.
class ConfigExportView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.EXPORT_CREATE}

    # Build a redacted snapshot suitable for support and tenant diagnostics.
    def get(self, request):
        school, branch = resolve_request_scope(request)
        definitions = ConfigurationDefinition.objects.filter(is_active=True)
        values = []
        for definition in definitions:
            value, source = resolve_value(definition, school=school, branch=branch)
            if definition.sensitivity == definition.Sensitivity.SECRET_REFERENCE:
                # Export follows the same redaction rule as effective reads.
                value = "[REDACTED]" if value is not None else None
            values.append({"key": definition.key, "value": value, "source": source.scope_key if source else "default"})
        capabilities = [
            {"key": item.key, "enabled": effective_capability(item, school=school, branch=branch)}
            for item in Capability.objects.filter(is_active=True)
        ]
        return success_response(
            "Configuration export generated.",
            {"values": values, "capabilities": capabilities},
        )
