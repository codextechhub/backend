from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from .models import (
    AuditEvent,
    ContactInfo,
    InviteStatus,
    OperationOutcome,
    OperationType,
    ProvisioningRecord,
    ProvisioningStatus,
    RESERVED_TENANT_SLUGS,
    Institution,
    InstitutionBranding,
    InstitutionLifecycleEvent,
    InstitutionModuleSetting,
    InstitutionOperationEvent,
    InstitutionStatus,
)


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def _normalize_slug(raw: str) -> str:
    """Normalize to URL-safe slug; keep consistent with model validators."""
    raw = (raw or "").strip().lower()
    base = slugify(raw)
    base = base.replace("_", "-")
    # slugify can produce empty for weird names; let validation handle empties
    return base


def _build_slug_suggestions(base_slug: str, max_suggestions: int = 5) -> List[str]:
    """Return slug suggestions like school-name-2, school-name-3, ..."""
    if not base_slug:
        return []
    return [f"{base_slug}-{i}" for i in range(2, 2 + max_suggestions)]


def _slug_is_unique(slug: str, exclude_institution_id: Optional[str] = None) -> bool:
    qs = Institution.objects.all()
    if exclude_institution_id:
        qs = qs.exclude(id=exclude_institution_id)
    return not qs.filter(institution_slug=slug).exists()


# -----------------------------------------------------------------------------
# Leaf serializers
# -----------------------------------------------------------------------------

class ContactInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactInfo
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class InstitutionBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionBranding
        fields = [
            "id",
            "logo_asset_ref",
            "primary_color",
            "secondary_color",
            "accent_color",
            "background_color",
            "text_color",
            "theme_pack_key",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Minimal “token” validation hooks; keep it light and let BrandingService enforce deeper rules.
        # If you have a design token registry, validate theme_pack_key against it here.
        return attrs


class InstitutionModuleSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionModuleSetting
        fields = [
            "id",
            "module_key",
            "enabled",
            "effective_from",
            "changed_by_actor_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_module_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("module_key is required.")
        return value


class ProvisioningRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProvisioningRecord
        fields = [
            "id",
            "provisioning_status",
            "last_error_code",
            "last_error_message",
            "queued_at",
            "started_at",
            "completed_at",
            "rollback_status",
            "rollback_completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields  # provisioning is system-owned


class InstitutionLifecycleEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionLifecycleEvent
        fields = [
            "id",
            "from_state",
            "to_state",
            "actor_id",
            "reason",
            "occurred_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class InstitutionOperationEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionOperationEvent
        fields = [
            "id",
            "operation_type",
            "actor_id",
            "reason",
            "confirmation_token",
            "outcome",
            "error_code",
            "error_message",
            "occurred_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "actor_id",
            "action",
            "resource_type",
            "resource_id",
            "before_hash",
            "after_hash",
            "outcome",
            "occurred_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# Primary Admin serializers (write supports nested ContactInfo)
# -----------------------------------------------------------------------------

class InstitutionPrimaryAdminWriteSerializer(serializers.Serializer):
    """
    Write-only structure for assigning primary admin.
    We store a ContactInfo record + InstitutionPrimaryAdmin link record.
    """

    full_name = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    role_label = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class InstitutionPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns link + contact."""
    id = serializers.UUIDField()
    role_label = serializers.CharField()
    invite_status = serializers.CharField()
    invite_queued_at = serializers.DateTimeField(allow_null=True)
    invite_sent_at = serializers.DateTimeField(allow_null=True)
    contact = ContactInfoSerializer()


# -----------------------------------------------------------------------------
# Institution serializers (read)
# -----------------------------------------------------------------------------

class InstitutionListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = [
            "id",
            "institution_name",
            "institution_slug",
            "category",
            "institution_type",
            "plan_tier",
            "country",
            "region",
            "timezone",
            "currency",
            "status",
            "activated_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class InstitutionDetailSerializer(serializers.ModelSerializer):
    branding = InstitutionBrandingSerializer(read_only=True)
    module_settings = InstitutionModuleSettingSerializer(many=True, read_only=True)
    provisioning = ProvisioningRecordSerializer(read_only=True)
    lifecycle_events = InstitutionLifecycleEventSerializer(many=True, read_only=True)
    operation_events = InstitutionOperationEventSerializer(many=True, read_only=True)
    audit_events = AuditEventSerializer(many=True, read_only=True)

    primary_admin = serializers.SerializerMethodField()

    class Meta:
        model = Institution
        fields = [
            "id",
            "institution_name",
            "institution_slug",
            "category",
            "institution_type",
            "plan_tier",
            "country",
            "region",
            "timezone",
            "currency",
            "primary_contact_name",
            "primary_contact_email",
            "primary_contact_phone",
            "status",
            "activated_at",
            "deleted_at",
            "created_at",
            "updated_at",
            # Nested
            "branding",
            "module_settings",
            "provisioning",
            "primary_admin",
            "lifecycle_events",
            "operation_events",
            "audit_events",
        ]
        read_only_fields = fields

    def get_primary_admin(self, obj: Institution) -> Optional[Dict[str, Any]]:
        link = getattr(obj, "primary_admin_link", None)
        if not link:
            return None
        return {
            "id": link.id,
            "role_label": link.role_label,
            "invite_status": link.invite_status,
            "invite_queued_at": link.invite_queued_at,
            "invite_sent_at": link.invite_sent_at,
            "contact": ContactInfoSerializer(link.contact).data,
        }


# -----------------------------------------------------------------------------
# Institution serializers (write: create / update)
# -----------------------------------------------------------------------------

class InstitutionCreateSerializer(serializers.ModelSerializer):
    """
    Creates a institution and its initial system-owned companions:
      - ProvisioningRecord (Queued)
      - Lifecycle event (Created)
    Optional nested:
      - Branding
      - Primary admin (ContactInfo + InstitutionPrimaryAdmin link)
      - Initial module settings

    NOTE: true provisioning execution + invite sending typically belongs in services/tasks.
    """

    institution_slug = serializers.CharField(required=False, allow_blank=True)
    branding = InstitutionBrandingSerializer(required=False)
    primary_admin = InstitutionPrimaryAdminWriteSerializer(required=False)
    module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

    class Meta:
        model = Institution
        fields = [
            "institution_name",
            "institution_slug",
            "category",
            "institution_type",
            "plan_tier",
            "country",
            "region",
            "timezone",
            "currency",
            "primary_contact_name",
            "primary_contact_email",
            "primary_contact_phone",
            # optional nested
            "branding",
            "primary_admin",
            "module_settings",
        ]

    def validate_institution_slug(self, value: str) -> str:
        # If user provided slug, normalize it and validate constraints.
        if value is None:
            return ""
        normalized = _normalize_slug(value)
        if not normalized:
            # allow empty here because we can auto-generate; strict check in validate()
            return ""
        if normalized in RESERVED_TENANT_SLUGS:
            raise serializers.ValidationError("This slug is reserved. Choose another.")
        if not _slug_is_unique(normalized):
            suggestions = _build_slug_suggestions(normalized)
            raise serializers.ValidationError(
                {"message": "Slug already exists.", "suggestions": suggestions}
            )
        return normalized

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Auto-generate slug if not provided
        raw_slug = (attrs.get("institution_slug") or "").strip()
        if not raw_slug:
            base = _normalize_slug(attrs.get("institution_name", ""))
            if not base:
                raise serializers.ValidationError(
                    {"institution_slug": "Unable to generate slug from institution_name. Provide institution_slug explicitly."}
                )
            if base in RESERVED_TENANT_SLUGS:
                base = f"{base}-institution"
            if not _slug_is_unique(base):
                suggestions = [s for s in _build_slug_suggestions(base) if _slug_is_unique(s)]
                raise serializers.ValidationError(
                    {"institution_slug": {"message": "Generated slug conflicts.", "suggestions": suggestions}}
                )
            attrs["institution_slug"] = base

        # Example: if module_settings provided, ensure module_key uniqueness in payload
        ms = attrs.get("module_settings") or []
        keys = [m.get("module_key") for m in ms if m.get("module_key")]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError({"module_settings": "Duplicate module_key values in request payload."})

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> Institution:
        branding_data = validated_data.pop("branding", None)
        primary_admin_data = validated_data.pop("primary_admin", None)
        module_settings_data = validated_data.pop("module_settings", [])

        # Create Institution
        institution = Institution.objects.create(**validated_data, status=InstitutionStatus.CREATED)

        # Create system-owned provisioning record (Queued)
        ProvisioningRecord.objects.create(
            institution=institution,
            provisioning_status=ProvisioningStatus.QUEUED,
        )

        # Record initial lifecycle event (Created -> Created is noisy; record as "Created")
        InstitutionLifecycleEvent.objects.create(
            institution=institution,
            from_state=InstitutionStatus.CREATED,
            to_state=InstitutionStatus.CREATED,
            actor_id=str(self.context.get("actor_id", "system")),
            reason="Institution created",
        )

        # Optional branding
        if branding_data:
            InstitutionBranding.objects.create(institution=institution, **branding_data)

        # Optional module settings (bulk upsert-friendly approach)
        for ms in module_settings_data:
            InstitutionModuleSetting.objects.create(
                institution=institution,
                module_key=ms["module_key"],
                enabled=ms.get("enabled", False),
                effective_from=ms.get("effective_from"),
                changed_by_actor_id=str(self.context.get("actor_id", "")),
            )

        # Optional primary admin assignment (ContactInfo + link)
        if primary_admin_data:
            contact = ContactInfo.objects.create(
                full_name=primary_admin_data["full_name"],
                email=primary_admin_data["email"],
                phone=primary_admin_data.get("phone", ""),
            )
            # Link model lives in models.py; import inside to avoid circular
            from .models import InstitutionPrimaryAdmin  # noqa: WPS433

            InstitutionPrimaryAdmin.objects.create(
                institution=institution,
                contact=contact,
                role_label=primary_admin_data.get("role_label", ""),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=None,
                invite_sent_at=None,
            )

        # Audit record (optional: keep this minimal; real audit service may do more)
        AuditEvent.objects.create(
            institution=institution,
            actor_id=str(self.context.get("actor_id", "system")),
            action="TENANT_CREATE",
            resource_type="Institution",
            resource_id=str(institution.id),
            outcome=OperationOutcome.SUCCEEDED,
        )

        return institution


class InstitutionUpdateSerializer(serializers.ModelSerializer):
    """
    Updates institution metadata and optionally branding/module settings.
    Keep destructive ops and lifecycle transitions in dedicated serializers.
    """

    branding = InstitutionBrandingSerializer(required=False)
    module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

    class Meta:
        model = Institution
        fields = [
            "institution_name",
            "category",
            "institution_type",
            "plan_tier",
            "country",
            "region",
            "timezone",
            "currency",
            "primary_contact_name",
            "primary_contact_email",
            "primary_contact_phone",
            "branding",
            "module_settings",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Example policy hook: disallow plan_tier changes when Live unless approved
        institution: Institution = self.instance
        if institution and institution.status == InstitutionStatus.LIVE and "plan_tier" in attrs:
            # If you want to allow this with approval, remove/relax.
            raise serializers.ValidationError({"plan_tier": "Plan tier changes are restricted once institution is Live."})

        ms = attrs.get("module_settings") or []
        keys = [m.get("module_key") for m in ms if m.get("module_key")]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError({"module_settings": "Duplicate module_key values in request payload."})
        return attrs

    @transaction.atomic
    def update(self, instance: Institution, validated_data: Dict[str, Any]) -> Institution:
        branding_data = validated_data.pop("branding", None)
        module_settings_data = validated_data.pop("module_settings", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.full_clean()
        instance.save()

        actor_id = str(self.context.get("actor_id", "system"))

        # Branding upsert
        if branding_data is not None:
            InstitutionBranding.objects.update_or_create(
                institution=instance,
                defaults=branding_data,
            )

        # Module settings upsert (per institution+module_key)
        if module_settings_data is not None:
            for ms in module_settings_data:
                InstitutionModuleSetting.objects.update_or_create(
                    institution=instance,
                    module_key=ms["module_key"],
                    defaults={
                        "enabled": ms.get("enabled", False),
                        "effective_from": ms.get("effective_from"),
                        "changed_by_actor_id": actor_id,
                    },
                )

        AuditEvent.objects.create(
            institution=instance,
            actor_id=actor_id,
            action="TENANT_UPDATE",
            resource_type="Institution",
            resource_id=str(instance.id),
            outcome=OperationOutcome.SUCCEEDED,
        )

        return instance


# -----------------------------------------------------------------------------
# Lifecycle & Operations serializers (write)
# -----------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    InstitutionStatus.CREATED: (InstitutionStatus.CONFIGURING, InstitutionStatus.LOCKED),
    InstitutionStatus.CONFIGURING: (InstitutionStatus.DATA_IMPORTING, InstitutionStatus.LOCKED),
    InstitutionStatus.DATA_IMPORTING: (InstitutionStatus.READY, InstitutionStatus.LOCKED),
    InstitutionStatus.READY: (InstitutionStatus.LIVE, InstitutionStatus.LOCKED),
    InstitutionStatus.LIVE: (InstitutionStatus.SUSPENDED, InstitutionStatus.LOCKED),
    InstitutionStatus.SUSPENDED: (InstitutionStatus.READY, InstitutionStatus.LIVE),
    InstitutionStatus.LOCKED: (InstitutionStatus.LOCKED,),
    InstitutionStatus.DELETED_SOFT: (InstitutionStatus.DELETED_SOFT,),
}


class InstitutionStateTransitionSerializer(serializers.Serializer):
    to_state = serializers.ChoiceField(choices=InstitutionStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        institution: Institution = self.context["institution"]
        to_state = attrs["to_state"]
        allowed = _ALLOWED_TRANSITIONS.get(institution.status, tuple())
        if to_state not in allowed:
            raise serializers.ValidationError(
                {"to_state": f"Invalid transition from {institution.status} to {to_state}."}
            )
        return attrs

    @transaction.atomic
    def save(self, **kwargs) -> Institution:
        institution: Institution = self.context["institution"]
        actor_id = str(self.context.get("actor_id", "system"))
        to_state = self.validated_data["to_state"]
        reason = self.validated_data.get("reason", "")

        institution.transition(to_state=to_state, actor_id=actor_id, reason=reason)

        AuditEvent.objects.create(
            institution=institution,
            actor_id=actor_id,
            action="TENANT_STATE_TRANSITION",
            resource_type="Institution",
            resource_id=str(institution.id),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return institution


class InstitutionSuspendSerializer(serializers.Serializer):
    reason = serializers.CharField()

    @transaction.atomic
    def save(self, **kwargs) -> Institution:
        institution: Institution = self.context["institution"]
        actor_id = str(self.context.get("actor_id", "system"))
        reason = self.validated_data["reason"].strip()

        if not reason:
            raise serializers.ValidationError({"reason": "Suspension reason is required."})

        try:
            institution.suspend(actor_id=actor_id, reason=reason)
            outcome = OperationOutcome.SUCCEEDED
            error_code = ""
            error_message = ""
        except DjangoValidationError as e:
            outcome = OperationOutcome.FAILED
            error_code = "VALIDATION_ERROR"
            error_message = str(e)
            raise
        finally:
            InstitutionOperationEvent.objects.create(
                institution=institution,
                operation_type=OperationType.SUSPEND,
                actor_id=actor_id,
                reason=reason,
                confirmation_token="",
                outcome=outcome,
                error_code=error_code,
                error_message=error_message,
            )
            AuditEvent.objects.create(
                institution=institution,
                actor_id=actor_id,
                action="TENANT_SUSPEND",
                resource_type="Institution",
                resource_id=str(institution.id),
                outcome=outcome,
            )

        return institution


class InstitutionReactivateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> Institution:
        institution: Institution = self.context["institution"]
        actor_id = str(self.context.get("actor_id", "system"))
        reason = self.validated_data.get("reason", "")

        institution.reactivate(actor_id=actor_id, reason=reason)

        InstitutionOperationEvent.objects.create(
            institution=institution,
            operation_type=OperationType.REACTIVATE,
            actor_id=actor_id,
            reason=reason,
            confirmation_token="",
            outcome=OperationOutcome.SUCCEEDED,
            error_code="",
            error_message="",
        )
        AuditEvent.objects.create(
            institution=institution,
            actor_id=actor_id,
            action="TENANT_REACTIVATE",
            resource_type="Institution",
            resource_id=str(institution.id),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return institution


class InstitutionSoftDeleteSerializer(serializers.Serializer):
    reason = serializers.CharField()

    @transaction.atomic
    def save(self, **kwargs) -> Institution:
        institution: Institution = self.context["institution"]
        actor_id = str(self.context.get("actor_id", "system"))
        reason = self.validated_data["reason"].strip()

        if institution.status == InstitutionStatus.DELETED_SOFT:
            return institution

        institution.soft_delete(actor_id=actor_id, reason=reason)

        InstitutionOperationEvent.objects.create(
            institution=institution,
            operation_type=OperationType.SOFT_DELETE,
            actor_id=actor_id,
            reason=reason,
            confirmation_token="",
            outcome=OperationOutcome.SUCCEEDED,
            error_code="",
            error_message="",
        )
        AuditEvent.objects.create(
            institution=institution,
            actor_id=actor_id,
            action="TENANT_SOFT_DELETE",
            resource_type="Institution",
            resource_id=str(institution.id),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return institution


class InstitutionResetConfigSerializer(serializers.Serializer):
    """
    Resets institution configuration to baseline (branding/modules/localization),
    without deleting core operational data (policy-driven).
    """
    confirmation_token = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> Institution:
        institution: Institution = self.context["institution"]
        actor_id = str(self.context.get("actor_id", "system"))

        token = (self.validated_data.get("confirmation_token") or "").strip()
        if not token:
            raise serializers.ValidationError({"confirmation_token": "Confirmation token is required."})

        # Baseline reset example:
        # - Remove branding
        # - Disable all modules (or re-seed defaults depending on your product policy)
        # - Clear localization (optional; many teams keep localization)
        InstitutionBranding.objects.filter(institution=institution).delete()
        InstitutionModuleSetting.objects.filter(institution=institution).update(enabled=False, changed_by_actor_id=actor_id)

        InstitutionOperationEvent.objects.create(
            institution=institution,
            operation_type=OperationType.RESET,
            actor_id=actor_id,
            reason=self.validated_data.get("reason", ""),
            confirmation_token=token[:128],  # never store secrets; token here is “typed phrase”
            outcome=OperationOutcome.SUCCEEDED,
            error_code="",
            error_message="",
        )
        AuditEvent.objects.create(
            institution=institution,
            actor_id=actor_id,
            action="TENANT_RESET_CONFIG",
            resource_type="Institution",
            resource_id=str(institution.id),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return institution
