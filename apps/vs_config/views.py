import csv
import json
import logging
from datetime import timedelta
from math import ceil
from uuid import UUID

from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import error_response, success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .constants import ConfigPermissions
from .models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
    ConfigurationAuditEvent,
    ConfigurationAuditExportJob,
    ConfigurationAuditSavedView,
    ConfigurationDefinition,
    ConfigurationValue,
)
from .serializers import (
    CapabilityEntitlementSerializer,
    CapabilityOverrideSerializer,
    CapabilitySerializer,
    ConfigurationAuditEventSerializer,
    ConfigurationAuditExportCreateSerializer,
    ConfigurationAuditExportJobSerializer,
    ConfigurationAuditFilterSerializer,
    ConfigurationAuditSavedViewSerializer,
    ConfigurationAuditSavedViewWriteSerializer,
    build_configuration_target_labels,
    ConfigurationDefinitionSerializer,
    ConfigurationValueSerializer,
    ClearConfigurationValueSerializer,
    BulkSetEntitlementSerializer,
    IntegrationSettingsUpdateSerializer,
    IntegrationConnectionTestSerializer,
    PlatformSettingsUpdateSerializer,
    SecuritySettingsUpdateSerializer,
    SetConfigurationValueSerializer,
    SetEntitlementSerializer,
    SetOverrideSerializer,
)
from .services.audit import record_configuration_event
from .services.capabilities import (
    bulk_effective_capabilities,
    bulk_schedule_entitlements,
    clear_entitlement,
    entitlement_resolution,
    set_entitlement,
    set_override,
    UNSET,
)
from .services.audit_exports import (
    apply_configuration_audit_filters,
    audit_filter_snapshot,
    create_configuration_audit_export,
    scoped_configuration_audit_queryset,
)
from .services.connections import (
    ConnectionTestCoolingDown,
    IntegrationConnection,
    test_email_connection,
    test_payment_connection,
)
from .services.resolution import clear_value, resolve_value, set_value
from .services.scopes import resolve_request_scope
from .platform_settings import ALL_FIELDS, ONBOARDING_FIELDS, PROFILE_FIELDS, resolve_platform_settings
from .runtime_settings import (
    INTEGRATION_FIELDS,
    PRODUCT_OWNED_KEYS,
    SECURITY_FIELDS,
    SPECIAL_MANAGED_KEYS,
    resolve_integration_settings,
    resolve_security_settings,
    validate_security_compliance,
)
from schools.vs_schools.models import Currency, OwnershipType, TermStructure
from vs_tenants.models import Tenant


logger = logging.getLogger(__name__)


