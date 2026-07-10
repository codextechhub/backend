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


class ConfigAPIView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    permission_map = {}
    platform_methods = set()

    @property
    def rbac_permission(self):
        method = getattr(self.request, "method", "GET")
        if method in {"HEAD", "OPTIONS"}:
            method = "GET"
        permission = self.permission_map.get(method)
        if not permission:
            raise MethodNotAllowed(method)
        return permission

    def paginate(self, request, queryset, serializer_class):
        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(serializer_class(page, many=True).data)

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method in self.platform_methods and request.user.user_type != "CX_STAFF":
            raise PermissionDenied("This operation is platform-scoped.")


class DefinitionListCreateView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.DEFINITION_VIEW,
        "POST": ConfigPermissions.DEFINITION_CREATE,
    }

    def get(self, request):
        qs = ConfigurationDefinition.objects.all()
        if request.query_params.get("include_inactive") != "true":
            qs = qs.filter(is_active=True)
        return self.paginate(request, qs, ConfigurationDefinitionSerializer)

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


class DefinitionDetailView(ConfigAPIView):
    platform_methods = {"PATCH", "DELETE"}
    permission_map = {
        "GET": ConfigPermissions.DEFINITION_VIEW,
        "PATCH": ConfigPermissions.DEFINITION_UPDATE,
        "DELETE": ConfigPermissions.DEFINITION_ARCHIVE,
    }

    def get_object(self, key):
        return get_object_or_404(ConfigurationDefinition, key=key)

    def get(self, request, key):
        obj = self.get_object(key)
        return success_response(
            "Configuration definition retrieved.",
            ConfigurationDefinitionSerializer(obj).data,
        )

    @transaction.atomic
    def patch(self, request, key):
        obj = self.get_object(key)
        before = ConfigurationDefinitionSerializer(obj).data
        serializer = ConfigurationDefinitionSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_configuration_event(
            action="config.definition.updated", target=obj, actor=request.user,
            before=before, after=serializer.data, reason=request.data.get("reason", ""),
        )
        return success_response("Configuration definition updated.", serializer.data)

    @transaction.atomic
    def delete(self, request, key):
        obj = self.get_object(key)
        if obj.is_active:
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])
            record_configuration_event(
                action="config.definition.archived", target=obj, actor=request.user,
                before={"is_active": True}, after={"is_active": False},
                reason=request.data.get("reason", ""),
            )
        return success_response("Configuration definition archived.")


class ValueListSetView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.VALUE_VIEW,
        "POST": ConfigPermissions.VALUE_UPDATE,
    }

    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = ConfigurationValue.all_objects.select_related("definition", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            qs = qs.filter(school=school, branch__isnull=True)
        else:
            qs = qs.filter(school__isnull=True, branch__isnull=True)
        return self.paginate(request, qs.order_by("definition__key"), ConfigurationValueSerializer)

    @transaction.atomic
    def post(self, request):
        raw_items = request.data.get("values")
        is_bulk = isinstance(raw_items, list)
        items = raw_items if is_bulk else [request.data]
        if not items:
            raise ValidationError({"values": "At least one configuration value is required."})
        serializers = [SetConfigurationValueSerializer(data=item) for item in items]
        for serializer in serializers:
            serializer.is_valid(raise_exception=True)
        school, branch = resolve_request_scope(request)
        rows = []
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


class EffectiveValueView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.VALUE_VIEW}

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
                value = "[REDACTED]" if value is not None else None
            data.append({
                "key": definition.key,
                "value": value,
                "source": source.scope_key if source else "default",
            })
        payload = data[0] if key else data
        return success_response("Effective configuration retrieved.", payload)


class CapabilityListCreateView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.CAPABILITY_VIEW,
        "POST": ConfigPermissions.CAPABILITY_MANAGE,
    }

    def get(self, request):
        qs = Capability.objects.all()
        if request.query_params.get("include_inactive") != "true":
            qs = qs.filter(is_active=True)
        return self.paginate(request, qs, CapabilitySerializer)

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


class CapabilityDetailView(ConfigAPIView):
    platform_methods = {"PATCH", "DELETE"}
    permission_map = {
        "GET": ConfigPermissions.CAPABILITY_VIEW,
        "PATCH": ConfigPermissions.CAPABILITY_MANAGE,
        "DELETE": ConfigPermissions.CAPABILITY_MANAGE,
    }

    def get_object(self, key):
        return get_object_or_404(Capability, key=key)

    def get(self, request, key):
        obj = self.get_object(key)
        return success_response("Capability retrieved.", CapabilitySerializer(obj).data)

    @transaction.atomic
    def patch(self, request, key):
        obj = self.get_object(key)
        before = CapabilitySerializer(obj).data
        serializer = CapabilitySerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        record_configuration_event(
            action="config.capability.updated", target=obj, actor=request.user,
            before=before, after=serializer.data, reason=request.data.get("reason", ""),
        )
        return success_response("Capability updated.", serializer.data)

    @transaction.atomic
    def delete(self, request, key):
        obj = self.get_object(key)
        if obj.is_active:
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])
            record_configuration_event(
                action="config.capability.archived", target=obj, actor=request.user,
                before={"is_active": True}, after={"is_active": False},
                reason=request.data.get("reason", ""),
            )
        return success_response("Capability archived.")


class EntitlementListSetView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {
        "GET": ConfigPermissions.ENTITLEMENT_VIEW,
        "POST": ConfigPermissions.ENTITLEMENT_MANAGE,
    }

    def get(self, request):
        school, _ = resolve_request_scope(request)
        qs = CapabilityEntitlement.all_objects.select_related("capability", "updated_by")
        qs = qs.filter(school=school) if school else qs.filter(school__isnull=True)
        return self.paginate(request, qs, CapabilityEntitlementSerializer)

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


class OverrideListSetView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.OVERRIDE_VIEW,
        "POST": ConfigPermissions.OVERRIDE_MANAGE,
    }

    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = CapabilityOverride.all_objects.select_related("capability", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            qs = qs.filter(school=school, branch__isnull=True)
        else:
            qs = qs.filter(school__isnull=True, branch__isnull=True)
        return self.paginate(request, qs, CapabilityOverrideSerializer)

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


class EffectiveCapabilitiesView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.CAPABILITY_VIEW}

    def get(self, request):
        school, branch = resolve_request_scope(request)
        data = [
            {"key": item.key, "enabled": effective_capability(item, school=school, branch=branch)}
            for item in Capability.objects.filter(is_active=True)
        ]
        return success_response("Effective capabilities retrieved.", data)


class AuditEventListView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_VIEW}

    def get(self, request):
        school, branch = resolve_request_scope(request)
        qs = ConfigurationAuditEvent.all_objects.select_related("actor")
        if branch:
            qs = qs.filter(branch=branch)
        elif school:
            qs = qs.filter(school=school)
        else:
            qs = qs.filter(school__isnull=True)
        return self.paginate(request, qs, ConfigurationAuditEventSerializer)


class ConfigExportView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.EXPORT_CREATE}

    def get(self, request):
        school, branch = resolve_request_scope(request)
        definitions = ConfigurationDefinition.objects.filter(is_active=True)
        values = []
        for definition in definitions:
            value, source = resolve_value(definition, school=school, branch=branch)
            if definition.sensitivity == definition.Sensitivity.SECRET_REFERENCE:
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
