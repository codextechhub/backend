from __future__ import annotations

from django.utils import timezone
from typing import Any, Dict, List, Optional, Tuple
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from .models import (
    ContactInfo,
    SchoolPackageSetup,
    InviteStatus,
    BranchStatus,
    RESERVED_TENANT_SLUGS,
    School,
    Branch,
    BranchPrimaryAdmin,
    SchoolPrimaryAdmin,
    SchoolBranding,
    BranchLifecycle,
    SchoolStatus,
    PackagePlan,
    XVSModules,
)
from vs_audit.models import (
    AuditEvent, 
    EntityAuditTrail,
    AuditModuleKey,
    AuditActionType
)

from vs_audit.services import AuditDiffService


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


def _slug_is_unique(slug: str, exclude_school_slug: Optional[str] = None) -> bool:
    qs = School.objects.all()
    if exclude_school_slug:
        qs = qs.exclude(slug=exclude_school_slug)
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


class SchoolBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchoolBranding
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


# ---------------------------------------------------------------
# Package Setup serializers
# ---------------------------------------------------------------

class PackagePlanSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a PackagePlan catalog entry.
    Used for listing available plans in the Package Plan dropdown.
    """
    class Meta:
        model = PackagePlan
        fields = [
            "id",
            "name",
            "code",
            "description",
            "billing_cycle",
            "max_students",
            "max_teachers",
            "max_admins",
            "max_branch",
            "is_active",
        ]
        read_only_fields = fields


class XVSModuleSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a platform module.
    Used for listing available modules in the Enabled Modules dropdown.
    """
    class Meta:
        model = XVSModules
        fields = [
            "id",
            "key",
            "name",
            "description",
            "is_active",
        ]
        read_only_fields = fields