# Base API view that binds RBAC permissions and platform-only method guards.
class ConfigAPIView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    permission_map = {}
    platform_methods = set()
    # Configuration defaults to the caller's home tenant when no explicit
    # scope is selected. Platform staff can therefore open Platform Settings
    # before a Finance entity or school context has been chosen.
    tenant_param_required = False
    # Platform (Codex) staff may assert a business tenant via ?tenant= to read
    # or configure that tenant's scope (e.g. grant a school entitlement); the
    # auth layer still bars non-platform callers from asserting a foreign tenant.
    platform_cross_tenant_param = True

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
        context = {}
        if serializer_class is ConfigurationAuditEventSerializer:
            context["_target_labels"] = build_configuration_target_labels(page)
        return paginator.get_paginated_response(
            serializer_class(page, many=True, context=context).data
        )

    # Enforce operations that must remain platform-owned.
    def check_permissions(self, request):
        super().check_permissions(request)
        # Platform-ness is a property of the actor's home tenant, not the tenant
        # currently being targeted (mirrors vs_rbac.permissions.IsVisionStaff):
        # a Codex staffer keeps platform rights while asserting a school tenant.
        is_platform = getattr(getattr(request.user, "tenant", None), "kind", None) == "PLATFORM"
        if request.method in self.platform_methods and not is_platform:
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
        protected_fields = {
            "key", "value_type", "default_value", "validation_rules",
            "allowed_scopes", "sensitivity", "is_active",
        }
        if key in set(ALL_FIELDS.values()) | PRODUCT_OWNED_KEYS and protected_fields.intersection(request.data):
            raise ValidationError({
                "definition": (
                    "This curated setting has a product-owned schema. "
                    "Edit its value from the matching Platform Settings section."
                )
            })
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
        if key in set(ALL_FIELDS.values()) | PRODUCT_OWNED_KEYS:
            raise ValidationError({
                "definition": "Curated Platform Settings definitions cannot be archived."
            })
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
        tenant, branch = resolve_request_scope(request)
        qs = ConfigurationValue.all_objects.select_related("definition", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif tenant:
            # Tenant scope excludes branch rows; effective-value endpoints handle inheritance.
            qs = qs.filter(tenant=tenant, branch__isnull=True)
        else:
            qs = qs.filter(tenant__isnull=True, branch__isnull=True)
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
        special_keys = {
            serializer.validated_data["key"]
            for serializer in serializers
            if serializer.validated_data["key"] in SPECIAL_MANAGED_KEYS
        }
        if special_keys:
            raise ValidationError({
                "values": (
                    "Security and integration settings require their dedicated manage "
                    "permission and must be changed from their Platform Settings section."
                )
            })
        tenant, branch = resolve_request_scope(request)
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
                tenant=tenant,
                branch=branch,
                reason=serializer.validated_data["reason"],
            ))
        data = ConfigurationValueSerializer(rows, many=True).data
        return success_response(
            "Configuration values saved." if is_bulk else "Configuration value saved.",
            data if is_bulk else data[0],
            status=status.HTTP_201_CREATED,
        )


# Clear one physical value so the next parent scope or product default becomes effective.
class ValueResetView(ConfigAPIView):
    permission_map = {"DELETE": ConfigPermissions.VALUE_UPDATE}

    @transaction.atomic
    def delete(self, request, key):
        serializer = ClearConfigurationValueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if key in SPECIAL_MANAGED_KEYS:
            raise ValidationError({
                "value": (
                    "This setting must be reset from its Security or Integrations section."
                )
            })
        definition = get_object_or_404(ConfigurationDefinition, key=key, is_active=True)
        tenant, branch = resolve_request_scope(request)
        cleared = clear_value(
            definition=definition,
            actor=request.user,
            tenant=tenant,
            branch=branch,
            reason=serializer.validated_data["reason"] or "Reset to inherited value",
        )
        value, source = resolve_value(definition, tenant=tenant, branch=branch)
        if definition.sensitivity == definition.Sensitivity.SECRET_REFERENCE:
            value = "[REDACTED]" if value is not None else None
        return success_response(
            "Configuration value reset." if cleared else "No scoped override was present.",
            {
                "key": key,
                "cleared": cleared,
                "effective_value": value,
                "source": source.scope_key if source else "default",
            },
        )


# Curated platform profile and school-onboarding settings used by real consumers.
class PlatformSettingsView(ConfigAPIView):
    platform_methods = {"GET", "PATCH"}
    permission_map = {
        "GET": ConfigPermissions.VALUE_VIEW,
        "PATCH": ConfigPermissions.VALUE_UPDATE,
    }

    @staticmethod
    def payload():
        data = resolve_platform_settings()
        data["options"] = {
            "ownership_types": [
                {"value": value, "label": label} for value, label in OwnershipType.choices
            ],
            "term_structures": [
                {"value": value, "label": label} for value, label in TermStructure.choices
            ],
            "currencies": [
                {"value": value, "label": label} for value, label in Currency.choices
            ],
        }
        return data

    def get(self, request):
        return success_response("Platform settings retrieved.", self.payload())

    @transaction.atomic
    def patch(self, request):
        serializer = PlatformSettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason") or "Updated from Platform Settings"

        submitted = {
            **{
                PROFILE_FIELDS[field]: value
                for field, value in serializer.validated_data.get("profile", {}).items()
            },
            **{
                ONBOARDING_FIELDS[field]: value
                for field, value in serializer.validated_data.get("onboarding", {}).items()
            },
        }
        definitions = {
            item.key: item
            for item in ConfigurationDefinition.objects.filter(
                key__in=submitted, is_active=True
            )
        }
        missing = sorted(set(submitted) - set(definitions))
        if missing:
            raise ValidationError({"settings": f"Missing definitions: {', '.join(missing)}."})

        for key, value in submitted.items():
            definition = definitions[key]
            if key in PROFILE_FIELDS.values() and value == "":
                clear_value(
                    definition=definition,
                    actor=request.user,
                    reason=reason,
                )
            else:
                set_value(
                    definition=definition,
                    value=value,
                    actor=request.user,
                    reason=reason,
                )

        return success_response("Platform settings saved.", self.payload())


