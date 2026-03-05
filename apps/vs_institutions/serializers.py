from __future__ import annotations

from time import timezone
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
    BranchStatus,
    RESERVED_TENANT_SLUGS,
    Institution,
    Branch,
    BranchPrimaryAdmin,
    InstitutionBranding,
    BranchLifecycle,
    InstitutionModuleSetting,
    InstitutionStatus,
    PlanTier,
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


def _slug_is_unique(slug: str, exclude_institution_slug: Optional[str] = None) -> bool:
    qs = Institution.objects.all()
    if exclude_institution_slug:
        qs = qs.exclude(slug=exclude_institution_slug)
    return not qs.filter(slug=slug).exists()


# -----------------------------------------------------------------------------
# Leaf serializers
# -----------------------------------------------------------------------------

class ContactInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactInfo
        fields = [
            "full_name",
            "email",
            "phone",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [ "created_at", "updated_at"]


class InstitutionBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionBranding
        fields = [
            
            "logo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [ "created_at", "updated_at"]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Minimal “token” validation hooks; keep it light and let BrandingService enforce deeper rules.
        # If you have a design token registry, validate theme_pack_key against it here.
        return attrs


class InstitutionModuleSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstitutionModuleSetting
        fields = [
            "module_key",
            "enabled",
            "effective_from",
            "changed_by_actor_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [ "created_at", "updated_at"]

    def validate_module_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("module_key is required.")
        return value


class BranchLifecycleSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchLifecycle
        fields = [
            "from_state",
            "to_state",
            "actor_id",
            "reason",
            "occurred_at",
        ]
        read_only_fields = fields


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "actor_id",
            "action",
            "resource_type",
            "resource_slug",
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

class BranchPrimaryAdminWriteSerializer(serializers.Serializer):
    """
    Write-only structure for assigning primary admin.
    We store a ContactInfo record + BranchPrimaryAdmin link record.
    """

    full_name = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    role_label = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class BranchPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns link + contact."""
    id = serializers.UUIDField()
    role_label = serializers.CharField()
    invite_status = serializers.CharField()
    invite_queued_at = serializers.DateTimeField(allow_null=True)
    invite_sent_at = serializers.DateTimeField(allow_null=True)
    contact = ContactInfoSerializer()


# class InstitutionListSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = Institution
#         fields = [
#             "name",
#             "slug",
#             "_type",
#             "country",
#             "state",
#             "city",
#             "status",
#         ]
#         read_only_fields = fields


# class InstitutionDetailSerializer(serializers.ModelSerializer):
#     branding = InstitutionBrandingSerializer(read_only=True)
#     primary_admin = BranchPrimaryAdminReadSerializer(read_only=True)

#     class Meta:
#         model = Institution
#         fields = [
#             "name",
#             ,
#             "slug",
#             "category",
#             "_type",
#             "plan_tier",
#             "country",
#             "state",
#             "status",
#             # Nested
#             "branding",
#             "primary_admin",
#         ]
#         read_only_fields = fields


# class InstitutionCreateSerializer(serializers.ModelSerializer):
#     """
#     Creates a institution and its initial system-owned companions:
#       - ProvisioningRecord (Queued)
#       - Lifecycle event (Created)
#     Optional nested:
#       - Branding
#       - Primary admin (ContactInfo + BranchPrimaryAdmin link)
#       - Initial module settings

#     NOTE: true provisioning execution + invite sending typically belongs in services/tasks.
#     """

#     branding = InstitutionBrandingSerializer(required=False)
#     primary_admin = BranchPrimaryAdminWriteSerializer(required=False)
#     module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

#     class Meta:
#         model = Institution
#         fields = [
#             "name",
#             "slug",
#             ,
#             "email",
#             "website",
#             "phone_number",
#             "category",
#             "_type",
#             "plan_tier",
#             "country",
#             "state",
#             "city",
#             "timezone",
#             "currency",
#             # optional nested
#             "branding",
#             "primary_admin",
#             "module_settings",
#         ]

#     def validate_slug(self, value: str) -> str:
#         # If user provided slug, normalize it and validate constraints.
#         if value is None:
#             return ""
#         normalized = _normalize_slug(value)
#         if not normalized:
#             # allow empty here because we can auto-generate; strict check in validate()
#             return ""
#         if normalized in RESERVED_TENANT_SLUGS:
#             raise serializers.ValidationError("This slug is reserved. Choose another.")
#         if not _slug_is_unique(normalized):
#             suggestions = _build_slug_suggestions(normalized)
#             raise serializers.ValidationError(
#                 {"message": "Slug already exists.", "suggestions": suggestions}
#             )
#         return normalized

#     def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
#         # Auto-generate slug if not provided
#         raw_slug = (attrs.get("slug") or "").strip()
#         if not raw_slug:
#             base = _normalize_slug(attrs.get("name", ""))
#             if not base:
#                 raise serializers.ValidationError(
#                     {"slug": "Unable to generate slug from name. Provide slug explicitly."}
#                 )
#             if base in RESERVED_TENANT_SLUGS:
#                 base = f"{base}-institution"
#             if not _slug_is_unique(base):
#                 suggestions = [s for s in _build_slug_suggestions(base) if _slug_is_unique(s)]
#                 raise serializers.ValidationError(
#                     {"slug": {"message": "Generated slug conflicts.", "suggestions": suggestions}}
#                 )
#             attrs["slug"] = base

#         # Example: if module_settings provided, ensure module_key uniqueness in payload
#         ms = attrs.get("module_settings") or []
#         keys = [m.get("module_key") for m in ms if m.get("module_key")]
#         if len(keys) != len(set(keys)):
#             raise serializers.ValidationError({"module_settings": "Duplicate module_key values in request payload."})

#         return attrs

#     @transaction.atomic
#     def create(self, validated_data: Dict[str, Any]) -> Institution:
#         branding_data = validated_data.pop("branding", None)
#         primary_admin_data = validated_data.pop("primary_admin", None)
#         module_settings_data = validated_data.pop("module_settings", [])

#         # Create Institution
#         institution = Institution.objects.create(**validated_data, status=InstitutionStatus.PENDING)

#         # Record initial lifecycle event (Created -> Created is noisy; record as "Created")
#         BranchLifecycle.objects.create(
#             institution=institution,
#             from_state="",
#             to_state=InstitutionStatus.PENDING,
#             actor_id=str(self.context.get("actor_id", "system")),
#             reason="Institution created",
#         )

#         # Optional branding
#         if branding_data:
#             InstitutionBranding.objects.create(institution=institution, **branding_data)

#         # Optional module settings (bulk upsert-friendly approach)
#         for ms in module_settings_data:
#             InstitutionModuleSetting.objects.create(
#                 institution=institution,
#                 module_key=ms["module_key"],
#                 enabled=ms.get("enabled", False),
#                 effective_from=ms.get("effective_from"),
#                 changed_by_actor_id=str(self.context.get("actor_id", "")),
#             )

#         # Optional primary admin assignment (ContactInfo + link)
#         if primary_admin_data:
#             contact = ContactInfo.objects.create(
#                 full_name=primary_admin_data["full_name"],
#                 email=primary_admin_data["email"],
#                 phone=primary_admin_data.get("phone", ""),
#             )
#             # Link model lives in models.py; import inside to avoid circular
#             from .models import BranchPrimaryAdmin

#             BranchPrimaryAdmin.objects.create(
#                 institution=institution,
#                 contact=contact,
#                 role_label=primary_admin_data.get("role_label", "Institution_Admin"),
#                 invite_status=InviteStatus.QUEUED,
#                 invite_queued_at=None,
#                 invite_sent_at=None,
#             )

#         # Audit record (optional: keep this minimal; real audit service may do more)
#         AuditEvent.objects.create(
#             institution=institution,
#             actor_id=str(self.context.get("actor_id", "system")),
#             action="INSTITUTION_CREATE",
#             resource_type="Institution",
#             resource_slug=str(institution.slug),
#             outcome=OperationOutcome.SUCCEEDED,
#         )

#         return institution


# class InstitutionUpdateSerializer(serializers.ModelSerializer):
#     """
#     Updates institution metadata and optionally branding/module settings.
#     Keep destructive ops and lifecycle transitions in dedicated serializers.
#     """

#     branding = InstitutionBrandingSerializer(required=False)
#     module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

#     class Meta:
#         model = Institution
#         fields = [
#             "category",
#             "_type",
#             "plan_tier",
#             "currency",
#             "branding",
#             "module_settings",
#         ]

#     def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
#         # Example policy hook: disallow plan_tier changes when Live unless approved
#         institution: Institution = self.instance
#         if institution and institution.status == InstitutionStatus.LIVE and "plan_tier" in attrs:
#             # If you want to allow this with approval, remove/relax.
#             raise serializers.ValidationError({"plan_tier": "Plan tier changes are restricted once institution is Live."})

#         ms = attrs.get("module_settings") or []
#         keys = [m.get("module_key") for m in ms if m.get("module_key")]
#         if len(keys) != len(set(keys)):
#             raise serializers.ValidationError({"module_settings": "Duplicate module_key values in request payload."})
#         return attrs

#     @transaction.atomic
#     def update(self, instance: Institution, validated_data: Dict[str, Any]) -> Institution:
#         branding_data = validated_data.pop("branding", None)
#         module_settings_data = validated_data.pop("module_settings", None)

#         for attr, value in validated_data.items():
#             setattr(instance, attr, value)
#         instance.full_clean()
#         instance.save()

#         actor_id = str(self.context.get("actor_id", "system"))

#         # Branding upsert
#         if branding_data is not None:
#             InstitutionBranding.objects.update_or_create(
#                 institution=instance,
#                 defaults=branding_data,
#             )

#         # Module settings upsert (per institution+module_key)
#         if module_settings_data is not None:
#             for ms in module_settings_data:
#                 InstitutionModuleSetting.objects.update_or_create(
#                     institution=instance,
#                     module_key=ms["module_key"],
#                     defaults={
#                         "enabled": ms.get("enabled", False),
#                         "effective_from": ms.get("effective_from"),
#                         "changed_by_actor_id": actor_id,
#                     },
#                 )

#         AuditEvent.objects.create(
#             institution=instance,
#             actor_id=actor_id,
#             action="INSTITUTION_UPDATE",
#             resource_type="Institution",
#             resource_slug=str(instance.id),
#             outcome=OperationOutcome.SUCCEEDED,
#         )

#         return instance


# -----------------------------------------------------------------------------
# Branch serializers (read)
# -----------------------------------------------------------------------------

class BranchListSerializer(serializers.ModelSerializer):
    institution_slug = serializers.CharField(source="institution.slug", read_only=True)

    class Meta:
        model = Branch
        fields = [
            "code",
            "institution_slug",
            "name",
            "is_main",
            "status",
            "country",
            "state",
            "city",
        ]
        read_only_fields = fields


class BranchDetailSerializer(serializers.ModelSerializer):
    institution_slug = serializers.CharField(source="institution.slug", read_only=True)
    primary_admin = BranchPrimaryAdminReadSerializer(read_only=True)

    class Meta:
        model = Branch
        fields = [
            "code",
            "institution_slug",
            "name",
            "is_main",

            "category",
            "plan_tier",

            "address",
            "email",
            "website",
            "phone_number",

            "country",
            "state",
            "city",
            "timezone",
            "currency",

            "status",
            "opened_at",
            "activated_at",
            "closed_at",
            "deleted_at",

            "created_at",
            "updated_at",

            # Nested read
            "primary_admin",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# Branch serializers (write)
# -----------------------------------------------------------------------------

class BranchCreateSerializer(serializers.ModelSerializer):
    """
    Creates a branch under a given institution.

    Notes:
    - code is AutoField; you don't supply it.
    - business rule: only one main branch per institution enforced by constraint.
    - you can optionally auto-set opened_at if not provided.
    """

    class Meta:
        model = Branch
        fields = [
            "institution",  # FK
            "name",
            "is_main",
            "category",
            "plan_tier",

            "address",
            "email",
            "website",
            "phone_number",

            "country",
            "state",
            "city",
            "timezone",
            "currency",
            "opened_at",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Example: if is_main=True, ensure no other main branch exists (friendly error before DB constraint)
        institution = attrs.get("institution")
        is_main = attrs.get("is_main", False)
        if institution and is_main:
            if Branch.objects.filter(institution=institution, is_main=True).exists():
                raise serializers.ValidationError({"is_main": "This institution already has a main branch."})

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> Branch:
        # Set default lifecycle state if you want it always created as pending
        branch = Branch.objects.create(
            **validated_data,
            status=BranchStatus.PENDING,
        )

        # Optional: open immediately if opened_at missing
        if branch.opened_at is None:
            branch.opened_at = timezone.now()
            branch.save(update_fields=["opened_at", "updated_at"])

        BranchLifecycle.objects.create(
            branch=branch,
            from_state="",
            to_state=BranchStatus.PENDING,
            actor_id=str(self.context.get("actor_id", "system")),
            reason="Branch created",
        )

        return branch


class BranchUpdateSerializer(serializers.ModelSerializer):
    """
    Updates branch metadata and contact/location details.
    Keep status transitions in a dedicated serializer if you want strict lifecycle policies.
    """

    class Meta:
        model = Branch
        fields = [
            "name",
            "is_main",
            "category",
            "plan_tier",

            "address",
            "email",
            "website",
            "phone_number",

            "country",
            "state",
            "city",
            "timezone",
            "currency",
            "opened_at",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        branch: Branch = self.instance
        # Friendly guard: if turning this branch into main, ensure no other main exists
        if "is_main" in attrs and attrs["is_main"] is True:
            exists_other_main = Branch.objects.filter(
                institution=branch.institution,
                is_main=True,
            ).exclude(code=branch.code).exists()
            if exists_other_main:
                raise serializers.ValidationError({"is_main": "Another main branch already exists for this institution."})
        return attrs

    @transaction.atomic
    def update(self, instance: Branch, validated_data: Dict[str, Any]) -> Branch:
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.full_clean()
        instance.save()
        return instance


# -----------------------------------------------------------------------------
# Institution serializers (read)
# -----------------------------------------------------------------------------

class InstitutionListSerializer(serializers.ModelSerializer):
    """
    Institution list now shows tenant identity + status.
    Location fields moved to Branch (main branch can be shown via nested/flattened approach).
    """
    main_branch = BranchListSerializer(read_only=True)

    class Meta:
        model = Institution
        fields = [
            "name",
            "slug",
            "_type",
            "status",
            "activated_at",
            "main_branch",
        ]
        read_only_fields = fields


class InstitutionDetailSerializer(serializers.ModelSerializer):
    """
    Detail includes tenant identity + nested branches.
    Location/contact details are on branches; main branch can be highlighted via main_branch field.
    """
    branches = BranchDetailSerializer(many=True, read_only=True)
    main_branch = BranchDetailSerializer(read_only=True)
    branding = InstitutionBrandingSerializer(read_only=True)
    module_settings = InstitutionModuleSettingSerializer(many=True, read_only=True)

    class Meta:
        model = Institution
        fields = [
            "name",
            "slug",
            "_type",
            "status",
            "activated_at",
            "deleted_at",

            # Convenient reads
            "main_branch",
            "branches",

            # Nested institution-level
            "branding",
            "module_settings",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# Institution serializers (write)
# -----------------------------------------------------------------------------

class InstitutionCreateSerializer(serializers.ModelSerializer):
    """
    Creates Institution (tenant identity) and optionally creates an initial MAIN branch.

    Old serializer created institution and handled nested:
      - Branding
      - Primary admin
      - Module settings

    Those can remain here, while Branch creation handles the location/contact fields.
    """

    # Create an initial branch in same request
    main_branch = BranchCreateSerializer(required=False)

    branding = InstitutionBrandingSerializer(required=False)
    primary_admin = BranchPrimaryAdminWriteSerializer(required=False)
    module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

    class Meta:
        model = Institution
        fields = [
            "name",
            "slug",
            "_type",

            # optional nested
            "main_branch",
            "primary_admin",
            "branding",
            "module_settings",
        ]

    def validate_slug(self, value: str) -> str:
        if value is None:
            return ""
        normalized = _normalize_slug(value)
        if not normalized:
            return ""
        if normalized in RESERVED_TENANT_SLUGS:
            raise serializers.ValidationError("This slug is reserved. Choose another.")
        if not _slug_is_unique(normalized):
            suggestions = _build_slug_suggestions(normalized)
            raise serializers.ValidationError({"message": "Slug already exists.", "suggestions": suggestions})
        return normalized

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        raw_slug = (attrs.get("slug") or "").strip()
        if not raw_slug:
            base = _normalize_slug(attrs.get("name", ""))
            if not base:
                raise serializers.ValidationError({"slug": "Unable to generate slug from name. Provide slug explicitly."})
            if base in RESERVED_TENANT_SLUGS:
                base = f"{base}-institution"
            if not _slug_is_unique(base):
                suggestions = [s for s in _build_slug_suggestions(base) if _slug_is_unique(s)]
                raise serializers.ValidationError({"slug": {"message": "Generated slug conflicts.", "suggestions": suggestions}})
            attrs["slug"] = base

        # Optional: enforce that nested main_branch, if provided, is_main must be True
        mb = attrs.get("main_branch")
        if mb and mb.get("is_main") is False:
            raise serializers.ValidationError({"main_branch": {"is_main": "main_branch must have is_main=true."}})

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> Institution:
        main_branch_data = validated_data.pop("main_branch", None)

        branding_data = validated_data.pop("branding", None)
        primary_admin_data = validated_data.pop("primary_admin", None)
        module_settings_data = validated_data.pop("module_settings", [])

        institution = Institution.objects.create(
            **validated_data,
            status=InstitutionStatus.ACTIVE,
        )

        # Create main branch (recommended: always have one)
        if main_branch_data:
            # Ensure institution FK is set correctly
            main_branch_data["institution"] = institution
            # Ensure is_main=True for main_branch payload
            main_branch_data["is_main"] = True
            branch = BranchCreateSerializer(context=self.context).create(main_branch_data)
        else:
            # If not provided, create a minimal default main branch
            branch = Branch.objects.create(
                institution=institution,
                name=f"{institution.name}",
                is_main=True,
                category="",
                plan_tier=PlanTier.STARTER,
                status=BranchStatus.PENDING,
                opened_at=timezone.now(),
            )

        BranchLifecycle.objects.create(
            branch = branch,
            from_state="",
            to_state=institution.status,
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
            from .models import BranchPrimaryAdmin

            BranchPrimaryAdmin.objects.create(
                branch = branch,
                contact=contact,
                role_label=primary_admin_data.get("role_label", "BR_AD"),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=timezone.now(),
                invite_sent_at=None,
            )

        return institution


class InstitutionUpdateSerializer(serializers.ModelSerializer):
    """
    Updates tenant identity fields only.
    Branch details are updated via BranchUpdateSerializer.
    """

    branding = InstitutionBrandingSerializer(required=False)
    module_settings = InstitutionModuleSettingSerializer(many=True, required=False)

    class Meta:
        model = Institution
        fields = [
            "_type",
            "status",       # include only if you allow direct status updates here

            # optional nested
            "branding",
            "module_settings",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
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

        return instance


# -----------------------------------------------------------------------------
# Lifecycle & Operations serializers (write)
# -----------------------------------------------------------------------------

TransitionChoiceField = {
    "ACTIVE": "Branch_Activated",
    "SUSPENDED": "Branch_Suspended",
    "INACTIVE": "Branch_Deactivated",
    "PENDING": "Branch_Pending",
    "CLOSED": "Branch_Closed",
}

class BranchStateTransitionSerializer(serializers.Serializer):
    to_state = serializers.ChoiceField(choices=BranchStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> Branch:
        branch: Branch = self.context["branch"]
        actor_id = str(self.context.get("actor_id", "system"))
        to_state = self.validated_data["to_state"]
        reason = self.validated_data.get("reason", "")

        if to_state not in TransitionChoiceField:
            raise serializers.ValidationError(f"Invalid to_state: {to_state}")
        
        if branch.status == to_state:
            raise serializers.ValidationError(f"Branch is already {to_state}.")
        
        branch.transition(to_state=to_state, actor_id=actor_id, reason=reason)

        return branch


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

        AuditEvent.objects.create(
            institution=institution,
            actor_id=actor_id,
            action="TENANT_RESET_CONFIG",
            resource_type="Institution",
            resource_slug=str(institution.slug),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return institution