class SchoolPackageSetupWriteSerializer(serializers.Serializer):
    """
    Write-only structure for submitting package setup during school creation.

    Accepts `package_plan` as the PackagePlan `code` (slug) — more stable
    than a numeric PK and matches what the dropdown naturally emits.
    Accepts `enabled_modules` as a list of XVSModules `key` strings.

    Validation enforces:
    - package_plan must exist and be active.
    - All module keys must exist and be active.
    - Capacities must be >= 1.
    - Capacities must not exceed plan limits.
    - subscription_expires_at must not be in the past.
    """

    package_plan = serializers.SlugRelatedField(
        slug_field="code",
        queryset=PackagePlan.objects.filter(is_active=True),
        help_text="The `code` of the PackagePlan to assign. E.g. 'basic', 'premium'.",
    )

    enabled_modules = serializers.ListField(
        child=serializers.SlugRelatedField(
            slug_field="key",
            queryset=XVSModules.objects.filter(is_active=True),
        ),
        required=False,
        default=list,
        help_text="List of module `key` strings to enable. E.g. ['students', 'attendance'].",
    )

    student_capacity = serializers.IntegerField(min_value=1)
    teacher_capacity = serializers.IntegerField(min_value=1)
    admin_capacity = serializers.IntegerField(min_value=1)

    subscription_expires_at = serializers.DateField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Optional. Date the subscription expires. Cannot be in the past.",
    )

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        plan: PackagePlan = attrs["package_plan"]
        errors = {}

        # --- Capacity vs plan limits ---
        if plan.max_students is not None and attrs["student_capacity"] > plan.max_students:
            errors["student_capacity"] = (
                f"Exceeds plan limit of {plan.max_students} students."
            )

        if plan.max_teachers is not None and attrs["teacher_capacity"] > plan.max_teachers:
            errors["teacher_capacity"] = (
                f"Exceeds plan limit of {plan.max_teachers} teachers."
            )

        if plan.max_admins is not None and attrs["admin_capacity"] > plan.max_admins:
            errors["admin_capacity"] = (
                f"Exceeds plan limit of {plan.max_admins} admins."
            )

        # --- Subscription expiry ---
        expires_at = attrs.get("subscription_expires_at")
        if expires_at and expires_at < timezone.localdate():
            errors["subscription_expires_at"] = (
                "Subscription expiry date cannot be in the past."
            )

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class SchoolPackageSetupReadSerializer(serializers.ModelSerializer):
    """
    Read-only nested representation of a package setup.
    Returned in SchoolDetailSerializer.
    """
    package_plan = PackagePlanSerializer(read_only=True)
    enabled_modules = XVSModuleSerializer(many=True, read_only=True)

    class Meta:
        model = SchoolPackageSetup
        fields = [
            "id",
            "package_plan",
            "enabled_modules",
            "student_capacity",
            "teacher_capacity",
            "admin_capacity",
            "subscription_expires_at",
            "is_active",
            "notes",
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
    branch_role = serializers.CharField(max_length=80, required=False, allow_blank=True, default="Head Teacher")
    role_label = serializers.CharField(max_length=80, required=False, allow_blank=True, default="BRANCH_ADMIN")

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class BranchPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns link + contact."""
    id = serializers.CharField()
    branch_role = serializers.CharField()
    role_label = serializers.CharField()
    invite_status = serializers.CharField()
    invite_queued_at = serializers.DateTimeField(allow_null=True)
    invite_sent_at = serializers.DateTimeField(allow_null=True)
    contact = ContactInfoSerializer()


class SchoolPrimaryAdminWriteSerializer(serializers.Serializer):
    """Write-only structure for assigning school-level primary admin."""

    full_name = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    school_role = serializers.CharField(max_length=80, required=False, allow_blank=True, default="IT Head")
    role_label = serializers.CharField(max_length=80, required=False, allow_blank=True, default="SCHOOL_ADMIN")

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class SchoolPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns school admin link + contact."""
    id = serializers.CharField()
    school_role = serializers.CharField()
    role_label = serializers.CharField()
    invite_status = serializers.CharField()
    invite_queued_at = serializers.DateTimeField(allow_null=True)
    invite_sent_at = serializers.DateTimeField(allow_null=True)
    contact = ContactInfoSerializer()


# -----------------------------------------------------------------------------
# Branch serializers (read)
# -----------------------------------------------------------------------------

class BranchListSerializer(serializers.ModelSerializer):
    school_slug = serializers.CharField(source="school.slug", read_only=True)

    class Meta:
        model = Branch
        fields = [
            "code",
            "school_slug",
            "name",
            "is_main",
            "_type",
            "status",
            "country",
            "state",
        ]
        read_only_fields = fields


class BranchDetailSerializer(serializers.ModelSerializer):
    school_slug = serializers.CharField(source="school.slug", read_only=True)
    primary_admin = BranchPrimaryAdminReadSerializer(read_only=True)

    class Meta:
        model = Branch
        fields = [
            "code",
            "school_slug",
            "name",
            "is_main",
            "_type",

            "address",
            "email",

            "country",
            "state",

            "status",
            "opened_at",

            # Nested read
            "primary_admin",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# Branch serializers (write)
# -----------------------------------------------------------------------------

class BranchCreateSerializer(serializers.ModelSerializer):
    """
    Creates a branch under a given school.

    Notes:
    - code is AutoField; you don't supply it.
    - business rule: only one main branch per school enforced by constraint.
    - you can optionally auto-set opened_at if not provided.
    """

    primary_admin_data = BranchPrimaryAdminWriteSerializer(required=False, write_only=True)

    class Meta:
        model = Branch
        fields = [
            "name",
            "is_main",
            "_type",

            "address",
            "email",

            "country",
            "state",
            "opened_at",

            # optional nested
            "primary_admin_data",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # Example: if is_main=True, ensure no other main branch exists (friendly error before DB constraint)
        school = self.context.get("school")
        is_main = attrs.get("is_main", False)
        if school and is_main:
            if Branch.objects.filter(school=school, is_main=True).exists():
                raise serializers.ValidationError({"is_main": "This school already has a main branch."})

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> Branch:
        primary_admin_data = validated_data.pop("primary_admin_data", None)
        school = self.context.get("school")
        
        # Set default lifecycle state if you want it always created as pending
        branch = Branch.objects.create(
            school=school,
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
            actor_id=self.context.get("actor_id", "system"),
            reason="Branch created",
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
                branch=branch,
                contact=contact,
                branch_role=primary_admin_data.get("branch_role", "Head Teacher"),
                role_label=primary_admin_data.get("role_label", "BRANCH_ADMIN"),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=timezone.now(),
                invite_sent_at=None,
            )
        else:
            raise serializers.ValidationError({"primary_admin_data": "Primary admin information is required to create a branch."})
        
        audit_e = AuditEvent.objects.create(
            module_key=AuditModuleKey.BRANCH,
            action_type=AuditActionType.CREATE,
            actor_user=self.context.get("actor_id", "system"),
            entity_type="Branch",
            entity_id=str(branch.code),
            entity_label=branch.name,
            before_data={},
            diff_data=AuditDiffService.from_instances(
                before_instance=None, 
                after_instance=branch,
                exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deleted_at"],
            )['diff'],
        )

        trail = EntityAuditTrail.objects.create(
            entity_type="Branch",
            entity_id=str(branch.code),
            entity_label=branch.name,
        )
        trail.register_event(audit_e)

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
            "_type",

            "address",
            "email",

            "country",
            "state",
            "opened_at",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        branch: Branch = self.instance
        # Friendly guard: if turning this branch into main, ensure no other main exists
        if "is_main" in attrs and attrs["is_main"] is True:
            exists_other_main = Branch.objects.filter(
                school=branch.school,
                is_main=True,
            ).exclude(code=branch.code).exists()
            if exists_other_main:
                raise serializers.ValidationError({"is_main": "Another main branch already exists for this school."})
        return attrs

    @transaction.atomic
    def update(self, instance: Branch, validated_data: Dict[str, Any]) -> Branch:
        before_instance = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deleted_at"],
        )
        
        changes = 0
        for attr, value in validated_data.items():
            if getattr(instance, attr) != value:  
                changes += 1
                setattr(instance, attr, value)
        
        if changes == 0:
            raise serializers.ValidationError({"detail": "No changes detected in update payload."})
        
        instance.full_clean()
        instance.save()
        
        after_instance = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deleted_at"],
        )

        audit_e = AuditEvent.objects.create(
            module_key=AuditModuleKey.BRANCH,
            action_type=AuditActionType.UPDATE,
            actor_user=self.context.get("actor_id", "system"),
            entity_type="Branch",
            entity_id=str(instance.code),
            entity_label=instance.name,
            before_data=before_instance,
            diff_data=AuditDiffService.diff_dicts(
                before_data=before_instance,
                after_data=after_instance,
            )
        )

        trail, _ = EntityAuditTrail.objects.get_or_create(
            entity_type="Branch",
            entity_id=str(instance.code),
            defaults={"entity_label": instance.name},
        )
        _.register_event(audit_e) if _ else trail.register_event(audit_e)

        return instance


# -----------------------------------------------------------------------------
# School serializers (read)
# -----------------------------------------------------------------------------

class SchoolListSerializer(serializers.ModelSerializer):
    """
    School list now shows tenant identity + status.
    Location fields moved to Branch (main branch can be shown via nested/flattened approach).
    """
    main_branch = BranchListSerializer(read_only=True)
    total_students = serializers.ReadOnlyField(default=0)

    class Meta:
        model = School
        fields = [
            "name",
            "slug",
            "code",
            "ownership_type",
            "status",
            "activated_at",
            "total_students",
            "main_branch",
        ]
        read_only_fields = fields


class SchoolDetailSerializer(serializers.ModelSerializer):
    """
    Detail includes tenant identity + nested branches.
    Location/contact details are on branches; main branch can be highlighted via main_branch field.
    """
    branches = BranchDetailSerializer(many=True, read_only=True)
    main_branch = BranchDetailSerializer(read_only=True)
    branding = SchoolBrandingSerializer(read_only=True)
    primary_admin = SchoolPrimaryAdminReadSerializer(read_only=True)
    package_setup = SchoolPackageSetupReadSerializer(read_only=True)

    class Meta:
        model = School
        fields = [
            "name",
            "slug",
            "code",
            "ownership_type",
            "address",
            "website",
            "motto",
            "term_structure",
            "currency",
            "registration_id",
            "status",
            "activated_at",
            "deactivated_at",

            # Convenient reads
            "main_branch",
            "branches",

            # Nested school-level
            "branding",
            "primary_admin",
            "package_setup",

            # Extras
            "total_students",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# School serializers (write)
# -----------------------------------------------------------------------------

class BranchInlineCreateSerializer(serializers.Serializer):
    """
    Represents a single branch entry submitted inline during school creation.

    This is intentionally a plain Serializer (not ModelSerializer) because it is
    used as a nested write structure — the actual Branch model creation happens
    inside SchoolCreateSerializer.create(), not here.

    Each branch entry must include primary_admin_data.
    is_main defaults to False. Exactly one branch should have is_main=True.
    """

    name = serializers.CharField(max_length=255)
    _type = serializers.CharField(max_length=80)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    country = serializers.CharField(max_length=80, default="Nigeria")
    state = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    is_main = serializers.BooleanField(default=False)
    opened_at = serializers.DateTimeField(required=False, allow_null=True, default=None)

    primary_admin_data = BranchPrimaryAdminWriteSerializer(required=True)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Branch name cannot be empty.")
        return value.strip()


class SchoolCreateSerializer(serializers.ModelSerializer):

    """
    Creates an School with optional:
      - Branding
      - School-level primary admin
      - One or more branches (each with their own branch admin)

    The `branches` field accepts a list of branch objects.
    Business rules enforced here:
      - At most ONE branch may have is_main=True.
      - If any branches are submitted, exactly one must be is_main=True.
      - Branch names must be unique within the submission.
    """

    slug = serializers.CharField(required=False, allow_blank=True)
    branding = SchoolBrandingSerializer(required=False)
    primary_admin_data = SchoolPrimaryAdminWriteSerializer(required=False, write_only=True)
    branches = BranchInlineCreateSerializer(many=True, required=False, default=list, write_only=True)
    package_setup_data = SchoolPackageSetupWriteSerializer(required=False, write_only=True)

    class Meta:
        model = School
        fields = [
            "name",
            "slug",
            "code",
            "ownership_type",
            "address",
            "website",
            "motto",
            "term_structure",
            "currency",
            "registration_id",

            # optional nested
            "branding",
            "primary_admin_data",
            "branches",
            "package_setup_data",
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
            raise serializers.ValidationError({
                "message": "Slug already exists.",
                "suggestions": suggestions
            })
        return normalized

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        # --- Slug auto-generation (unchanged from before) ---
        raw_slug = (attrs.get("slug") or "").strip()
        if not raw_slug:
            base = _normalize_slug(attrs.get("name", ""))
            if not base:
                raise serializers.ValidationError({
                    "slug": "Unable to generate slug from name. Provide slug explicitly."
                })
            if base in RESERVED_TENANT_SLUGS:
                base = f"{base}-school"
            if not _slug_is_unique(base):
                suggestions = [s for s in _build_slug_suggestions(base) if _slug_is_unique(s)]
                raise serializers.ValidationError({
                    "slug": {"message": "Generated slug conflicts.", "suggestions": suggestions}
                })
            attrs["slug"] = base

        # --- Branch-level validations ---
        branches = attrs.get("branches", [])

        if branches:
            # Rule 1: Branch names must be unique within the submission
            names = [b["name"].strip().lower() for b in branches]
            if len(names) != len(set(names)):
                raise serializers.ValidationError({
                    "branches": "Each branch must have a unique name within this submission."
                })

            # Rule 2: Exactly one branch must be marked as main
            main_branches = [b for b in branches if b.get("is_main", False)]
            if len(main_branches) == 0:
                raise serializers.ValidationError({
                    "branches": "Exactly one branch must be marked as is_main=true."
                })
            if len(main_branches) > 1:
                raise serializers.ValidationError({
                    "branches": "Only one branch can be marked as is_main=true."
                })

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> School:
        branding_data = validated_data.pop("branding", None)
        primary_admin_data = validated_data.pop("primary_admin_data", None)
        branches_data = validated_data.pop("branches", [])
        package_setup_data = validated_data.pop("package_setup_data", None)

        # --- 1. Create the School ---
        school = School.objects.create(
            **validated_data,
            status=SchoolStatus.ACTIVE,
            activated_at=timezone.now(),
        )

        # --- 2. Optional branding ---
        if branding_data:
            SchoolBranding.objects.create(school=school, **branding_data)

        # --- 3. Optional school-level primary admin ---
        if primary_admin_data:
            contact = ContactInfo.objects.create(
                full_name=primary_admin_data["full_name"],
                email=primary_admin_data["email"],
                phone=primary_admin_data.get("phone", ""),
            )
            SchoolPrimaryAdmin.objects.create(
                school=school,
                contact=contact,
                school_role=primary_admin_data.get("school_role", "IT Head"),
                role_label=primary_admin_data.get("role_label", "SCHOOL_ADMIN"),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=timezone.now(),
                invite_sent_at=None,
            )

        # --- 4. Create branches inline ---
        for branch_data in branches_data:
            branch_admin_data = branch_data.pop("primary_admin_data", None)

            branch = Branch.objects.create(
                school=school,
                status=BranchStatus.PENDING,
                opened_at=branch_data.pop("opened_at", None) or timezone.now(),
                **branch_data,
            )

            # Log initial lifecycle event
            BranchLifecycle.objects.create(
                branch=branch,
                from_state="",
                to_state=BranchStatus.PENDING,
                actor_id=self.context.get("actor_id", "system"),
                reason="Branch created during school onboarding",
            )

            # Create branch admin if provided
            if branch_admin_data:
                contact = ContactInfo.objects.create(
                    full_name=branch_admin_data["full_name"],
                    email=branch_admin_data["email"],
                    phone=branch_admin_data.get("phone", ""),
                )
                BranchPrimaryAdmin.objects.create(
                    branch=branch,
                    contact=contact,
                    branch_role=branch_admin_data.get("branch_role", "Head Teacher"),
                    role_label=branch_admin_data.get("role_label", "BRANCH_ADMIN"),
                    invite_status=InviteStatus.QUEUED,
                    invite_queued_at=timezone.now(),
                    invite_sent_at=None,
                )
        
            # branch audit trail for creation
            audit_branch = AuditEvent.objects.create(
                module_key=AuditModuleKey.BRANCH,
                action_type=AuditActionType.CREATE,
                actor_user=self.context.get("actor_id", "system"),
                entity_type="Branch",
                entity_id=str(branch.code),
                entity_label=branch.name,
                before_data={},
                diff_data=AuditDiffService.from_instances(
                    before_instance=None, 
                    after_instance=branch,
                    exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deleted_at"],
                )['diff'],
            )

            trail = EntityAuditTrail.objects.create(
                entity_type="Branch",
                entity_id=f"{str(branch.school.slug)}-{str(branch.code)}",
                entity_label=branch.name,
            )
            trail.register_event(audit_branch)
        
        # --- 5. Optional package setup ---
        if package_setup_data:
            enabled_modules = package_setup_data.pop("enabled_modules", [])

            # subscription_expires_at defaults to 1 year if not provided
            expires_at = package_setup_data.pop("subscription_expires_at", None)
            if not expires_at:
                from datetime import date
                from dateutil.relativedelta import relativedelta
                expires_at = date.today() + relativedelta(years=1)

            setup = SchoolPackageSetup.objects.create(
                school=school,
                subscription_expires_at=expires_at,
                **package_setup_data,
            )

            # Assign M2M modules after creation
            if enabled_modules:
                setup.enabled_modules.set(enabled_modules)

        # --- 6. Audit trail for school ---
        audit_school = AuditEvent.objects.create(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.CREATE,
            actor_user=self.context.get("actor_id", "system"),
            entity_type="School",
            entity_id=str(school.slug),
            entity_label=school.name,
            before_data={},
            diff_data=AuditDiffService.from_instances(
                before_instance=None,
                after_instance=school,
                exclude_fields=["created_at", "updated_at", "activated_at", "deactivated_at"],
            )['diff'],
        )
        trail = EntityAuditTrail.objects.create(
            entity_type="School",
            entity_id=str(school.slug),
            entity_label=school.name,
        )
        trail.register_event(audit_school)

        return school
    

class SchoolUpdateSerializer(serializers.ModelSerializer):
    """
    Updates tenant identity fields only.
    Branch details are updated via BranchUpdateSerializer.
    """

    branding = SchoolBrandingSerializer(required=False)

    class Meta:
        model = School
        fields = [
            "ownership_type",
            "address",
            "website",
            "motto",
            "term_structure",
            "currency",
            "registration_id",
            "status",       # include only if you allow direct status updates here

            # optional nested
            "branding",
        ]

    @transaction.atomic
    def update(self, instance: School, validated_data: Dict[str, Any]) -> School:
        branding_data = validated_data.pop("branding", None)

        changes = 0
        for attr, value in validated_data.items():
            if getattr(instance, attr) != value:  
                changes += 1
                setattr(instance, attr, value)
        
        if changes == 0:
            raise serializers.ValidationError({"detail": "No changes detected in update payload."})
        
        instance.full_clean()
        instance.save()

        actor_id = self.context.get("actor_id", "system")

        # Branding upsert
        if branding_data is not None:
            SchoolBranding.objects.update_or_create(
                school=instance,
                defaults=branding_data,
            )

        return instance


# -----------------------------------------------------------------------------
# Lifecycle & Operations serializers (write)
# -----------------------------------------------------------------------------

Branch_Transition_Choice = [
    ("ACTIVE", "Branch Activated"),
    ("SUSPENDED", "Branch Suspended"),
    ("INACTIVE", "Branch Deactivated"),
    ("PENDING", "Branch Pending"),
    ("CLOSED", "Branch Closed"),
]

class BranchStateTransitionSerializer(serializers.Serializer):
    to_state = serializers.ChoiceField(choices=Branch_Transition_Choice)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> Branch:
        branch: Branch = self.context["branch"]
        actor_id = self.context.get("actor_id", "system")
        to_state = self.validated_data["to_state"]
        reason = self.validated_data.get("reason", "")

        if to_state not in [choice[0] for choice in Branch_Transition_Choice]:
            raise serializers.ValidationError(f"Invalid to_state: {to_state}")
        
        if branch.status == to_state:
            raise serializers.ValidationError(f"Branch is already {to_state}.", code=400)
        
        branch.transition(to_state=to_state, actor_id=actor_id, reason=reason)

        return branch


class SchoolResetConfigSerializer(serializers.Serializer):
    """
    Resets school configuration to baseline (branding/modules/localization),
    without deleting core operational data (policy-driven).
    """
    confirmation_token = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> School:
        school: School = self.context["school"]
        actor_id = self.context.get("actor_id", "system")

        token = (self.validated_data.get("confirmation_token") or "").strip()
        if not token:
            raise serializers.ValidationError({"confirmation_token": "Confirmation token is required."})

        # Baseline reset example:
        # - Remove branding
        # - Disable all modules (or re-seed defaults depending on your product policy)
        # - Clear localization (optional; many teams keep localization)
        SchoolBranding.objects.filter(school=school).delete()

        return school