def _save_curated_values(
    *, field_map, validated_data, actor, reason, tenant=None, branch=None,
    compliance_validator=None,
):
    submitted = {
        field_map[field]: value
        for field, value in validated_data.items()
        if field in field_map
    }
    definitions = {
        item.key: item
        for item in ConfigurationDefinition.objects.filter(key__in=submitted, is_active=True)
    }
    missing = sorted(set(submitted) - set(definitions))
    if missing:
        raise ValidationError({"settings": f"Missing definitions: {', '.join(missing)}."})
    for key, value in submitted.items():
        if value is None:
            clear_value(
                definition=definitions[key], actor=actor, tenant=tenant,
                branch=branch, reason=reason,
            )
        else:
            if compliance_validator:
                field = next(name for name, mapped_key in field_map.items() if mapped_key == key)
                try:
                    compliance_validator(field, value, tenant=tenant, branch=branch)
                except ValueError as exc:
                    raise ValidationError({field: str(exc)})
            set_value(
                definition=definitions[key], value=value, actor=actor,
                tenant=tenant, branch=branch, reason=reason,
            )


# Runtime authentication controls, guarded by dedicated special-config permissions.
class SecuritySettingsView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.SECURITY_VIEW,
        "PATCH": ConfigPermissions.SECURITY_MANAGE,
    }

    def get(self, request):
        tenant, branch = resolve_request_scope(request)
        return success_response(
            "Security settings retrieved.",
            resolve_security_settings(tenant=tenant, branch=branch),
        )

    @transaction.atomic
    def patch(self, request):
        serializer = SecuritySettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant, branch = resolve_request_scope(request)
        reason = serializer.validated_data.get("reason") or "Updated from Security Settings"
        _save_curated_values(
            field_map=SECURITY_FIELDS,
            validated_data=serializer.validated_data,
            actor=request.user,
            reason=reason,
            tenant=tenant,
            branch=branch,
            compliance_validator=validate_security_compliance,
        )
        return success_response(
            "Security settings saved.",
            resolve_security_settings(tenant=tenant, branch=branch),
        )


# Editable communication defaults and secret-free deployment integration status.
class IntegrationSettingsView(ConfigAPIView):
    platform_methods = {"GET", "PATCH"}
    permission_map = {
        "GET": ConfigPermissions.INTEGRATION_VIEW,
        "PATCH": ConfigPermissions.INTEGRATION_MANAGE,
    }

    def get(self, request):
        return success_response("Integration settings retrieved.", resolve_integration_settings())

    @transaction.atomic
    def patch(self, request):
        serializer = IntegrationSettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason") or "Updated from Integration Settings"
        _save_curated_values(
            field_map=INTEGRATION_FIELDS,
            validated_data=serializer.validated_data,
            actor=request.user,
            reason=reason,
        )
        return success_response("Integration settings saved.", resolve_integration_settings())


class IntegrationConnectionTestView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {"POST": ConfigPermissions.INTEGRATION_MANAGE}

    def post(self, request):
        serializer = IntegrationConnectionTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = serializer.validated_data["connection"]
        tester = test_email_connection if connection == "email" else test_payment_connection
        connected = False
        try:
            tester(actor_id=request.user.pk)
            connected = True
        except ConnectionTestCoolingDown:
            raise ValidationError(
                {"connection": "Wait 30 seconds before testing this connection again."}
            )
        except Exception:
            # Provider and SMTP failures can carry endpoints and response bodies,
            # so the trace goes to the server log and never to the response.
            logger.exception(
                "Integration connection test failed for '%s' (actor %s)",
                connection, request.user.pk,
            )
            connected = False

        record_configuration_event(
            action="config.integration.connection_tested",
            target=IntegrationConnection(connection),
            actor=request.user,
            metadata={"connection": connection, "result": "connected" if connected else "failed"},
        )
        return success_response(
            "Connection test completed.",
            {
                "connection": connection,
                "connected": connected,
                "message": (
                    "Connection succeeded."
                    if connected
                    else "Connection failed. Check deployment credentials and network access."
                ),
            },
        )


# Report resolved configuration values after inheritance and secret redaction.
class EffectiveValueView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.VALUE_VIEW}

    # Compute effective values instead of returning only physically stored rows.
    def get(self, request, key=None):
        tenant, branch = resolve_request_scope(request)
        definitions = ConfigurationDefinition.objects.filter(is_active=True)
        if key:
            definitions = definitions.filter(key=key)
            if not definitions.exists():
                raise NotFound("Configuration definition not found.")
        data = []
        for definition in definitions:
            value, source = resolve_value(definition, tenant=tenant, branch=branch)
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
        # Prefetch keeps the serializer's dependency-key read O(1) per page.
        qs = Capability.objects.prefetch_related("dependency_links__requires")
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

    # List entitlements at the resolved platform or tenant scope.
    def get(self, request):
        tenant, _ = resolve_request_scope(request)
        qs = CapabilityEntitlement.all_objects.select_related("capability", "updated_by")
        # Entitlements never live at branch scope, so discard the branch half of resolution.
        qs = qs.filter(tenant=tenant) if tenant else qs.filter(tenant__isnull=True)
        return self.paginate(request, qs, CapabilityEntitlementSerializer)

    # Grant or revoke entitlement before scoped overrides can enable the feature.
    def post(self, request):
        serializer = SetEntitlementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capability = get_object_or_404(Capability, key=serializer.validated_data["capability"])
        tenant, _ = resolve_request_scope(request)
        row = set_entitlement(
            capability=capability, tenant=tenant,
            state=serializer.validated_data["state"],
            source=serializer.validated_data["source"], actor=request.user,
            starts_at=serializer.validated_data.get("starts_at"),
            ends_at=serializer.validated_data.get("ends_at"),
            reason=serializer.validated_data["reason"],
        )
        return success_response(
            "Capability entitlement saved.", CapabilityEntitlementSerializer(row).data,
            status=status.HTTP_201_CREATED,
        )


class EntitlementResetView(ConfigAPIView):
    platform_methods = {"DELETE"}
    permission_map = {"DELETE": ConfigPermissions.ENTITLEMENT_MANAGE}

    def delete(self, request, capability):
        item = get_object_or_404(Capability, key=capability)
        tenant, _ = resolve_request_scope(request)
        serializer = ClearConfigurationValueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cleared = clear_entitlement(
            capability=item,
            tenant=tenant,
            actor=request.user,
            reason=serializer.validated_data["reason"],
        )
        inherited, active, state = entitlement_resolution(item, tenant)
        return success_response(
            "Capability entitlement reset.",
            {
                "capability": item.key,
                "cleared": cleared,
                "effective": active,
                "status": state,
                "source": (
                    "school" if inherited and inherited.tenant_id else
                    "platform" if inherited else "none"
                ),
            },
        )


class EntitlementCalendarView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.ENTITLEMENT_VIEW}
    CALENDAR_LIMIT = 500

    def get(self, request):
        tenant, _ = resolve_request_scope(request)
        all_tenants = request.query_params.get("all_tenants") == "true"
        is_platform_actor = (
            getattr(getattr(request.user, "tenant", None), "kind", None) == "PLATFORM"
        )
        if all_tenants and not is_platform_actor:
            raise PermissionDenied("Cross-school entitlement calendars are platform-scoped.")
        try:
            window_days = int(request.query_params.get("window_days", 90))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"window_days": "Use a whole number of days."}) from exc
        if window_days < 7 or window_days > 366:
            raise ValidationError({"window_days": "Use a window from 7 to 366 days."})

        queryset = CapabilityEntitlement.all_objects.filter(
            state=CapabilityEntitlement.State.GRANTED,
        ).select_related("capability", "tenant")
        if not all_tenants:
            queryset = (
                queryset.filter(tenant=tenant)
                if tenant is not None
                else queryset.filter(tenant__isnull=True)
            )
        now = timezone.now()
        window_end = now + timedelta(days=window_days)
        recent_start = now - timedelta(days=30)
        summary = {
            "expired": queryset.filter(ends_at__lte=now).count(),
            "expiring_7_days": queryset.filter(ends_at__gt=now, ends_at__lte=now + timedelta(days=7)).count(),
            "expiring_30_days": queryset.filter(ends_at__gt=now, ends_at__lte=now + timedelta(days=30)).count(),
            "expiring_90_days": queryset.filter(ends_at__gt=now, ends_at__lte=now + timedelta(days=90)).count(),
            "scheduled": queryset.filter(starts_at__gt=now).count(),
        }
        calendar_rows = list(
            queryset.filter(
                Q(ends_at__gte=recent_start, ends_at__lte=window_end)
                | Q(starts_at__gt=now, starts_at__lte=window_end)
            ).order_by("ends_at", "starts_at", "tenant__name", "capability__label")[: self.CALENDAR_LIMIT + 1]
        )
        truncated = len(calendar_rows) > self.CALENDAR_LIMIT
        entries = []
        for row in calendar_rows[: self.CALENDAR_LIMIT]:
            if row.ends_at and row.ends_at <= now:
                status_value = "expired"
                warning = "expired"
            elif row.starts_at and row.starts_at > now:
                status_value = "scheduled"
                warning = "scheduled"
            else:
                status_value = "active"
                days_left = ceil((row.ends_at - now).total_seconds() / 86400) if row.ends_at else None
                warning = (
                    "critical" if days_left is not None and days_left <= 7 else
                    "warning" if days_left is not None and days_left <= 30 else
                    "notice" if days_left is not None and days_left <= 90 else
                    "none"
                )
            entries.append({
                "id": str(row.pk),
                "capability": row.capability.key,
                "capability_label": row.capability.label,
                "tenant_slug": row.tenant.slug if row.tenant else "",
                "tenant_name": row.tenant.name if row.tenant else "Platform",
                "scope": "school" if row.tenant else "platform",
                "starts_at": row.starts_at.isoformat() if row.starts_at else None,
                "ends_at": row.ends_at.isoformat() if row.ends_at else None,
                "status": status_value,
                "warning": warning,
                "days_until_expiry": (
                    ceil((row.ends_at - now).total_seconds() / 86400)
                    if row.ends_at else None
                ),
            })
        return success_response(
            "Entitlement renewal calendar retrieved.",
            {
                "generated_at": now.isoformat(),
                "window_days": window_days,
                "summary": summary,
                "entries": entries,
                "truncated": truncated,
            },
        )


class EntitlementBulkScheduleView(ConfigAPIView):
    platform_methods = {"POST"}
    permission_map = {"POST": ConfigPermissions.ENTITLEMENT_MANAGE}

    def post(self, request):
        serializer = BulkSetEntitlementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["items"]
        capability_keys = {item["capability"] for item in items}
        tenant_slugs = {item.get("tenant", "") for item in items if item.get("tenant")}
        capabilities = {
            row.key: row
            for row in Capability.objects.filter(
                key__in=capability_keys,
                is_active=True,
                requires_entitlement=True,
            )
        }
        tenants = {
            row.slug: row
            for row in Tenant.objects.filter(
                slug__in=tenant_slugs,
                kind=Tenant.Kind.SCHOOL,
            )
        }
        missing_capabilities = sorted(capability_keys - set(capabilities))
        missing_tenants = sorted(tenant_slugs - set(tenants))
        if missing_capabilities:
            raise ValidationError({
                "items": f"Unknown entitlement capabilities: {', '.join(missing_capabilities)}."
            })
        if missing_tenants:
            raise ValidationError({"items": "One or more school scopes are unavailable."})
        targets = [
            (capabilities[item["capability"]], tenants.get(item.get("tenant", "")))
            for item in items
        ]
        kwargs = {
            "targets": targets,
            "actor": request.user,
            "reason": serializer.validated_data["reason"],
            "starts_at": serializer.validated_data.get("starts_at", UNSET),
            "ends_at": serializer.validated_data.get("ends_at", UNSET),
        }
        rows = bulk_schedule_entitlements(**kwargs)
        return success_response(
            f"Scheduled {len(rows)} entitlements.",
            CapabilityEntitlementSerializer(rows, many=True).data,
        )


# Manage capability overrides at branch, school, or platform scope.
class OverrideListSetView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.OVERRIDE_VIEW,
        "POST": ConfigPermissions.OVERRIDE_MANAGE,
    }

    # List overrides that are physically stored at the resolved scope.
    def get(self, request):
        tenant, branch = resolve_request_scope(request)
        qs = CapabilityOverride.all_objects.select_related("capability", "updated_by")
        if branch:
            qs = qs.filter(branch=branch)
        elif tenant:
            # Listing physical overrides does not include inherited platform rows.
            qs = qs.filter(tenant=tenant, branch__isnull=True)
        else:
            qs = qs.filter(tenant__isnull=True, branch__isnull=True)
        return self.paginate(request, qs, CapabilityOverrideSerializer)

    # Write an override after the service enforces entitlement constraints.
    def post(self, request):
        serializer = SetOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capability = get_object_or_404(Capability, key=serializer.validated_data["capability"])
        tenant, branch = resolve_request_scope(request)
        row = set_override(
            capability=capability, state=serializer.validated_data["state"],
            actor=request.user, tenant=tenant, branch=branch,
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
        tenant, branch = resolve_request_scope(request)
        data = bulk_effective_capabilities(tenant=tenant, branch=branch)
        return success_response("Effective capabilities retrieved.", data)


def _scoped_audit_queryset(request):
    tenant, branch = resolve_request_scope(request)
    return scoped_configuration_audit_queryset(tenant=tenant, branch=branch)


def _filtered_audit_queryset(request):
    raw_filters = {
        key: request.query_params.get(key)
        for key in (
            "action", "target_type", "target_id", "actor",
            "created_after", "created_before",
        )
        if request.query_params.get(key) not in (None, "")
    }
    serializer = ConfigurationAuditFilterSerializer(data=raw_filters)
    serializer.is_valid(raise_exception=True)
    return apply_configuration_audit_filters(
        _scoped_audit_queryset(request),
        audit_filter_snapshot(serializer.validated_data),
    )


class AuditSavedViewListCreateView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.AUDIT_VIEW,
        "POST": ConfigPermissions.AUDIT_VIEW,
    }

    def get(self, request):
        queryset = ConfigurationAuditSavedView.objects.filter(
            owner=request.user,
        ).select_related("tenant", "branch")
        return self.paginate(request, queryset, ConfigurationAuditSavedViewSerializer)

    def post(self, request):
        serializer = ConfigurationAuditSavedViewWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant, branch = resolve_request_scope(request)
        try:
            with transaction.atomic():
                saved_view = ConfigurationAuditSavedView.objects.create(
                    owner=request.user,
                    tenant=tenant,
                    branch=branch,
                    name=serializer.validated_data["name"],
                    filters=serializer.validated_data["filters"],
                )
        except IntegrityError as exc:
            raise ValidationError({"name": "You already have an audit view with this name."}) from exc
        return success_response(
            "Audit view saved.",
            ConfigurationAuditSavedViewSerializer(saved_view).data,
            status=status.HTTP_201_CREATED,
        )


class AuditSavedViewDeleteView(ConfigAPIView):
    permission_map = {"DELETE": ConfigPermissions.AUDIT_VIEW}

    def delete(self, request, view_id):
        saved_view = get_object_or_404(
            ConfigurationAuditSavedView.objects.filter(owner=request.user),
            pk=view_id,
        )
        saved_view.delete()
        return success_response("Saved audit view deleted.")


class AuditExportJobListCreateView(ConfigAPIView):
    permission_map = {
        "GET": ConfigPermissions.AUDIT_EXPORT,
        "POST": ConfigPermissions.AUDIT_EXPORT,
    }

    def get(self, request):
        queryset = ConfigurationAuditExportJob.objects.filter(
            requested_by=request.user,
        ).select_related("tenant", "branch")
        return self.paginate(request, queryset, ConfigurationAuditExportJobSerializer)

    def post(self, request):
        serializer = ConfigurationAuditExportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant, branch = resolve_request_scope(request)
        filters = audit_filter_snapshot(serializer.validated_data.get("filters", {}))
        try:
            job, created = create_configuration_audit_export(
                actor=request.user,
                tenant=tenant,
                branch=branch,
                filters=filters,
                client_key=serializer.validated_data.get("client_key", ""),
            )
        except ValueError as exc:
            if str(exc) != "EXPORT_LIMIT":
                raise
            raise ValidationError({
                "detail": (
                    "Three audit exports are already queued or running. "
                    "Wait for one to finish."
                )
            }) from exc
        if created:
            from .tasks import run_configuration_audit_export_task

            try:
                run_configuration_audit_export_task.delay(str(job.pk))
            except Exception:
                job.status = job.Status.FAILED
                job.failure_message = "The export queue is unavailable. Try again shortly."
                job.completed_at = timezone.now()
                job.save(update_fields=["status", "failure_message", "completed_at"])
                return error_response(
                    "The export queue is unavailable. Try again shortly.",
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        return success_response(
            "Configuration audit export queued." if created else "Existing export returned.",
            ConfigurationAuditExportJobSerializer(job).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AuditExportJobDownloadView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_EXPORT}

    def get(self, request, job_id):
        job = get_object_or_404(
            ConfigurationAuditExportJob.objects.select_related("tenant", "branch").filter(
                requested_by=request.user,
            ),
            pk=job_id,
        )
        actor_tenant = getattr(request.user, "tenant", None)
        is_platform = getattr(actor_tenant, "kind", None) == "PLATFORM"
        if job.tenant_id and not is_platform and job.tenant_id != getattr(actor_tenant, "pk", None):
            raise NotFound("Export job not found.")
        if job.status != job.Status.COMPLETED:
            return error_response("This export is not ready to download.", status=status.HTTP_409_CONFLICT)
        if not job.available_until or job.available_until <= timezone.now():
            return error_response("This export file has expired.", status=status.HTTP_410_GONE)
        if not job.storage_name or not default_storage.exists(job.storage_name):
            raise NotFound("The export file is no longer available.")
        record_configuration_event(
            action="config.audit.export_downloaded",
            target=job,
            actor=request.user,
            tenant=job.tenant,
            branch=job.branch,
            after={"row_count": job.row_count},
            reason="Downloaded queued configuration audit export",
        )
        return FileResponse(
            default_storage.open(job.storage_name, "rb"),
            as_attachment=True,
            filename=job.file_name,
            content_type="text/csv; charset=utf-8",
        )


# Expose immutable configuration audit events within the caller's tenant scope.
class AuditEventListView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_VIEW}

    # Restrict audit history to the branch or tenant selected by the caller.
    def get(self, request):
        return self.paginate(request, _filtered_audit_queryset(request), ConfigurationAuditEventSerializer)


class AuditEventFacetsView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_VIEW}

    def get(self, request):
        # Facets honor the selected tenant/branch and date window, while each
        # facet ignores the other categorical filters so choices do not vanish.
        qs = _scoped_audit_queryset(request)
        for date_key, lookup in (("created_after", "created_at__gte"), ("created_before", "created_at__lte")):
            raw = request.query_params.get(date_key)
            if raw:
                parsed = parse_datetime(raw)
                if parsed is None:
                    raise ValidationError({date_key: "Use an ISO 8601 date and time."})
                qs = qs.filter(**{lookup: parsed})
        actions = list(qs.order_by("action").values_list("action", flat=True).distinct()[:100])
        target_types = list(
            qs.order_by("target_type").values_list("target_type", flat=True).distinct()[:100]
        )
        actors = [
            {
                "id": str(row[0]),
                "full_name": f"{row[1]} {row[2]}".strip(),
                "email": row[3],
            }
            for row in qs.exclude(actor__isnull=True)
            .order_by("actor__first_name", "actor__last_name", "actor__email")
            .values_list("actor_id", "actor__first_name", "actor__last_name", "actor__email")
            .distinct()[:200]
        ]
        target_rows = list(
            qs.select_related(None).only("target_type", "target_id", "metadata")[:500]
        )
        seen = set()
        targets = []
        serializer = ConfigurationAuditEventSerializer(context={
            "_target_labels": build_configuration_target_labels(target_rows),
        })
        for event in target_rows:
            key = (event.target_type, event.target_id)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "type": event.target_type,
                "id": event.target_id,
                "label": serializer.get_target_label(event) or event.target_type,
            })
            if len(targets) >= 200:
                break
        targets.sort(key=lambda item: (item["label"].lower(), item["type"], item["id"]))
        return success_response(
            "Configuration audit facets retrieved.",
            {"actions": actions, "target_types": target_types, "actors": actors, "targets": targets},
        )


class AuditEventExportView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_EXPORT}
    EXPORT_LIMIT = 5000

    def get(self, request):
        events = list(_filtered_audit_queryset(request)[: self.EXPORT_LIMIT + 1])
        truncated = len(events) > self.EXPORT_LIMIT
        events = events[: self.EXPORT_LIMIT]
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="configuration-audit.csv"'
        response["X-Export-Truncated"] = "true" if truncated else "false"
        writer = csv.writer(response)
        writer.writerow([
            "created_at", "action", "target_type", "target", "actor_name",
            "actor_email", "reason", "scope", "before", "after",
        ])
        serializer = ConfigurationAuditEventSerializer(context={
            "_target_labels": build_configuration_target_labels(events),
        })
        for event in events:
            writer.writerow([
                event.created_at.isoformat(),
                event.action,
                event.target_type,
                serializer.get_target_label(event) or event.target_type,
                event.actor.full_name if event.actor else "System",
                event.actor.email if event.actor else "",
                event.reason,
                event.scope_key,
                json.dumps(event.before_data, ensure_ascii=True, sort_keys=True),
                json.dumps(event.after_data, ensure_ascii=True, sort_keys=True),
            ])
        return response


# Return the complete redacted snapshots for one scoped audit event.
class AuditEventDetailView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.AUDIT_VIEW}

    def get(self, request, event_id):
        event = get_object_or_404(_scoped_audit_queryset(request), pk=event_id)
        return success_response(
            "Configuration audit event retrieved.",
            ConfigurationAuditEventSerializer(event).data,
        )


# Export effective configuration and capability state for the resolved scope.
class ConfigExportView(ConfigAPIView):
    permission_map = {"GET": ConfigPermissions.EXPORT_CREATE}

    # Build a redacted snapshot suitable for support and tenant diagnostics.
    def get(self, request):
        tenant, branch = resolve_request_scope(request)
        definitions = ConfigurationDefinition.objects.filter(is_active=True)
        values = []
        for definition in definitions:
            value, source = resolve_value(definition, tenant=tenant, branch=branch)
            if definition.sensitivity == definition.Sensitivity.SECRET_REFERENCE:
                # Export follows the same redaction rule as effective reads.
                value = "[REDACTED]" if value is not None else None
            values.append({"key": definition.key, "value": value, "source": source.scope_key if source else "default"})
        capabilities = bulk_effective_capabilities(tenant=tenant, branch=branch)
        return success_response(
            "Configuration export generated.",
            {"values": values, "capabilities": capabilities},
        )
